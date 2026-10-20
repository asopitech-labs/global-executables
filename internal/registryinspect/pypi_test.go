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

func TestPyPIOversizedOrCorruptArtifactsAreFinalForTheVersionAndNeverDownloaded(t *testing.T) {
	var artifactRequests atomic.Int32
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/pypi/huge/json":
			_, _ = fmt.Fprintf(w, `{"info":{"name":"huge","version":"2.0"},"urls":[{"packagetype":"sdist","filename":"huge-2.0.tar.gz","size":%d,"url":%s}]}`,
				200<<20, strconv.Quote(server.URL+"/files/huge.tar.gz"))
		case "/pypi/broken/json":
			_, _ = fmt.Fprintf(w, `{"info":{"name":"broken","version":"1.1"},"urls":[{"packagetype":"sdist","filename":"broken-1.1.tar.gz","url":%s}]}`,
				strconv.Quote(server.URL+"/files/broken.tar.gz"))
		case "/files/broken.tar.gz":
			artifactRequests.Add(1)
			_, _ = w.Write(bytes.Repeat([]byte("not a tar header "), 64))
		default:
			artifactRequests.Add(1)
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	inspector := NewPyPIInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: time.Second})

	huge := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "huge"})
	if huge.Verdict != gocrawl.VerdictPermanent || huge.Outcome != gocrawl.Unreadable || huge.Latest != "2.0" ||
		!strings.Contains(huge.Error, "exceeds limit") {
		t.Fatalf("huge=%+v", huge)
	}
	if artifactRequests.Load() != 0 {
		t.Fatal("an artifact PyPI reports as oversized must not be requested")
	}
	broken := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "broken"})
	if broken.Verdict != gocrawl.VerdictPermanent || broken.Outcome != gocrawl.Unreadable || broken.Latest != "1.1" {
		t.Fatalf("broken=%+v", broken)
	}
	// The next visit of the same version reads the JSON only.
	before := artifactRequests.Load()
	again := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "broken", Known: "1.1"})
	if !again.Unchanged || artifactRequests.Load() != before {
		t.Fatalf("again=%+v, artifact requests %d -> %d", again, before, artifactRequests.Load())
	}
}

func TestPyPIProjectJSONOverTheLimitIsReadThroughTheReleaseDocument(t *testing.T) {
	wheel := wheelWithEntryPoints(t, "[console_scripts]\nbig = big:main\n")
	history := strings.Repeat(`"0.0.1":[{"filename":"x"}],`, 4000)
	var server *httptest.Server
	var projectRanges, projectFull, release atomic.Int32
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/pypi/big/json":
			document := `{"info":{"name":"big","version":"2.0","description":"` + strings.Repeat("d", 80<<10) +
				`"},"last_serial":1,"releases":{` + history + `"2.0":[]},"urls":[]}`
			if r.Header.Get("Range") != "" {
				projectRanges.Add(1)
			} else {
				projectFull.Add(1)
			}
			http.ServeContent(w, r, "big.json", time.Time{}, strings.NewReader(document))
		case "/pypi/big/2.0/json":
			release.Add(1)
			_, _ = fmt.Fprintf(w, `{"info":{"name":"big","version":"2.0"},"urls":[{"packagetype":"bdist_wheel","filename":"big-2.0-py3-none-any.whl","size":%d,"url":%s}]}`,
				len(wheel), strconv.Quote(server.URL+"/files/big.whl"))
		case "/files/big.whl":
			http.ServeContent(w, r, "big.whl", time.Time{}, bytes.NewReader(wheel))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	inspector := NewPyPIInspector(Config{BaseURL: server.URL, RequestTimeout: time.Second, PackageTimeout: 5 * time.Second,
		MaxMetadataBytes: 100 << 10})

	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "big"})
	if result.Verdict != gocrawl.VerdictSuccess || result.Latest != "2.0" || commands(result.Observations) != "big" {
		t.Fatalf("result=%+v", result)
	}
	if projectFull.Load() != 1 || projectRanges.Load() != 2 || release.Load() != 1 {
		t.Fatalf("project full=%d ranges=%d release=%d (want 1, 2: 64 KiB then 100 KiB, 1)", projectFull.Load(), projectRanges.Load(), release.Load())
	}
	// The same version again: the head of the project document only.
	projectRanges.Store(0)
	again := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "big", Known: "2.0"})
	if !again.Unchanged || release.Load() != 1 || projectRanges.Load() != 2 {
		t.Fatalf("again=%+v release=%d ranges=%d", again, release.Load(), projectRanges.Load())
	}
}

func TestLeadingInfoReadsAPrefixAndSaysWhenItIsTooShort(t *testing.T) {
	document := []byte(`{"info":{"name":"n","version":"1.2","description":"long"},"releases":{"1.2":[]}}`)
	info, complete, err := leadingInfo(document[:40])
	if err != nil || complete {
		t.Fatalf("a cut inside info: %+v %v %v", info, complete, err)
	}
	info, complete, err = leadingInfo(document[:62])
	if err != nil || !complete || info.Version != "1.2" {
		t.Fatalf("info complete, the rest cut: %+v %v %v", info, complete, err)
	}
	if _, _, err := leadingInfo([]byte(`[1,2]`)); err == nil {
		t.Fatal("not an object")
	}
}

func wheelWithEntryPoints(t *testing.T, entryPoints string) []byte {
	t.Helper()
	var wheel bytes.Buffer
	writer := zip.NewWriter(&wheel)
	entry, err := writer.Create("big-2.0.dist-info/entry_points.txt")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = entry.Write([]byte(entryPoints))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return wheel.Bytes()
}
