package registryinspect

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const userAgent = "global-executables-registry-crawl/2.0 (+https://github.com/asopitech-labs/global-executables)"

type Config struct {
	BaseURL                string
	Client                 *http.Client
	RequestTimeout         time.Duration
	PackageTimeout         time.Duration
	MaxAttempts            int
	BaseBackoff            time.Duration
	MaxBackoff             time.Duration
	MaxRetryAfter          time.Duration
	MaxMetadataBytes       int64
	MaxArtifactBytes       int64
	MaxFullDownloadBytes   int64
	RangeBlockSize         int64
	RangeCacheBlocks       int
	InitialHostConcurrency int
	MaxHostConcurrency     int
	MinRequestInterval     time.Duration
	CircuitThreshold       int
	CircuitOpenDuration    time.Duration
	Sleep                  func(context.Context, time.Duration) error
	Jitter                 func(time.Duration) time.Duration
}

type Metrics struct {
	Requests        uint64
	RateLimited     uint64
	Timeouts        uint64
	CircuitOpens    uint64
	HostConcurrency int
}

type metricCounters struct {
	requests     atomic.Uint64
	rateLimited  atomic.Uint64
	timeouts     atomic.Uint64
	circuitOpens atomic.Uint64
}

func (m *metricCounters) snapshot() Metrics {
	return Metrics{Requests: m.requests.Load(), RateLimited: m.rateLimited.Load(), Timeouts: m.timeouts.Load(), CircuitOpens: m.circuitOpens.Load()}
}

type responseData struct {
	statusCode int
	header     http.Header
	body       []byte
}

type HTTPError struct {
	StatusCode int
	URL        string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, http.StatusText(e.StatusCode))
}

type requester struct {
	config  Config
	client  *http.Client
	metrics metricCounters
	gatesMu sync.Mutex
	gates   map[string]*adaptiveGate
}

func newRequester(config Config) *requester {
	if config.RequestTimeout <= 0 {
		config.RequestTimeout = 30 * time.Second
	}
	if config.PackageTimeout <= 0 {
		config.PackageTimeout = 90 * time.Second
	}
	if config.MaxAttempts <= 0 {
		config.MaxAttempts = 3
	}
	if config.BaseBackoff <= 0 {
		config.BaseBackoff = 200 * time.Millisecond
	}
	if config.MaxBackoff <= 0 {
		config.MaxBackoff = 15 * time.Second
	}
	if config.MaxRetryAfter <= 0 {
		config.MaxRetryAfter = 5 * time.Minute
	}
	if config.MaxMetadataBytes <= 0 {
		config.MaxMetadataBytes = 8 * 1024 * 1024
	}
	if config.MaxArtifactBytes <= 0 {
		config.MaxArtifactBytes = 128 * 1024 * 1024
	}
	if config.MaxFullDownloadBytes <= 0 {
		config.MaxFullDownloadBytes = 16 * 1024 * 1024
	}
	if config.RangeBlockSize <= 0 {
		config.RangeBlockSize = 128 * 1024
	}
	if config.RangeCacheBlocks <= 0 {
		config.RangeCacheBlocks = 16
	}
	if config.MaxHostConcurrency <= 0 {
		config.MaxHostConcurrency = 32
	}
	if config.InitialHostConcurrency <= 0 || config.InitialHostConcurrency > config.MaxHostConcurrency {
		config.InitialHostConcurrency = config.MaxHostConcurrency
	}
	if config.CircuitThreshold <= 0 {
		config.CircuitThreshold = 8
	}
	if config.CircuitOpenDuration <= 0 {
		config.CircuitOpenDuration = 5 * time.Second
	}
	if config.Sleep == nil {
		config.Sleep = sleepContext
	}
	if config.Jitter == nil {
		config.Jitter = func(duration time.Duration) time.Duration {
			if duration <= 1 {
				return duration
			}
			spread := duration / 5
			return duration - spread + time.Duration(rand.Int64N(int64(spread*2+1)))
		}
	}
	client := config.Client
	if client == nil {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.MaxIdleConns = 256
		transport.MaxIdleConnsPerHost = 128
		transport.IdleConnTimeout = 90 * time.Second
		transport.ForceAttemptHTTP2 = true
		client = &http.Client{Transport: transport}
	}
	return &requester{config: config, client: client, gates: make(map[string]*adaptiveGate)}
}
func (r *requester) metricsSnapshot() Metrics {
	metrics := r.metrics.snapshot()
	r.gatesMu.Lock()
	defer r.gatesMu.Unlock()
	for _, gate := range r.gates {
		limit := gate.limitValue()
		if metrics.HostConcurrency == 0 || limit < metrics.HostConcurrency {
			metrics.HostConcurrency = limit
		}
	}
	return metrics
}

func (r *requester) gateFor(target string) *adaptiveGate {
	host := target
	if parsed, err := url.Parse(target); err == nil && parsed.Host != "" {
		host = parsed.Host
	}
	r.gatesMu.Lock()
	defer r.gatesMu.Unlock()
	if gate := r.gates[host]; gate != nil {
		return gate
	}
	gate := newAdaptiveGate(r.config.InitialHostConcurrency, r.config.MaxHostConcurrency,
		r.config.CircuitThreshold, r.config.CircuitOpenDuration, r.config.MinRequestInterval, &r.metrics)
	r.gates[host] = gate
	return gate
}

func (r *requester) request(ctx context.Context, method, target string, headers http.Header, maxBytes int64) (responseData, error) {
	var lastErr error
	for attempt := 1; attempt <= r.config.MaxAttempts; attempt++ {
		gate := r.gateFor(target)
		if err := gate.acquire(ctx); err != nil {
			return responseData{}, err
		}
		requestCtx, cancel := context.WithTimeout(ctx, r.config.RequestTimeout)
		req, err := http.NewRequestWithContext(requestCtx, method, target, nil)
		if err != nil {
			gate.release(0, err, 0)
			cancel()
			return responseData{}, err
		}
		for key, values := range headers {
			for _, value := range values {
				req.Header.Add(key, value)
			}
		}
		req.Header.Set("User-Agent", userAgent)
		req.Header.Set("Accept-Encoding", "identity")
		r.metrics.requests.Add(1)
		resp, err := r.client.Do(req)
		if err != nil {
			gate.release(0, err, 0)
			cancel()
			lastErr = err
			if isTimeout(err) {
				r.metrics.timeouts.Add(1)
			}
			if ctx.Err() != nil {
				return responseData{}, ctx.Err()
			}
			if attempt < r.config.MaxAttempts && retryableNetworkError(err) {
				if err := r.wait(ctx, attempt, ""); err != nil {
					return responseData{}, err
				}
				continue
			}
			return responseData{}, err
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
		closeErr := resp.Body.Close()
		gate.release(resp.StatusCode, readErr, r.retryAfter(resp.Header.Get("Retry-After")))
		cancel()
		if readErr != nil {
			lastErr = readErr
			if attempt < r.config.MaxAttempts {
				if err := r.wait(ctx, attempt, ""); err != nil {
					return responseData{}, err
				}
				continue
			}
			return responseData{}, readErr
		}
		if closeErr != nil {
			return responseData{}, closeErr
		}
		if int64(len(body)) > maxBytes {
			return responseData{}, fmt.Errorf("response exceeded %d bytes", maxBytes)
		}
		result := responseData{statusCode: resp.StatusCode, header: resp.Header.Clone(), body: body}
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return result, nil
		}
		httpErr := &HTTPError{StatusCode: resp.StatusCode, URL: target}
		lastErr = httpErr
		if resp.StatusCode == http.StatusTooManyRequests {
			r.metrics.rateLimited.Add(1)
		}
		if attempt < r.config.MaxAttempts && retryableStatus(resp.StatusCode) {
			if err := r.wait(ctx, attempt, resp.Header.Get("Retry-After")); err != nil {
				return responseData{}, err
			}
			continue
		}
		return result, httpErr
	}
	return responseData{}, lastErr
}

type adaptiveGate struct {
	mu               sync.Mutex
	inFlight         int
	limit            int
	maximum          int
	successes        int
	consecutive      int
	circuitThreshold int
	circuitOpen      time.Duration
	blockedUntil     time.Time
	nextRequest      time.Time
	requestInterval  time.Duration
	changed          chan struct{}
	metrics          *metricCounters
}

func newAdaptiveGate(initial, maximum, threshold int, open, interval time.Duration, metrics *metricCounters) *adaptiveGate {
	return &adaptiveGate{limit: initial, maximum: maximum, circuitThreshold: threshold,
		circuitOpen: open, requestInterval: interval, changed: make(chan struct{}), metrics: metrics}
}

func (g *adaptiveGate) acquire(ctx context.Context) error {
	for {
		g.mu.Lock()
		now := time.Now()
		readyAt := g.blockedUntil
		if g.nextRequest.After(readyAt) {
			readyAt = g.nextRequest
		}
		if g.inFlight < g.limit && !now.Before(readyAt) {
			g.inFlight++
			g.nextRequest = now.Add(g.requestInterval)
			g.mu.Unlock()
			return nil
		}
		changed := g.changed
		blocked := time.Until(readyAt)
		g.mu.Unlock()
		if blocked > 0 {
			timer := time.NewTimer(blocked)
			select {
			case <-ctx.Done():
				timer.Stop()
				return ctx.Err()
			case <-changed:
				timer.Stop()
			case <-timer.C:
			}
			continue
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

func (g *adaptiveGate) release(status int, err error, retryAfter time.Duration) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.inFlight > 0 {
		g.inFlight--
	}
	transient := status == http.StatusTooManyRequests || status == http.StatusRequestTimeout || status >= 500 || isTimeout(err)
	if transient {
		g.limit = max(1, g.limit/2)
		g.successes = 0
		g.consecutive++
		now := time.Now()
		advertisedUntil := now.Add(retryAfter)
		if retryAfter > 0 && !now.Before(g.blockedUntil) {
			g.blockedUntil = advertisedUntil
			g.consecutive = 0
			g.metrics.circuitOpens.Add(1)
		} else if g.consecutive >= g.circuitThreshold && !now.Before(g.blockedUntil) {
			g.blockedUntil = now.Add(g.circuitOpen)
			g.consecutive = 0
			g.metrics.circuitOpens.Add(1)
		}
	} else if err == nil {
		g.consecutive = 0
		g.successes++
		if g.successes >= 128 && g.limit < g.maximum {
			g.limit++
			g.successes = 0
		}
	}
	close(g.changed)
	g.changed = make(chan struct{})
}

func (g *adaptiveGate) limitValue() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.limit
}

func (r *requester) wait(ctx context.Context, attempt int, retryAfter string) error {
	if advertised := r.retryAfter(retryAfter); advertised > 0 {
		return r.config.Sleep(ctx, advertised)
	}
	duration := r.config.Jitter(r.config.BaseBackoff << (attempt - 1))
	duration = min(duration, r.config.MaxBackoff)
	return r.config.Sleep(ctx, duration)
}

func (r *requester) retryAfter(value string) time.Duration {
	var duration time.Duration
	if seconds, err := strconv.Atoi(strings.TrimSpace(value)); err == nil && seconds >= 0 {
		duration = time.Duration(seconds) * time.Second
	} else if when, err := http.ParseTime(value); err == nil && time.Until(when) > 0 {
		duration = time.Until(when)
	}
	return min(duration, r.config.MaxRetryAfter)
}

func sleepContext(ctx context.Context, duration time.Duration) error {
	if duration <= 0 {
		return nil
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func retryableStatus(status int) bool {
	return status == http.StatusRequestTimeout || status == http.StatusTooManyRequests ||
		status == http.StatusInternalServerError || status == http.StatusBadGateway ||
		status == http.StatusServiceUnavailable || status == http.StatusGatewayTimeout
}

func retryableNetworkError(err error) bool {
	return !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded)
}

func isTimeout(err error) bool {
	var networkError net.Error
	return errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &networkError) && networkError.Timeout())
}

type permanentError struct{ error }

func classify(parent context.Context, err error) (gocrawlVerdict string, message string) {
	if parent.Err() != nil {
		return "canceled", parent.Err().Error()
	}
	var permanent permanentError
	if errors.As(err, &permanent) {
		return "permanent", err.Error()
	}
	var httpErr *HTTPError
	if errors.As(err, &httpErr) {
		switch httpErr.StatusCode {
		case http.StatusNotFound, http.StatusMethodNotAllowed, http.StatusGone, http.StatusUnavailableForLegalReasons:
			return "permanent", err.Error()
		}
	}
	return "retry", err.Error()
}
