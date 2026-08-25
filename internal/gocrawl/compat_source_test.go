package gocrawl

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestLoadSourceCompatibilityImportsNPMPythonState(t *testing.T) {
	directory := t.TempDir()
	catalog := filepath.Join(directory, "npm-packages.txt")
	state := filepath.Join(directory, "registry-state.json")
	observations := filepath.Join(directory, "npm.jsonl")
	if err := os.WriteFile(catalog, []byte("alpha\nbeta\ngamma\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(state, []byte(`{"version":1,"sources":{"npm":{"cursor":2,"packages_file":"data/production/npm-packages.txt","failures":{"flaky":"timeout"},"failure_attempts":{"flaky":2},"retry_npm":["flaky"],"unavailable":{"gone":"HTTP 404"},"parser_generation":3}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(observations, []byte(`{"command":"alpha","confidence":"direct","ecosystem":"npm","language":"javascript","latest_version":"1.0.0","package":"alpha","registry":"npm","repository":null,"source":"fixture","source_type":"language_package","version":"1.0.0"}`+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	snapshot, _, err := LoadSourceCompatibility(state, observations, catalog, CompatibilityProfileFor("npm"))
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Cursor != 2 || snapshot.CatalogSize != 3 || !snapshot.CatalogComplete {
		t.Fatalf("snapshot=%+v", snapshot)
	}
	if snapshot.ModulesFile != "data/production/npm-packages.txt" || snapshot.Retries["flaky"].Attempts != 2 {
		t.Fatalf("snapshot=%+v", snapshot)
	}
}

func TestExportSourceCompatibilityPreservesOtherSourcesAndPythonKeys(t *testing.T) {
	directory := t.TempDir()
	paths := ExportPaths{
		State: filepath.Join(directory, "registry-state.json"), Observations: filepath.Join(directory, "npm.jsonl"),
		Report: filepath.Join(directory, "report.json"),
	}
	document := StateDocument{
		"version": json.RawMessage("1"),
		"sources": json.RawMessage(`{"crates":{"cursor":99},"npm":{"parser_generation":3,"catalog_bytes":123}}`),
	}
	snapshot := Snapshot{ImportSnapshot: ImportSnapshot{
		Cursor: 3, CatalogSize: 3, CatalogComplete: true, ModulesFile: "data/production/npm-packages.txt",
		Retries: map[string]RetryEntry{}, Unavailable: map[string]string{"gone": "HTTP 404"},
		Observations: []Observation{{Command: "demo", Confidence: "direct", Ecosystem: "npm", Language: "javascript", LatestVersion: "1.0.0", Package: "demo", Registry: "npm", Source: "fixture", SourceType: "language_package", Version: "1.0.0"}},
	}, Generation: 7, Processed: 3}
	pass := PassReport{StartedAt: time.Unix(1, 0), FinishedAt: time.Unix(2, 0), Processed: 3, Records: 1, Workers: 16, PackageBudget: 3000}

	if err := ExportSourceCompatibility(paths, document, snapshot, pass, CompatibilityProfileFor("npm")); err != nil {
		t.Fatal(err)
	}
	var state struct {
		Sources map[string]map[string]any `json:"sources"`
	}
	body, _ := os.ReadFile(paths.State)
	if err := json.Unmarshal(body, &state); err != nil {
		t.Fatal(err)
	}
	if state.Sources["crates"]["cursor"] != float64(99) || state.Sources["npm"]["parser_generation"] != float64(3) {
		t.Fatalf("state=%s", body)
	}
	if state.Sources["npm"]["packages_file"] != "data/production/npm-packages.txt" || state.Sources["npm"]["cursor"] != float64(3) {
		t.Fatalf("state=%s", body)
	}
	if _, exists := state.Sources["npm"]["modules_file"]; exists {
		t.Fatalf("Go-only key leaked into npm state: %s", body)
	}
	var report struct {
		Sources map[string]map[string]any `json:"sources"`
	}
	reportBody, _ := os.ReadFile(paths.Report)
	if err := json.Unmarshal(reportBody, &report); err != nil {
		t.Fatal(err)
	}
	if report.Sources["npm"]["packages_per_minute"] != float64(180) || report.Sources["npm"]["package_budget"] != float64(3000) {
		t.Fatalf("report=%s", reportBody)
	}
}
