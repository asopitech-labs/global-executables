package gocrawl

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestCompatibilityImportAndExportPreserveOtherSources(t *testing.T) {
	directory := t.TempDir()
	catalogPath := filepath.Join(directory, "go-modules.txt")
	statePath := filepath.Join(directory, "registry-state.json")
	observationsPath := filepath.Join(directory, "go.jsonl")
	reportPath := filepath.Join(directory, "report.json")
	if err := os.WriteFile(catalogPath, []byte("example.com/a\nexample.com/b\nexample.com/c\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stateBody := `{
  "version": 1,
  "sources": {
    "pypi": {"cursor": 99, "sentinel": true},
    "go": {
      "cursor": 1,
      "catalog_complete": true,
      "catalog_since": "2026-08-21T03:30:55Z",
      "modules_file": "data/production/go-modules.txt",
      "failures": {"example.com/retry": "HTTP 503"},
      "failure_attempts": {"example.com/retry": 2},
      "retry_modules": ["example.com/retry"],
      "unavailable": {"example.com/gone": "HTTP 404"}
    }
  }
}`
	if err := os.WriteFile(statePath, []byte(stateBody), 0o644); err != nil {
		t.Fatal(err)
	}
	row := Observation{Command: "old", Confidence: "direct", Ecosystem: "go", Language: "go", LatestVersion: "v1.0.0", Package: "example.com/a", Registry: "go", Source: "old.zip", SourceType: "language_package", Version: "v1.0.0"}
	rowBody, _ := json.Marshal(row)
	if err := os.WriteFile(observationsPath, append(rowBody, '\n'), 0o644); err != nil {
		t.Fatal(err)
	}

	imported, document, err := LoadCompatibility(statePath, observationsPath, catalogPath)
	if err != nil {
		t.Fatal(err)
	}
	if imported.Cursor != 1 || imported.CatalogSize != 3 || imported.CatalogOffset != int64(len("example.com/a\n")) {
		t.Fatalf("imported=%+v", imported)
	}
	if imported.Retries["example.com/retry"].Attempts != 2 || len(imported.Observations) != 1 {
		t.Fatalf("imported=%+v", imported)
	}

	store, err := OpenBoltStore(filepath.Join(directory, "crawl.db"), StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), imported); err != nil {
		t.Fatal(err)
	}
	if err := store.Commit(context.Background(), []ModuleResult{{
		Work:         ModuleWork{CatalogIndex: 1, CatalogOffset: int64(len("example.com/a\nexample.com/b\n")), Module: "example.com/b", Attempt: 1},
		Verdict:      VerdictSuccess,
		Observations: []Observation{{Command: "b", Confidence: "direct", Ecosystem: "go", Language: "go", LatestVersion: "v1.0.0", Package: "example.com/b", Registry: "go", Source: "b.zip", SourceType: "language_package", Version: "v1.0.0"}},
	}}); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Progress(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	report := PassReport{StartedAt: time.Unix(100, 0).UTC(), FinishedAt: time.Unix(160, 0).UTC(), Processed: 1, Records: 1}
	if err := ExportStoreCompatibility(context.Background(), ExportPaths{State: statePath, Observations: observationsPath, Report: reportPath}, document, store, report); err != nil {
		t.Fatal(err)
	}

	var exported map[string]any
	stateBytes, _ := os.ReadFile(statePath)
	if err := json.Unmarshal(stateBytes, &exported); err != nil {
		t.Fatal(err)
	}
	sources := exported["sources"].(map[string]any)
	if sources["pypi"].(map[string]any)["cursor"] != float64(99) {
		t.Fatalf("other source changed: %s", stateBytes)
	}
	goState := sources["go"].(map[string]any)
	if goState["cursor"] != float64(2) || goState["catalog_size"] != float64(3) ||
		goState["snapshot_generation"] != float64(snapshot.Generation) {
		t.Fatalf("go state=%v", goState)
	}
	rows, _ := os.ReadFile(observationsPath)
	if got := len(bytesLines(rows)); got != 2 {
		t.Fatalf("observation rows=%d: %s", got, rows)
	}
	var exportedReport map[string]any
	reportBytes, _ := os.ReadFile(reportPath)
	if err := json.Unmarshal(reportBytes, &exportedReport); err != nil {
		t.Fatal(err)
	}
	if exportedReport["snapshot_generation"] != float64(snapshot.Generation) {
		t.Fatalf("report=%s", reportBytes)
	}
}

func bytesLines(body []byte) [][]byte {
	var lines [][]byte
	for _, line := range bytes.Split(body, []byte{'\n'}) {
		if len(line) > 0 {
			lines = append(lines, line)
		}
	}
	return lines
}
