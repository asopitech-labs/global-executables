package goproxy

import (
	"archive/zip"
	"bytes"
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

func moduleZIP(t *testing.T) []byte {
	t.Helper()
	var body bytes.Buffer
	writer := zip.NewWriter(&body)
	for name, content := range map[string]string{
		"example.com/demo@v1.0.0/cmd/demo/main.go":             "package main\nfunc main() {}\n",
		"example.com/demo@v1.0.0/internal/lib/lib.go":          "package lib\n",
		"example.com/demo@v1.0.0/vendor/tool/main.go":          "package main\n",
		"example.com/demo@v1.0.0/testdata/helper/main.go":      "package main\n",
		"example.com/demo@v1.0.0/cmd/demo/main_test.go":        "package main\n",
		"example.com/demo@v1.0.0/internal/lib/another_file.go": "package lib\n",
	} {
		entry, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := entry.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return body.Bytes()
}

func TestInspectorUsesRangeReaderAndFindsMainPackages(t *testing.T) {
	archive := moduleZIP(t)
	var ranges atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(len(archive)))
			w.Header().Set("Accept-Ranges", "bytes")
		case r.Header.Get("Range") != "":
			ranges.Add(1)
			var start, end int
			if _, err := fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &start, &end); err != nil {
				t.Fatalf("range: %v", err)
			}
			if end >= len(archive) {
				end = len(archive) - 1
			}
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, len(archive)))
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write(archive[start : end+1])
		default:
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL)
		}
	}))
	defer server.Close()

	inspector := NewInspector(Config{
		BaseURL:               server.URL,
		Client:                server.Client(),
		FullDownloadThreshold: 1,
		RangeBlockSize:        64,
		MaxAttempts:           1,
	})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{
		Order: 0, CatalogIndex: 0, Module: "example.com/demo", Attempt: 1,
	})

	if result.Verdict != gocrawl.VerdictSuccess || len(result.Observations) != 1 {
		t.Fatalf("result=%+v", result)
	}
	if result.Observations[0].Command != "demo" || result.Observations[0].LatestVersion != "v1.0.0" {
		t.Fatalf("observations=%+v", result.Observations)
	}
	if ranges.Load() == 0 || result.DownloadedBytes == 0 {
		t.Fatal("large archive did not use measured HTTP ranges")
	}
}

func TestInspectorUsesPackageIndexWithoutReadingArchive(t *testing.T) {
	var archiveRequests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case strings.HasPrefix(r.URL.Path, "/v1/packages/"):
			_, _ = w.Write([]byte(`{
				"modulePath":"example.com/demo",
				"version":"v1.0.0",
				"packages":{"items":[
					{"path":"example.com/demo/cmd/demo","name":"main"},
					{"path":"example.com/demo/examples/demo","name":"main"}
				],"total":2}
			}`))
		default:
			archiveRequests.Add(1)
			http.Error(w, "archive must not be read", http.StatusInternalServerError)
		}
	}))
	defer server.Close()

	result := NewInspector(Config{
		BaseURL: server.URL, PackageIndexURL: server.URL, Client: server.Client(), MaxAttempts: 1,
	}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "example.com/demo", Attempt: 1})

	if result.Verdict != gocrawl.VerdictSuccess || len(result.Observations) != 1 {
		t.Fatalf("result=%+v", result)
	}
	if result.Observations[0].Command != "demo" || archiveRequests.Load() != 0 {
		t.Fatalf("observations=%+v archiveRequests=%d", result.Observations, archiveRequests.Load())
	}
	if result.DownloadedBytes == 0 || !strings.Contains(result.Observations[0].Source, "/v1/packages/") {
		t.Fatalf("result=%+v", result)
	}
}

func TestInspectorFallsBackFromPackageIndexAndValidatesModulePath(t *testing.T) {
	archive := moduleZIP(t)
	var modRequests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case strings.HasPrefix(r.URL.Path, "/v1/packages/"):
			http.NotFound(w, r)
		case strings.HasSuffix(r.URL.Path, ".mod"):
			modRequests.Add(1)
			_, _ = w.Write([]byte("module example.com/demo\n"))
		case r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(len(archive)))
		case strings.HasSuffix(r.URL.Path, ".zip"):
			_, _ = w.Write(archive)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	result := NewInspector(Config{
		BaseURL: server.URL, PackageIndexURL: server.URL, Client: server.Client(), MaxAttempts: 1,
	}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "example.com/demo", Attempt: 1})

	if result.Verdict != gocrawl.VerdictSuccess || len(result.Observations) != 1 || modRequests.Load() != 1 {
		t.Fatalf("result=%+v modRequests=%d", result, modRequests.Load())
	}
}

func TestInspectorRejectsMismatchedModuleDeclarationBeforeArchive(t *testing.T) {
	var archiveRequests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case strings.HasPrefix(r.URL.Path, "/v1/packages/"):
			http.NotFound(w, r)
		case strings.HasSuffix(r.URL.Path, ".mod"):
			_, _ = w.Write([]byte("module example.com/canonical\n"))
		default:
			archiveRequests.Add(1)
			http.Error(w, "archive must not be read", http.StatusInternalServerError)
		}
	}))
	defer server.Close()

	result := NewInspector(Config{
		BaseURL: server.URL, PackageIndexURL: server.URL, Client: server.Client(), MaxAttempts: 1,
	}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "example.com/alias", Attempt: 1})

	if result.Verdict != gocrawl.VerdictPermanent || !strings.Contains(result.Error, "declares module") {
		t.Fatalf("result=%+v", result)
	}
	if archiveRequests.Load() != 0 {
		t.Fatalf("archiveRequests=%d", archiveRequests.Load())
	}
}

func TestInspectorPrefersCachedOnlyArchive(t *testing.T) {
	archive := moduleZIP(t)
	var regularArchiveRequests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case strings.HasSuffix(r.URL.Path, ".mod"):
			_, _ = w.Write([]byte("module example.com/demo\n"))
		case strings.HasPrefix(r.URL.Path, "/cached-only/") && r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(len(archive)))
		case strings.HasPrefix(r.URL.Path, "/cached-only/"):
			_, _ = w.Write(archive)
		case strings.HasSuffix(r.URL.Path, ".zip"):
			regularArchiveRequests.Add(1)
			http.Error(w, "regular archive must not be read", http.StatusInternalServerError)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	result := NewInspector(Config{
		BaseURL: server.URL, CachedOnlyURL: server.URL + "/cached-only",
		Client: server.Client(), MaxAttempts: 1,
	}).Inspect(t.Context(), gocrawl.ModuleWork{Module: "example.com/demo", Attempt: 1})

	if result.Verdict != gocrawl.VerdictSuccess || regularArchiveRequests.Load() != 0 {
		t.Fatalf("result=%+v regularArchiveRequests=%d", result, regularArchiveRequests.Load())
	}
}

func TestInspectorClassifiesMissingModuleAsPermanent(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()
	inspector := NewInspector(Config{BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "example.com/gone", Attempt: 1})
	if result.Verdict != gocrawl.VerdictPermanent {
		t.Fatalf("verdict=%s error=%s", result.Verdict, result.Error)
	}
}

func TestInspectorRetriesTransientHTTPResponses(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) == 1 {
			http.Error(w, "busy", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
	}))
	defer server.Close()
	inspector := NewInspector(Config{
		BaseURL:     server.URL,
		Client:      server.Client(),
		MaxAttempts: 2,
		Sleep:       func(context.Context, time.Duration) error { return nil },
	})
	_, err := inspector.latestVersion(context.Background(), "example.com/retry")
	if err != nil || calls.Load() != 2 {
		t.Fatalf("calls=%d err=%v", calls.Load(), err)
	}
}

func TestInspectorTreatsAttemptDeadlineAsRetryNotPassCancellation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()
	inspector := NewInspector(Config{
		BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1,
		RequestTimeout: 10 * time.Millisecond,
	})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "example.com/slow", Attempt: 1})
	if result.Verdict != gocrawl.VerdictRetry || !result.UncountedRetry {
		t.Fatalf("verdict=%s error=%s", result.Verdict, result.Error)
	}
}

func TestInspectorBoundsWholeModuleAndQueuesRetry(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/@latest") {
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
			return
		}
		<-r.Context().Done()
	}))
	defer server.Close()
	inspector := NewInspector(Config{
		BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1,
		RequestTimeout: time.Second, ModuleTimeout: 20 * time.Millisecond,
	})
	started := time.Now()
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "example.com/large", Attempt: 1})
	if result.Verdict != gocrawl.VerdictRetry || !result.UncountedRetry {
		t.Fatalf("verdict=%s error=%s", result.Verdict, result.Error)
	}
	if time.Since(started) > 500*time.Millisecond {
		t.Fatalf("module timeout was not enforced: %s", time.Since(started))
	}
}

func TestInspectorPropagatesParentCancellation(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	}))
	defer server.Close()
	inspector := NewInspector(Config{
		BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1,
		RequestTimeout: time.Second, ModuleTimeout: time.Second,
	})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	result := inspector.Inspect(ctx, gocrawl.ModuleWork{Module: "example.com/canceled", Attempt: 1})
	if result.Verdict != gocrawl.VerdictCanceled {
		t.Fatalf("verdict=%s error=%s", result.Verdict, result.Error)
	}
}

func TestRangeReaderBoundsItsBlockCache(t *testing.T) {
	body := bytes.Repeat([]byte("x"), 1024)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var start, end int
		if _, err := fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &start, &end); err != nil {
			t.Fatal(err)
		}
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(body[start : end+1])
	}))
	defer server.Close()
	inspector := NewInspector(Config{BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1})
	reader := newHTTPRangeReaderAt(context.Background(), inspector, server.URL, int64(len(body)), 64, 2)
	buffer := make([]byte, 1)
	for offset := int64(0); offset < int64(len(body)); offset += 64 {
		if _, err := reader.ReadAt(buffer, offset); err != nil {
			t.Fatal(err)
		}
	}
	if len(reader.blocks) > 2 {
		t.Fatalf("cached %d blocks, want at most 2", len(reader.blocks))
	}
}

func TestInspectorQueuesCorruptArchiveForRetry(t *testing.T) {
	body := []byte("not-a-zip")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		default:
			_, _ = w.Write(body)
		}
	}))
	defer server.Close()
	inspector := NewInspector(Config{BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1})
	result := inspector.Inspect(context.Background(), gocrawl.ModuleWork{Module: "example.com/corrupt", Attempt: 1})
	if result.Verdict != gocrawl.VerdictRetry || result.Error == "" {
		t.Fatalf("result=%+v", result)
	}
}

func TestInspectorDeduplicatesCommandNamesAcrossDirectories(t *testing.T) {
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	for _, name := range []string{
		"example.com/demo@v1.0.0/cmd/tool/main.go",
		"example.com/demo@v1.0.0/examples/tool/main.go",
	} {
		entry, err := writer.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = entry.Write([]byte("package main\n"))
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(archive.Len()))
		default:
			_, _ = w.Write(archive.Bytes())
		}
	}))
	defer server.Close()
	result := NewInspector(Config{BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1}).Inspect(
		context.Background(), gocrawl.ModuleWork{Module: "example.com/demo", Attempt: 1},
	)
	if result.Verdict != gocrawl.VerdictSuccess || len(result.Observations) != 1 {
		t.Fatalf("result=%+v", result)
	}
}

func TestRangeReaderSupportsZIP64CentralDirectory(t *testing.T) {
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	for index := range 65_536 {
		name := fmt.Sprintf("example.com/zip64@v1.0.0/files/%05d.txt", index)
		if _, err := writer.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Store}); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var start, end int
		if _, err := fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &start, &end); err != nil {
			t.Fatal(err)
		}
		if end >= archive.Len() {
			end = archive.Len() - 1
		}
		w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", start, end, archive.Len()))
		w.WriteHeader(http.StatusPartialContent)
		_, _ = w.Write(archive.Bytes()[start : end+1])
	}))
	defer server.Close()
	inspector := NewInspector(Config{BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1})
	reader := newHTTPRangeReaderAt(
		context.Background(), inspector, server.URL, int64(archive.Len()), 512*1024, 8,
	)
	parsed, err := zip.NewReader(reader, int64(archive.Len()))
	if err != nil {
		t.Fatal(err)
	}
	if len(parsed.File) != 65_536 {
		t.Fatalf("files=%d", len(parsed.File))
	}
}

func TestInspectorAccountsForRangeProbeBeforeFullDownloadFallback(t *testing.T) {
	archive := moduleZIP(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(len(archive)))
		case r.Header.Get("Range") != "":
			var start, end int
			_, _ = fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-%d", &start, &end)
			if end >= len(archive) {
				end = len(archive) - 1
			}
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write(archive[start : end+1])
		default:
			_, _ = w.Write(archive)
		}
	}))
	defer server.Close()
	result := NewInspector(Config{
		BaseURL: server.URL, Client: server.Client(), MaxAttempts: 1,
		FullDownloadThreshold: 1, MaxDirectoryProbes: 1,
	}).Inspect(context.Background(), gocrawl.ModuleWork{Module: "example.com/demo", Attempt: 1})
	if result.Verdict != gocrawl.VerdictSuccess {
		t.Fatalf("result=%+v", result)
	}
	if result.DownloadedBytes <= int64(len(archive)) {
		t.Fatalf("downloaded=%d archive=%d", result.DownloadedBytes, len(archive))
	}
}
