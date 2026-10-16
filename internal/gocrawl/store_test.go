package gocrawl

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bolt "go.etcd.io/bbolt"
)

func TestBoltStoreStripsURLCredentialsFromObservations(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Import(context.Background(), ImportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	repository := "git+ssh://ghp_secret@github.com:example/tool.git"
	legacy := Observation{
		Command: "tool", Ecosystem: "npm", Package: "tool",
		Repository: &repository, Source: "https://token@example.test/tool",
	}
	// Bypass putObservation to model a credential-bearing row from an existing DB.
	if err := store.db.Update(func(tx *bolt.Tx) error {
		return putJSON(tx.Bucket(observationBucket), "legacy", legacy)
	}); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	var streamed []Observation
	if err := store.ViewSnapshot(context.Background(), func(_ Snapshot, observations ObservationSequence) error {
		return observations(func(observation Observation) error {
			streamed = append(streamed, observation)
			return nil
		})
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	for _, observations := range [][]Observation{snapshot.Observations, streamed} {
		if got := *observations[0].Repository; got != "git+ssh://github.com:example/tool.git" {
			t.Fatalf("repository=%q", got)
		}
		if got := observations[0].Source; got != "https://example.test/tool" {
			t.Fatalf("source=%q", got)
		}
	}
}

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

func TestBoltStoreNeverSettlesUncountedRetry(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	for attempt := range 9 {
		result := ModuleResult{
			Work:    ModuleWork{Module: "example.com/healthy", Retry: attempt > 0, Attempt: 1},
			Verdict: VerdictRetry, Error: "temporary network failure", UncountedRetry: true,
		}
		if attempt == 0 {
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
	if snapshot.Retries["example.com/healthy"].Attempts != 0 || len(snapshot.Unavailable) != 0 {
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

func TestBoltStoreRefreshReplacesPackageObservationsAndCyclesCursor(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(t.Context(), ImportSnapshot{
		Cursor: 2, CatalogSize: 2,
		Observations: []Observation{
			{Command: "old", Ecosystem: "npm", Package: "demo", Source: "old"},
			{Command: "keep", Ecosystem: "npm", Package: "other", Source: "keep"},
		},
	}); err != nil {
		t.Fatal(err)
	}

	result := ModuleResult{
		Work: ModuleWork{Module: "demo", Refresh: true, CatalogIndex: 0,
			CatalogOffset: int64(len("demo\n"))},
		Verdict: VerdictSuccess,
		Observations: []Observation{
			{Command: "new", Ecosystem: "npm", Package: "demo", Source: "new"},
		},
	}
	if err := store.Commit(t.Context(), []ModuleResult{result}); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Snapshot(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.RefreshCursor != 1 || len(snapshot.Observations) != 2 ||
		snapshot.Observations[0].Command != "keep" || snapshot.Observations[1].Command != "new" {
		t.Fatalf("snapshot=%+v", snapshot)
	}

	result.Work = ModuleWork{Module: "demo", Refresh: true, CatalogIndex: 1,
		CatalogOffset: int64(len("demo\nother\n"))}
	result.Observations = nil
	if err := store.Commit(t.Context(), []ModuleResult{result}); err != nil {
		t.Fatal(err)
	}
	snapshot, err = store.Snapshot(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.RefreshCursor != 0 || len(snapshot.Observations) != 1 || snapshot.Observations[0].Command != "keep" {
		t.Fatalf("snapshot=%+v", snapshot)
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
	if err := store.Import(context.Background(), ImportSnapshot{CatalogSize: 2, RefreshCursor: 1}); err != nil {
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
	if progress.RefreshCursor != 1 {
		t.Fatalf("refresh cursor=%d", progress.RefreshCursor)
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
	if err := store.Import(context.Background(), ImportSnapshot{CatalogSize: 2, RefreshCursor: 1}); err != nil {
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
	if progress.RefreshCursor != 0 {
		t.Fatalf("refresh cursor=%d", progress.RefreshCursor)
	}
}

func TestAFailureOutsideTheRetryQueueCountsOnFromTheRecordedAttempts(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{FailureAttemptLimit: 4})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{Retries: map[string]RetryEntry{"pkg": {Error: "x", Attempts: 2}}}); err != nil {
		t.Fatal(err)
	}
	// A feed (or rotation) work carries Attempt 1; it used to reset the count to 1.
	feed := ModuleResult{Work: ModuleWork{Module: "pkg", Feed: true, Attempt: 1}, Verdict: VerdictRetry, Error: "timeout"}
	if err := store.Commit(context.Background(), []ModuleResult{feed}); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := snapshot.Retries["pkg"].Attempts; got != 3 {
		t.Fatalf("attempts = %d, want 3", got)
	}
	if err := store.Commit(context.Background(), []ModuleResult{feed}); err != nil {
		t.Fatal(err)
	}
	snapshot, _ = store.Snapshot(context.Background())
	if _, still := snapshot.Retries["pkg"]; still || !strings.Contains(snapshot.Unavailable["pkg"], "gave up after 4 attempts") {
		t.Fatalf("the limit must be reached: retries=%v unavailable=%v", snapshot.Retries, snapshot.Unavailable)
	}
}

func TestAnUnreadableVersionIsRecordedAsCheckedNotUnavailableNorRetried(t *testing.T) {
	store, err := OpenBoltStore(filepath.Join(t.TempDir(), "crawl.db"), StoreOptions{FailureAttemptLimit: 3})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{Retries: map[string]RetryEntry{"huge": {Error: "response exceeded", Attempts: 1}}}); err != nil {
		t.Fatal(err)
	}
	result := ModuleResult{Work: ModuleWork{Module: "huge", Retry: true, Attempt: 2}, Verdict: VerdictPermanent,
		Error: "artifact size 1 exceeds limit 0", Latest: "2.0", Outcome: Unreadable}
	if err := store.Commit(context.Background(), []ModuleResult{result}); err != nil {
		t.Fatal(err)
	}
	snapshot, err := store.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	check, ok := ParseCheck(snapshot.Checks["huge"])
	if !ok || check.Outcome != Unreadable || check.Version != "2.0" {
		t.Fatalf("check=%q", snapshot.Checks["huge"])
	}
	if len(snapshot.Retries) != 0 || snapshot.Unavailable["huge"] != "" {
		t.Fatalf("retries=%v unavailable=%v", snapshot.Retries, snapshot.Unavailable)
	}
	if again, ok := ParseCheck(EncodeCheck(check)); !ok || again != check {
		t.Fatalf("round trip %+v -> %+v", check, again)
	}
}
