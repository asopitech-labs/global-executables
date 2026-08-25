package registryinspect

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestPyPIInspectorReadsWheelCommandsWithRanges(t *testing.T) {
	var wheel bytes.Buffer
	writer := zip.NewWriter(&wheel)
	entry, err := writer.Create("demo-1.0.0.dist-info/entry_points.txt")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = entry.Write([]byte("[console_scripts]\ndemo = demo:main\n"))
	script, err := writer.Create("demo-1.0.0.data/scripts/demo-admin")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = script.Write([]byte("#!/bin/sh\n"))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}

	var fullDownloads atomic.Int64
	var headRequests atomic.Int64
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/pypi/demo/json":
			w.Header().Set("Content-Type", "application/json")
			_, _ = fmt.Fprintf(w, `{"info":{"name":"demo","version":"1.0.0","home_page":"https://example.test/demo"},"urls":[{"packagetype":"bdist_wheel","filename":"demo-1.0.0-py3-none-any.whl","size":%d,"url":%s}]}`,
				wheel.Len(), strconv.Quote(server.URL+"/files/demo.whl"))
		case "/files/demo.whl":
			if r.Method == http.MethodHead {
				headRequests.Add(1)
			}
			if r.Method == http.MethodGet && r.Header.Get("Range") == "" {
				fullDownloads.Add(1)
			}
			http.ServeContent(w, r, "demo.whl", time.Unix(0, 0), bytes.NewReader(wheel.Bytes()))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	inspector := NewPyPIInspector(Config{
		BaseURL: server.URL, RequestTimeout: 3 * time.Second, PackageTimeout: 10 * time.Second,
		RangeBlockSize: 128,
	})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo"})

	if result.Verdict != gocrawl.VerdictSuccess {
		t.Fatalf("result=%+v", result)
	}
	if got := commands(result.Observations); got != "demo,demo-admin" {
		t.Fatalf("commands=%s observations=%+v", got, result.Observations)
	}
	if fullDownloads.Load() != 0 {
		t.Fatalf("wheel was downloaded in full %d times", fullDownloads.Load())
	}
	if headRequests.Load() != 0 {
		t.Fatalf("metadata already supplied the size, but issued %d HEAD requests", headRequests.Load())
	}
}

func TestPyPIInspectorTreatsNoDistributionAsPermanent(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"info":{"name":"empty","version":"1.0.0"},"urls":[]}`))
	}))
	defer server.Close()
	inspector := NewPyPIInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second})

	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "empty"})

	if result.Verdict != gocrawl.VerdictPermanent || !strings.Contains(result.Error, "no wheel or sdist") {
		t.Fatalf("result=%+v", result)
	}
}

func TestPyPIInspectorReadsSdistProjectScripts(t *testing.T) {
	var artifact bytes.Buffer
	compressed := gzip.NewWriter(&artifact)
	archive := tar.NewWriter(compressed)
	body := []byte("[project]\nname = 'demo'\n[project.scripts]\ndemo = 'demo:main'\n")
	if err := archive.WriteHeader(&tar.Header{Name: "demo-1.0.0/pyproject.toml", Mode: 0o644, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	if _, err := archive.Write(body); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := compressed.Close(); err != nil {
		t.Fatal(err)
	}

	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/pypi/demo/json":
			_, _ = fmt.Fprintf(w, `{"info":{"name":"demo","version":"1.0.0"},"urls":[{"packagetype":"sdist","filename":"demo-1.0.0.tar.gz","url":%s}]}`,
				strconv.Quote(server.URL+"/files/demo.tar.gz"))
		case "/files/demo.tar.gz":
			_, _ = w.Write(artifact.Bytes())
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	inspector := NewPyPIInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "demo"})
	if result.Verdict != gocrawl.VerdictSuccess || commands(result.Observations) != "demo" {
		t.Fatalf("result=%+v", result)
	}
}

func TestSetupCFGIncludesOnlyConsoleScriptGroup(t *testing.T) {
	files := map[string][]byte{"demo/setup.cfg": []byte(`[options.entry_points]
console_scripts =
    demo = demo:main
gui_scripts =
    demo-window = demo:window
`)}
	if got := strings.Join(commandsFromMetadata(files), ","); got != "demo" {
		t.Fatalf("commands=%q", got)
	}
}

func commands(observations []gocrawl.Observation) string {
	values := make([]string, 0, len(observations))
	for _, observation := range observations {
		values = append(values, observation.Command)
	}
	return strings.Join(values, ",")
}
