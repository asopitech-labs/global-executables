package main

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
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

func TestSourceDefaultsKeepDatabasesAndCompatibilityViewsIsolated(t *testing.T) {
	tests := []struct {
		source, catalog, database, output string
	}{
		{"go", "data/production/go-modules.txt", "data/production/go-crawl.db", "data/production/intermediate/go.jsonl"},
		{"npm", "data/production/npm-critical-packages.txt", "data/production/npm-crawl.db", "data/production/intermediate/npm.jsonl"},
		{"pypi", "data/production/pypi-projects.txt", "data/production/pypi-crawl.db", "data/production/intermediate/pypi.jsonl"},
		{"rubygems", "data/production/rubygems-names.txt", "data/production/rubygems-crawl.db", "data/production/intermediate/rubygems.jsonl"},
		{"packagist", "data/production/packagist-packages.txt", "data/production/packagist-crawl.db", "data/production/intermediate/packagist.jsonl"},
	}
	for _, test := range tests {
		t.Run(test.source, func(t *testing.T) {
			config := crawlConfig{Source: test.source}
			if err := config.applyDefaults(); err != nil {
				t.Fatal(err)
			}
			if config.CatalogPath != test.catalog || config.DatabasePath != test.database || config.ObservationsPath != test.output {
				t.Fatalf("config=%+v", config)
			}
		})
	}
}

func TestExecutePassRunsNPMWithBoundedPacingAndExportsCompatibility(t *testing.T) {
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
		time.Sleep(20 * time.Millisecond)
		name := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/"), "/latest")
		_, _ = fmt.Fprintf(w, `{"name":%q,"version":"1.0.0","bin":{%q:"cli.js"}}`, name, name)
	}))
	defer server.Close()

	directory := t.TempDir()
	config := crawlConfig{
		Source: "npm", StatePath: filepath.Join(directory, "registry-state.json"),
		ObservationsPath: filepath.Join(directory, "npm.jsonl"), ReportPath: filepath.Join(directory, "report.json"),
		CatalogPath: filepath.Join(directory, "npm-packages.txt"), DatabasePath: filepath.Join(directory, "npm-crawl.db"),
		RegistryURL: server.URL, PackageBudget: 32, ByteBudget: 1 << 20, Workers: 8,
		MaxInFlight: 16, CommitBatch: 4, RequestTimeout: time.Second, ModuleTimeout: 10 * time.Second,
	}
	var catalog strings.Builder
	for index := range 32 {
		_, _ = fmt.Fprintf(&catalog, "package-%02d\n", index)
	}
	if err := os.WriteFile(config.CatalogPath, []byte(catalog.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(config.StatePath, []byte(`{"version":1,"sources":{"npm":{"cursor":0,"packages_file":"npm-packages.txt","parser_generation":3}}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	report, err := executePass(context.Background(), config)
	if err != nil {
		t.Fatal(err)
	}
	if report.Processed != 32 || report.Records != 32 || !report.Complete || maximum.Load() > 2 {
		t.Fatalf("report=%+v maximum=%d", report, maximum.Load())
	}
	stateBody, _ := os.ReadFile(config.StatePath)
	if !bytes.Contains(stateBody, []byte(`"npm"`)) || !bytes.Contains(stateBody, []byte(`"cursor": 32`)) {
		t.Fatalf("state=%s", stateBody)
	}
	if bytes.Contains(stateBody, []byte(`"go"`)) {
		t.Fatalf("Go source leaked into npm state: %s", stateBody)
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

func TestBuildPassWorksPrioritizesCatalogAndBoundsRetries(t *testing.T) {
	directory := t.TempDir()
	catalog := filepath.Join(directory, "go-modules.txt")
	if err := os.WriteFile(catalog, []byte("new-a\nnew-b\nnew-c\nnew-d\nnew-e\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	before := gocrawl.Snapshot{ImportSnapshot: gocrawl.ImportSnapshot{
		CatalogSize: 5,
		Retries: map[string]gocrawl.RetryEntry{
			"retry-a": {Attempts: 1},
			"retry-b": {Attempts: 2},
		},
	}}

	works, err := buildPassWorks(catalog, before, 4)
	if err != nil {
		t.Fatal(err)
	}
	if len(works) != 4 || works[0].Retry || works[1].Retry || works[2].Retry || !works[3].Retry {
		t.Fatalf("works=%+v", works)
	}
	if works[0].Module != "new-a" || works[3].Module != "retry-a" {
		t.Fatalf("works=%+v", works)
	}
}
