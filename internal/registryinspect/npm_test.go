package registryinspect

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestNPMInspectorProducesCompatibilityObservation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.EscapedPath() != "/@scope%2Fdemo/latest" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"name":"@scope/demo","version":"1.2.3","repository":{"url":"git+https://example.test/demo.git"},"bin":{"@scope/demo":"cli.js","../nested":"nested.js"}}`))
	}))
	defer server.Close()

	inspector := NewNPMInspector(Config{
		BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second,
	})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "@scope/demo"})

	if result.Verdict != gocrawl.VerdictSuccess {
		t.Fatalf("result=%+v", result)
	}
	if len(result.Observations) != 2 {
		t.Fatalf("observations=%+v", result.Observations)
	}
	if result.Observations[0].Command != "demo" || result.Observations[1].Command != "nested" {
		t.Fatalf("observations=%+v", result.Observations)
	}
	for _, observation := range result.Observations {
		if observation.Ecosystem != "npm" || observation.Registry != "npm" ||
			observation.Language != "javascript" || observation.Version != "1.2.3" {
			t.Fatalf("observation=%+v", observation)
		}
	}
}

func TestNPMInspectorClassifiesWithdrawnPackageAsPermanent(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()
	inspector := NewNPMInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second})

	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "withdrawn"})

	if result.Verdict != gocrawl.VerdictPermanent {
		t.Fatalf("result=%+v", result)
	}
}

func TestNPMInspectorRetriesMalformedMetadata(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"name":`))
	}))
	defer server.Close()
	inspector := NewNPMInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second})

	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "malformed"})

	if result.Verdict != gocrawl.VerdictRetry || result.Error == "" {
		t.Fatalf("result=%+v", result)
	}
}
