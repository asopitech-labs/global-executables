package registryinspect

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestPackagistInspectorProducesComposerBinObservations(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.EscapedPath() != "/p2/demo/tool.json" {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write([]byte(`{"packages":{"demo/tool":[{"version":"1.2.3","bin":["bin/demo","tools/admin"],"source":{"url":"https://example.test/demo/tool"}}]}}`))
	}))
	defer server.Close()

	result := NewPackagistInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "demo/tool"},
	)
	if result.Verdict != gocrawl.VerdictSuccess || len(result.Observations) != 2 {
		t.Fatalf("result=%+v", result)
	}
	if result.Observations[0].Command != "admin" || result.Observations[1].Command != "demo" {
		t.Fatalf("observations=%+v", result.Observations)
	}
	if result.Observations[0].Registry != "packagist" || result.Observations[0].Language != "php" {
		t.Fatalf("observation=%+v", result.Observations[0])
	}
}

func TestPackagistInspectorSupportsStringBinAndHomepage(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"packages":{"demo/tool":[{"version":"dev-main","bin":"bin/demo","homepage":"https://example.test/home"}],"demo/empty":[{"version":"1.0.0"}]}}`))
	}))
	defer server.Close()

	result := NewPackagistInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "demo/tool"},
	)
	if result.Verdict != gocrawl.VerdictSuccess || len(result.Observations) != 1 ||
		result.Observations[0].Command != "demo" || result.Observations[0].Repository == nil ||
		*result.Observations[0].Repository != "https://example.test/home" {
		t.Fatalf("result=%+v", result)
	}
	empty := NewPackagistInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "demo/empty"},
	)
	if empty.Verdict != gocrawl.VerdictSuccess || len(empty.Observations) != 0 {
		t.Fatalf("missing bin result=%+v", empty)
	}
}

func TestPackagistInspectorClassifiesBadInputs(t *testing.T) {
	tests := []struct {
		name    string
		handler http.Handler
		verdict gocrawl.Verdict
	}{
		{"malformed", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(`{"packages":`)) }), gocrawl.VerdictRetry},
		{"withdrawn", http.NotFoundHandler(), gocrawl.VerdictPermanent},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(test.handler)
			defer server.Close()
			result := NewPackagistInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
				context.Background(), gocrawl.ModuleWork{Module: "demo/tool"},
			)
			if result.Verdict != test.verdict {
				t.Fatalf("result=%+v", result)
			}
		})
	}
}

func TestPackagistInspectorUsesSharedRequestPolicies(t *testing.T) {
	t.Run("retry after", func(t *testing.T) {
		requests := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			requests++
			if requests == 1 {
				w.Header().Set("Retry-After", "0")
				http.Error(w, "slow down", http.StatusTooManyRequests)
				return
			}
			_, _ = w.Write([]byte(`{"packages":{"demo/tool":[{"version":"1.0.0"}]}}`))
		}))
		defer server.Close()
		inspector := NewPackagistInspector(Config{
			BaseURL: server.URL, RequestTimeout: time.Second, MaxAttempts: 2,
			Sleep: func(context.Context, time.Duration) error { return nil },
		})
		result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo/tool"})
		if result.Verdict != gocrawl.VerdictSuccess || requests != 2 || inspector.Metrics().RateLimited != 1 {
			t.Fatalf("result=%+v requests=%d metrics=%+v", result, requests, inspector.Metrics())
		}
	})

	t.Run("timeout", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
		defer server.Close()
		result := NewPackagistInspector(Config{
			BaseURL: server.URL, RequestTimeout: time.Millisecond, PackageTimeout: time.Second, MaxAttempts: 1,
		}).Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo/tool"})
		if result.Verdict != gocrawl.VerdictRetry || !result.UncountedRetry {
			t.Fatalf("result=%+v", result)
		}
	})

	t.Run("disconnect", func(t *testing.T) {
		client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return nil, &netError{errors.New("connection reset")}
		})}
		result := NewPackagistInspector(Config{BaseURL: "https://example.test", Client: client, MaxAttempts: 1}).Inspect(
			context.Background(), gocrawl.ModuleWork{Module: "demo/tool"},
		)
		if result.Verdict != gocrawl.VerdictRetry || !result.UncountedRetry {
			t.Fatalf("result=%+v", result)
		}
	})

	t.Run("cancellation", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		result := NewPackagistInspector(Config{BaseURL: "https://example.test", MaxAttempts: 1}).Inspect(
			ctx, gocrawl.ModuleWork{Module: "demo/tool"},
		)
		if result.Verdict != gocrawl.VerdictCanceled {
			t.Fatalf("result=%+v", result)
		}
	})

	t.Run("response limit", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"packages":{"demo/tool":[]}}`))
		}))
		defer server.Close()
		result := NewPackagistInspector(Config{
			BaseURL: server.URL, RequestTimeout: time.Second, MaxAttempts: 1, MaxMetadataBytes: 8,
		}).Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo/tool"})
		if result.Verdict != gocrawl.VerdictRetry || result.UncountedRetry {
			t.Fatalf("result=%+v", result)
		}
	})
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

type netError struct{ error }

func (netError) Timeout() bool   { return false }
func (netError) Temporary() bool { return true }
