package gocrawl

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestBoltStoreCommitsCursorRetryAndObservationsAtomically(t *testing.T) {
	path := filepath.Join(t.TempDir(), "crawl.db")
	store, err := OpenBoltStore(path, StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	seed := ImportSnapshot{
		Cursor:        10,
		CatalogOffset: 100,
		Observations:  []Observation{{Command: "old", Ecosystem: "go", Package: "old/module", Source: "old.zip"}},
	}
	if err := store.Import(context.Background(), seed); err != nil {
		t.Fatal(err)
	}
	results := []ModuleResult{
		{
			Work:         ModuleWork{Order: 0, CatalogIndex: 10, CatalogOffset: 110, Module: "example.com/tool"},
			Verdict:      VerdictSuccess,
			Observations: []Observation{{Command: "tool", Ecosystem: "go", Package: "example.com/tool", Source: "tool.zip"}},
		},
		{
			Work:    ModuleWork{Order: 1, CatalogIndex: 11, CatalogOffset: 120, Module: "example.com/retry", Attempt: 1},
			Verdict: VerdictRetry,
			Error:   "HTTP 503",
		},
	}
	if err := store.Commit(context.Background(), results); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	store, err = OpenBoltStore(path, StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	snapshot, err := store.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Cursor != 12 || snapshot.CatalogOffset != 120 || snapshot.Generation != 1 {
		t.Fatalf("checkpoint=%+v", snapshot)
	}
	if snapshot.Retries["example.com/retry"].Error != "HTTP 503" || snapshot.Retries["example.com/retry"].Attempts != 1 {
		t.Fatalf("retries=%v", snapshot.Retries)
	}
	if len(snapshot.Observations) != 2 {
		t.Fatalf("observations=%d", len(snapshot.Observations))
	}
}

func TestBoltStoreRejectsCursorHoleWithoutPartialMutation(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{Cursor: 4, CatalogOffset: 40}); err != nil {
		t.Fatal(err)
	}
	err = store.Commit(context.Background(), []ModuleResult{{
		Work:         ModuleWork{Order: 0, CatalogIndex: 5, CatalogOffset: 60, Module: "example.com/hole"},
		Verdict:      VerdictSuccess,
		Observations: []Observation{{Command: "must-not-commit", Package: "example.com/hole"}},
	}})
	if err == nil {
		t.Fatal("cursor hole was accepted")
	}
	snapshot, snapshotErr := store.Snapshot(context.Background())
	if snapshotErr != nil {
		t.Fatal(snapshotErr)
	}
	if snapshot.Cursor != 4 || snapshot.CatalogOffset != 40 || snapshot.Generation != 0 || len(snapshot.Observations) != 0 {
		t.Fatalf("failed transaction mutated checkpoint: %+v", snapshot)
	}
}

func TestBoltStoreMovesRepeatedFailureToUnavailable(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	for attempt := 1; attempt <= 3; attempt++ {
		result := ModuleResult{
			Work:    ModuleWork{Order: uint64(attempt - 1), Module: "example.com/broken", Retry: attempt > 1, Attempt: attempt},
			Verdict: VerdictRetry,
			Error:   "invalid zip",
		}
		if attempt == 1 {
			result.Work.CatalogIndex = 0
			result.Work.CatalogOffset = 20
		}
		if err := store.Commit(context.Background(), []ModuleResult{result}); err != nil {
			t.Fatal(err)
		}
	}
	snapshot, err := store.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Retries) != 0 || snapshot.Unavailable["example.com/broken"] == "" {
		t.Fatalf("snapshot=%+v", snapshot)
	}
}

func TestBoltStoreProgressOmitsHeavyObservations(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{
		Cursor:       7,
		Retries:      map[string]RetryEntry{"example.com/retry": {Attempts: 1}},
		Observations: []Observation{{Command: "tool", Package: "example.com/tool"}},
	}); err != nil {
		t.Fatal(err)
	}
	progress, err := store.Progress(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if progress.Cursor != 7 || len(progress.Retries) != 1 {
		t.Fatalf("progress=%+v", progress)
	}
	if len(progress.Observations) != 0 {
		t.Fatalf("progress loaded %d observations", len(progress.Observations))
	}
}

func TestBoltStoreReconcilesCatalogAppendBeforeCheckpoint(t *testing.T) {
	directory := t.TempDir()
	catalogPath := filepath.Join(directory, "go-modules.txt")
	if err := os.WriteFile(catalogPath, []byte("example.com/a\nexample.com/b\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store, err := OpenBoltStore(filepath.Join(directory, "crawl.db"), StoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{CatalogSize: 2}); err != nil {
		t.Fatal(err)
	}
	if err := store.SyncCatalog(context.Background(), catalogPath); err != nil {
		t.Fatal(err)
	}

	// Simulate a crash after the durable catalog append but before its DB metadata
	// transaction. Startup reconciliation must index the tail without duplicating it.
	file, err := os.OpenFile(catalogPath, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteString("example.com/c\nexample.com/d\n"); err != nil {
		t.Fatal(err)
	}
	if err := file.Sync(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	if err := store.SyncCatalog(context.Background(), catalogPath); err != nil {
		t.Fatal(err)
	}
	newModules, err := store.FilterNewCatalogModules(context.Background(), []string{
		"example.com/b", "example.com/c", "example.com/d", "example.com/e", "example.com/e",
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(newModules) != 1 || newModules[0] != "example.com/e" {
		t.Fatalf("new modules=%v", newModules)
	}
	progress, err := store.Progress(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if progress.CatalogSize != 4 {
		t.Fatalf("catalog size=%d", progress.CatalogSize)
	}
}

func TestBoltStoreRebuildsSameSizedReplacedCatalogIdentity(t *testing.T) {
	directory := t.TempDir()
	catalogPath := filepath.Join(directory, "go-modules.txt")
	if err := os.WriteFile(catalogPath, []byte("a\nb\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store, err := OpenBoltStore(filepath.Join(directory, "crawl.db"), StoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{CatalogSize: 2}); err != nil {
		t.Fatal(err)
	}
	if err := store.SyncCatalog(context.Background(), catalogPath); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(catalogPath, []byte("abc\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := store.SyncCatalog(context.Background(), catalogPath); err != nil {
		t.Fatal(err)
	}
	progress, err := store.Progress(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if progress.CatalogSize != 1 {
		t.Fatalf("catalog size=%d", progress.CatalogSize)
	}
}
