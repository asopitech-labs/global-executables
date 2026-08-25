package registryinspect

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestRequesterReducesHostConcurrencyAfterRateLimits(t *testing.T) {
	var active atomic.Int64
	var maximum atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := active.Add(1)
		defer active.Add(-1)
		for {
			previous := maximum.Load()
			if current <= previous || maximum.CompareAndSwap(previous, current) {
				break
			}
		}
		if current > 2 {
			w.Header().Set("Retry-After", "0")
			http.Error(w, "slow down", http.StatusTooManyRequests)
			return
		}
		time.Sleep(time.Millisecond)
		name := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/"), "/latest")
		_, _ = fmt.Fprintf(w, `{"name":%q,"version":"1.0.0","bin":{%q:"cli.js"}}`, name, name)
	}))
	defer server.Close()

	inspector := NewNPMInspector(Config{
		BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second,
		MaxAttempts: 4, InitialHostConcurrency: 16, MaxHostConcurrency: 16,
		Sleep:  func(context.Context, time.Duration) error { return nil },
		Jitter: func(duration time.Duration) time.Duration { return duration },
	})
	works := make([]gocrawl.ModuleWork, 40)
	for index := range works {
		works[index] = gocrawl.ModuleWork{Order: uint64(index), CatalogIndex: uint64(index), Module: fmt.Sprintf("package-%d", index)}
	}
	committer := &memoryCommitter{}
	if err := (gocrawl.Coordinator{Workers: 16, MaxInFlight: 32, CommitBatch: 4}).Run(context.Background(), works, inspector, committer); err != nil {
		t.Fatal(err)
	}
	metrics := inspector.Metrics()
	if metrics.RateLimited == 0 || metrics.HostConcurrency >= 16 {
		t.Fatalf("metrics=%+v maximum=%d", metrics, maximum.Load())
	}
	if len(committer.results) != len(works) {
		t.Fatalf("committed=%d want=%d", len(committer.results), len(works))
	}
}

func TestAdvertisedRetryAfterIsNotClippedByNormalBackoff(t *testing.T) {
	requester := newRequester(Config{MaxBackoff: time.Second, MaxRetryAfter: 5 * time.Minute})
	if got := requester.retryAfter("122"); got != 122*time.Second {
		t.Fatalf("retry after=%s", got)
	}
	gate := newAdaptiveGate(8, 8, 8, time.Second, 0, &requester.metrics)
	if err := gate.acquire(context.Background()); err != nil {
		t.Fatal(err)
	}
	gate.release(http.StatusTooManyRequests, nil, 100*time.Millisecond)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := gate.acquire(ctx); err == nil {
		t.Fatal("host gate ignored advertised retry window")
	}
}

func TestAdaptiveGatePacesRequestsWithoutBursting(t *testing.T) {
	requester := newRequester(Config{})
	gate := newAdaptiveGate(8, 8, 8, time.Second, 10*time.Millisecond, &requester.metrics)
	started := time.Now()
	for range 4 {
		if err := gate.acquire(context.Background()); err != nil {
			t.Fatal(err)
		}
		gate.release(http.StatusOK, nil, 0)
	}
	if elapsed := time.Since(started); elapsed < 25*time.Millisecond {
		t.Fatalf("requests burst in %s", elapsed)
	}
}

func TestRequestTimeoutStartsAfterHostGate(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))
	defer server.Close()
	requester := newRequester(Config{
		RequestTimeout: 500 * time.Millisecond, PackageTimeout: 3 * time.Second,
		MaxAttempts: 1, InitialHostConcurrency: 1, MaxHostConcurrency: 1,
		MinRequestInterval: time.Second,
	})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for range 2 {
		if _, err := requester.request(ctx, http.MethodGet, server.URL, nil, 16); err != nil {
			t.Fatalf("host pacing consumed the HTTP-attempt timeout: %v", err)
		}
	}
}

func TestRequesterReusesConnections(t *testing.T) {
	var connections atomic.Int64
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))
	server.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			connections.Add(1)
		}
	}
	server.Start()
	defer server.Close()

	requester := newRequester(Config{RequestTimeout: time.Second, MaxAttempts: 1})
	for range 2 {
		if _, err := requester.request(context.Background(), http.MethodGet, server.URL, nil, 16); err != nil {
			t.Fatal(err)
		}
	}
	if connections.Load() != 1 {
		t.Fatalf("opened %d connections for two sequential requests", connections.Load())
	}
	if metrics := requester.metricsSnapshot(); metrics.Requests != 2 {
		t.Fatalf("metrics=%+v", metrics)
	}
}

func TestRequesterEnforcesResponseLimit(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("response-is-too-large"))
	}))
	defer server.Close()
	requester := newRequester(Config{RequestTimeout: time.Second, MaxAttempts: 1})
	if _, err := requester.request(context.Background(), http.MethodGet, server.URL, nil, 8); err == nil ||
		!strings.Contains(err.Error(), "response exceeded 8 bytes") {
		t.Fatalf("limit error=%v", err)
	}
}

func TestClassifyMarksOnlyNetworkRetriesUncounted(t *testing.T) {
	network := classifyResult(context.Background(), gocrawl.ModuleResult{}, &net.DNSError{Err: "temporary", Name: "example.test", IsTemporary: true})
	if network.Verdict != gocrawl.VerdictRetry || !network.UncountedRetry {
		t.Fatalf("network result=%+v", network)
	}
	content := classifyResult(context.Background(), gocrawl.ModuleResult{}, fmt.Errorf("invalid metadata"))
	if content.Verdict != gocrawl.VerdictRetry || content.UncountedRetry {
		t.Fatalf("content result=%+v", content)
	}
	truncated := classifyResult(context.Background(), gocrawl.ModuleResult{}, io.ErrUnexpectedEOF)
	if truncated.Verdict != gocrawl.VerdictRetry || truncated.UncountedRetry {
		t.Fatalf("parser result=%+v", truncated)
	}
}

type memoryCommitter struct{ results []gocrawl.ModuleResult }

func (m *memoryCommitter) Commit(_ context.Context, results []gocrawl.ModuleResult) error {
	m.results = append(m.results, results...)
	return nil
}
