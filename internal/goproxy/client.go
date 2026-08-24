package goproxy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type responseData struct {
	StatusCode int
	Header     http.Header
	Body       []byte
}

type HTTPError struct {
	StatusCode int
	Status     string
	URL        string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.StatusCode, e.Status)
}

func (i *Inspector) request(
	ctx context.Context,
	method string,
	url string,
	headers http.Header,
	maxBytes int64,
) (responseData, error) {
	var lastErr error
	for attempt := 1; attempt <= i.config.MaxAttempts; attempt++ {
		requestCtx := ctx
		cancel := func() {}
		if i.config.RequestTimeout > 0 {
			requestCtx, cancel = context.WithTimeout(ctx, i.config.RequestTimeout)
		}
		req, err := http.NewRequestWithContext(requestCtx, method, url, nil)
		if err != nil {
			cancel()
			return responseData{}, err
		}
		for key, values := range headers {
			for _, value := range values {
				req.Header.Add(key, value)
			}
		}
		req.Header.Set("Accept-Encoding", "identity")
		resp, err := i.client.Do(req)
		if err != nil {
			cancel()
			if ctx.Err() != nil {
				return responseData{}, ctx.Err()
			}
			lastErr = err
			if attempt < i.config.MaxAttempts && retryableNetworkError(err) {
				if err := i.sleep(ctx, i.backoff(attempt, "")); err != nil {
					return responseData{}, err
				}
				continue
			}
			return responseData{}, err
		}
		limit := maxBytes
		if limit < 0 {
			limit = i.config.MaxArchiveBytes
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, limit+1))
		closeErr := resp.Body.Close()
		cancel()
		if readErr != nil {
			lastErr = readErr
			if attempt < i.config.MaxAttempts {
				if err := i.sleep(ctx, i.backoff(attempt, "")); err != nil {
					return responseData{}, err
				}
				continue
			}
			return responseData{}, readErr
		}
		if closeErr != nil {
			return responseData{}, closeErr
		}
		if int64(len(body)) > limit {
			return responseData{}, fmt.Errorf("response exceeded %d bytes", limit)
		}
		result := responseData{StatusCode: resp.StatusCode, Header: resp.Header.Clone(), Body: body}
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return result, nil
		}
		httpErr := &HTTPError{StatusCode: resp.StatusCode, Status: http.StatusText(resp.StatusCode), URL: url}
		lastErr = httpErr
		if attempt < i.config.MaxAttempts && retryableStatus(resp.StatusCode) {
			if err := i.sleep(ctx, i.backoff(attempt, resp.Header.Get("Retry-After"))); err != nil {
				return responseData{}, err
			}
			continue
		}
		return result, httpErr
	}
	return responseData{}, lastErr
}

func retryableStatus(status int) bool {
	return status == http.StatusRequestTimeout || status == http.StatusTooManyRequests ||
		status == http.StatusInternalServerError || status == http.StatusBadGateway ||
		status == http.StatusServiceUnavailable || status == http.StatusGatewayTimeout
}

func retryableNetworkError(err error) bool {
	return !errors.Is(err, context.Canceled) && !errors.Is(err, context.DeadlineExceeded)
}

func (i *Inspector) backoff(attempt int, retryAfter string) time.Duration {
	if seconds, err := strconv.Atoi(strings.TrimSpace(retryAfter)); err == nil && seconds >= 0 {
		return time.Duration(seconds) * time.Second
	}
	if when, err := http.ParseTime(retryAfter); err == nil {
		if duration := time.Until(when); duration > 0 {
			return duration
		}
	}
	duration := i.config.BaseBackoff << (attempt - 1)
	if duration > i.config.MaxBackoff {
		return i.config.MaxBackoff
	}
	return duration
}

func defaultSleep(ctx context.Context, duration time.Duration) error {
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
