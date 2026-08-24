package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestRunReportsBootstrapVersion(t *testing.T) {
	var stdout, stderr bytes.Buffer

	if code := run([]string{"--version"}, &stdout, &stderr); code != 0 {
		t.Fatalf("run code = %d, want 0; stderr = %q", code, stderr.String())
	}
	if got := strings.TrimSpace(stdout.String()); got != "go-registry-crawler dev" {
		t.Fatalf("version = %q", got)
	}
	if stderr.Len() != 0 {
		t.Fatalf("stderr = %q", stderr.String())
	}
}

func TestRunRequiresExplicitCrawlSubcommand(t *testing.T) {
	var stdout, stderr bytes.Buffer

	if code := run(nil, &stdout, &stderr); code != 2 {
		t.Fatalf("run code = %d, want 2; stderr = %q", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "production routing is disabled") {
		t.Fatalf("stderr = %q", stderr.String())
	}
	if stdout.Len() != 0 {
		t.Fatalf("stdout = %q", stdout.String())
	}
}

func TestExecutePassExportsProgressAndResumesIdempotently(t *testing.T) {
	var archive bytes.Buffer
	writer := zip.NewWriter(&archive)
	entry, err := writer.Create("example.com/demo@v1.0.0/cmd/demo/main.go")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = entry.Write([]byte("package main\nfunc main() {}\n"))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/index":
			// An empty short page means the catalog is current at this instant.
		case strings.HasSuffix(r.URL.Path, "/@latest"):
			_, _ = w.Write([]byte(`{"Version":"v1.0.0"}`))
		case r.Method == http.MethodHead:
			w.Header().Set("Content-Length", strconv.Itoa(archive.Len()))
		case strings.HasSuffix(r.URL.Path, ".zip"):
			_, _ = w.Write(archive.Bytes())
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	directory := t.TempDir()
	config := crawlConfig{
		StatePath:        filepath.Join(directory, "registry-state.json"),
		ObservationsPath: filepath.Join(directory, "go.jsonl"),
		ReportPath:       filepath.Join(directory, "report.json"),
		CatalogPath:      filepath.Join(directory, "go-modules.txt"),
		DatabasePath:     filepath.Join(directory, "go-crawl.db"),
		ProxyURL:         server.URL,
		IndexURL:         server.URL + "/index",
		CatalogPages:     1,
		PackageBudget:    10,
		ByteBudget:       1 << 20,
		Workers:          4,
		MaxInFlight:      8,
		CommitBatch:      2,
		RequestTimeout:   time.Second,
		ModuleTimeout:    time.Second,
	}
	if err := os.WriteFile(config.CatalogPath, []byte("example.com/demo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(config.StatePath, []byte(`{"version":1,"sources":{"go":{"cursor":0,"catalog_complete":true,"modules_file":"data/production/go-modules.txt"}}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	first, err := executePass(context.Background(), config)
	if err != nil {
		t.Fatal(err)
	}
	if first.Processed != 1 || first.Records != 1 {
		t.Fatalf("first=%+v", first)
	}
	var state map[string]any
	stateBody, _ := os.ReadFile(config.StatePath)
	if err := json.Unmarshal(stateBody, &state); err != nil {
		t.Fatal(err)
	}
	goState := state["sources"].(map[string]any)["go"].(map[string]any)
	if goState["cursor"] != float64(1) {
		t.Fatalf("state=%s", stateBody)
	}
	rows, _ := os.ReadFile(config.ObservationsPath)
	if strings.Count(string(rows), "\n") != 1 || !bytes.Contains(rows, []byte(`"command":"demo"`)) {
		t.Fatalf("rows=%s", rows)
	}
	// The database is canonical after import. A torn compatibility export is rebuilt
	// from its read snapshot instead of being parsed back into durable state.
	if err := os.WriteFile(config.ObservationsPath, []byte("not-json\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	second, err := executePass(context.Background(), config)
	if err != nil {
		t.Fatal(err)
	}
	if second.Processed != 0 || second.Records != 0 {
		t.Fatalf("second=%+v", second)
	}
	rows, _ = os.ReadFile(config.ObservationsPath)
	if strings.Count(string(rows), "\n") != 1 {
		t.Fatalf("restart duplicated rows: %s", rows)
	}
}

func TestExecuteLoopRunsRequestedPassesAndStopsAtCompletion(t *testing.T) {
	var output bytes.Buffer
	calls := 0
	executor := func(context.Context, crawlConfig) (gocrawl.PassReport, error) {
		calls++
		return gocrawl.PassReport{Processed: 10, Complete: calls == 2}, nil
	}
	if err := executeLoop(context.Background(), crawlConfig{}, 0, 0, &output, executor); err != nil {
		t.Fatal(err)
	}
	if calls != 2 || strings.Count(output.String(), "processed=10") != 2 {
		t.Fatalf("calls=%d output=%q", calls, output.String())
	}
	if !strings.Contains(output.String(), "catalog is exhaustive") {
		t.Fatalf("output=%q", output.String())
	}
}
