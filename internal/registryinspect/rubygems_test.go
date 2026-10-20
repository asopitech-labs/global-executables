package registryinspect

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestRubyGemsInspectorReadsExecutablesFromGemHead(t *testing.T) {
	metadata := []byte("---\nexecutables:\n- ../bin/demo\n- demo-admin\n")
	var compressed bytes.Buffer
	gzipWriter := gzip.NewWriter(&compressed)
	_, _ = gzipWriter.Write(metadata)
	_ = gzipWriter.Close()
	var gem bytes.Buffer
	tarWriter := tar.NewWriter(&gem)
	_ = tarWriter.WriteHeader(&tar.Header{Name: "metadata.gz", Size: int64(compressed.Len()), Mode: 0o644})
	_, _ = tarWriter.Write(compressed.Bytes())
	_ = tarWriter.Close()

	rangeRequested := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/gems/demo.json":
			_, _ = fmt.Fprintf(w, `{"name":"demo","version":"1.2.3","gem_uri":%q,"source_code_uri":"https://example.test/demo"}`, "http://"+r.Host+"/gems/demo-1.2.3.gem")
		case "/gems/demo-1.2.3.gem":
			rangeRequested = r.Header.Get("Range") == "bytes=0-65535"
			w.Header().Set("Content-Range", fmt.Sprintf("bytes 0-%d/%d", gem.Len()-1, gem.Len()))
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write(gem.Bytes())
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	inspector := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo"})

	if result.Verdict != gocrawl.VerdictSuccess || !rangeRequested {
		t.Fatalf("result=%+v rangeRequested=%v", result, rangeRequested)
	}
	if result.DownloadedBytes != int64(gem.Len()) {
		t.Fatalf("downloaded=%d want=%d", result.DownloadedBytes, gem.Len())
	}
	if len(result.Observations) != 2 || result.Observations[0].Command != "demo" || result.Observations[1].Command != "demo-admin" {
		t.Fatalf("observations=%+v", result.Observations)
	}
	if result.Observations[0].Registry != "rubygems" || result.Observations[0].Version != "1.2.3" {
		t.Fatalf("observation=%+v", result.Observations[0])
	}
}

func TestRubyGemsInspectorTreatsYankedGemAsPermanent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"name":"gone","version":"1.0.0","yanked":true}`))
	}))
	defer server.Close()

	result := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "gone"},
	)
	if result.Verdict != gocrawl.VerdictPermanent {
		t.Fatalf("result=%+v", result)
	}
}

func TestRubyGemsInspectorFallsBackToFullGem(t *testing.T) {
	metadata := []byte("---\nexecutables: [demo, ../bin/admin]\n")
	var compressed bytes.Buffer
	gzipWriter := gzip.NewWriter(&compressed)
	_, _ = gzipWriter.Write(metadata)
	_ = gzipWriter.Close()
	var gem bytes.Buffer
	tarWriter := tar.NewWriter(&gem)
	_ = tarWriter.WriteHeader(&tar.Header{Name: "metadata.gz", Size: int64(compressed.Len()), Mode: 0o644})
	_, _ = tarWriter.Write(compressed.Bytes())
	_ = tarWriter.Close()

	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/gems/demo.json":
			_, _ = fmt.Fprintf(w, `{"name":"demo","version":"1.0.0","gem_uri":%q,"source_code_uri":"","homepage_uri":""}`, "http://"+r.Host+"/demo.gem")
		case "/api/v1/gems/missing.json":
			_, _ = fmt.Fprintf(w, `{"name":"missing","version":"1.0.0","gem_uri":%q}`, "http://"+r.Host+"/demo.gem")
		case "/demo.gem":
			requests++
			if r.Header.Get("Range") != "" {
				w.WriteHeader(http.StatusPartialContent)
				_, _ = w.Write([]byte("not-a-tar"))
				return
			}
			_, _ = w.Write(gem.Bytes())
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	result := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "demo"},
	)
	if result.Verdict != gocrawl.VerdictSuccess || requests != 2 || len(result.Observations) != 2 ||
		result.Observations[0].Repository == nil || *result.Observations[0].Repository != "" {
		t.Fatalf("result=%+v requests=%d", result, requests)
	}
	if result.DownloadedBytes != int64(gem.Len()) {
		t.Fatalf("downloaded=%d want=%d", result.DownloadedBytes, gem.Len())
	}
	missing := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "missing"},
	)
	if missing.Verdict != gocrawl.VerdictSuccess || len(missing.Observations) != 2 || missing.Observations[0].Repository != nil {
		t.Fatalf("missing repository result=%+v", missing)
	}
}

func TestRubyGemsInspectorRetriesMalformedGem(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, ".json") {
			_, _ = fmt.Fprintf(w, `{"name":"demo","version":"1.0.0","gem_uri":%q}`, "http://"+r.Host+"/demo.gem")
			return
		}
		_, _ = w.Write([]byte("not-a-gem"))
	}))
	defer server.Close()

	result := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "demo"},
	)
	if result.Verdict != gocrawl.VerdictRetry || result.Error == "" {
		t.Fatalf("result=%+v", result)
	}
}

func TestRubyGemsInspectorRetriesMalformedGemspec(t *testing.T) {
	var compressed bytes.Buffer
	gzipWriter := gzip.NewWriter(&compressed)
	_, _ = gzipWriter.Write([]byte("executables: broken\n"))
	_ = gzipWriter.Close()
	var gem bytes.Buffer
	tarWriter := tar.NewWriter(&gem)
	_ = tarWriter.WriteHeader(&tar.Header{Name: "metadata.gz", Size: int64(compressed.Len()), Mode: 0o644})
	_, _ = tarWriter.Write(compressed.Bytes())
	_ = tarWriter.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, ".json") {
			_, _ = fmt.Fprintf(w, `{"name":"demo","version":"1.0.0","gem_uri":%q}`, "http://"+r.Host+"/demo.gem")
			return
		}
		_, _ = w.Write(gem.Bytes())
	}))
	defer server.Close()
	result := NewRubyGemsInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, MaxAttempts: 1}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "demo"},
	)
	if result.Verdict != gocrawl.VerdictRetry || result.UncountedRetry {
		t.Fatalf("result=%+v", result)
	}
}

func TestRubyGemsParsersRejectMalformedMetadata(t *testing.T) {
	var gem bytes.Buffer
	writer := tar.NewWriter(&gem)
	_ = writer.WriteHeader(&tar.Header{Name: "metadata.gz", Size: 10, Mode: 0o644})
	_, _ = writer.Write([]byte("not-a-gzip"))
	_ = writer.Close()
	if _, err := gemMetadata(gem.Bytes(), 1024); err == nil {
		t.Fatal("invalid metadata.gz was accepted")
	}
	for _, body := range []string{"executables: [unterminated\n", "executables: broken\n"} {
		if _, err := gemspecCommands(body); err == nil {
			t.Fatalf("malformed executables list was accepted: %q", body)
		}
	}
}

func TestRubyGemsInspectorUsesSharedRequestPolicies(t *testing.T) {
	t.Run("retry after", func(t *testing.T) {
		requests := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			requests++
			if requests == 1 {
				w.Header().Set("Retry-After", "0")
				http.Error(w, "slow down", http.StatusTooManyRequests)
				return
			}
			_, _ = w.Write([]byte(`{"name":"gone","version":"1.0.0","yanked":true}`))
		}))
		defer server.Close()
		inspector := NewRubyGemsInspector(Config{
			BaseURL: server.URL, RequestTimeout: time.Second, MaxAttempts: 2,
			Sleep: func(context.Context, time.Duration) error { return nil },
		})
		result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "gone"})
		if result.Verdict != gocrawl.VerdictPermanent || requests != 2 || inspector.Metrics().RateLimited != 1 {
			t.Fatalf("result=%+v requests=%d metrics=%+v", result, requests, inspector.Metrics())
		}
	})

	t.Run("timeout", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) { <-r.Context().Done() }))
		defer server.Close()
		result := NewRubyGemsInspector(Config{
			BaseURL: server.URL, RequestTimeout: time.Millisecond, PackageTimeout: time.Second, MaxAttempts: 1,
		}).Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo"})
		if result.Verdict != gocrawl.VerdictRetry || !result.UncountedRetry {
			t.Fatalf("result=%+v", result)
		}
	})

	t.Run("disconnect", func(t *testing.T) {
		client := &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
			return nil, &netError{errors.New("connection reset")}
		})}
		result := NewRubyGemsInspector(Config{BaseURL: "https://example.test", Client: client, MaxAttempts: 1}).Inspect(
			context.Background(), gocrawl.ModuleWork{Module: "demo"},
		)
		if result.Verdict != gocrawl.VerdictRetry || !result.UncountedRetry {
			t.Fatalf("result=%+v", result)
		}
	})

	t.Run("cancellation", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		result := NewRubyGemsInspector(Config{BaseURL: "https://example.test", MaxAttempts: 1}).Inspect(
			ctx, gocrawl.ModuleWork{Module: "demo"},
		)
		if result.Verdict != gocrawl.VerdictCanceled {
			t.Fatalf("result=%+v", result)
		}
	})

	t.Run("response limit", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"name":"demo","version":"1.0.0"}`))
		}))
		defer server.Close()
		result := NewRubyGemsInspector(Config{
			BaseURL: server.URL, RequestTimeout: time.Second, MaxAttempts: 1, MaxMetadataBytes: 8,
		}).Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo"})
		if result.Verdict != gocrawl.VerdictRetry || result.UncountedRetry {
			t.Fatalf("result=%+v", result)
		}
	})
}
