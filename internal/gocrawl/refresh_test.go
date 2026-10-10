package gocrawl

import (
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

type refreshGolden struct {
	MaxDays int `json:"max_days"`
	Cases   []struct {
		Module   string `json:"module"`
		Streak   int    `json:"streak"`
		Interval int    `json:"interval"`
	} `json:"cases"`
}

// The same table is checked by tests/test_refresh_policy.py: the two implementations
// must schedule a package identically, or a Python publisher and the Go crawler would
// disagree about which packages are due.
func TestRecheckIntervalMatchesSharedGolden(t *testing.T) {
	body, err := os.ReadFile("testdata/refresh/policy-golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var golden refreshGolden
	if err := json.Unmarshal(body, &golden); err != nil {
		t.Fatal(err)
	}
	if len(golden.Cases) < 20 {
		t.Fatalf("golden has %d cases", len(golden.Cases))
	}
	for _, c := range golden.Cases {
		if got := RecheckInterval(c.Module, c.Streak, golden.MaxDays); got != c.Interval {
			t.Errorf("RecheckInterval(%q, %d, %d)=%d want %d", c.Module, c.Streak, golden.MaxDays, got, c.Interval)
		}
	}
}

func TestCheckEncodingRoundTripsAndRejectsGarbage(t *testing.T) {
	for _, check := range []Check{{Day: 20000, Streak: 3, Version: "1.2.3"}, {Day: 0, Streak: 0, Version: "v1.0.0+incompatible:x"}} {
		got, ok := ParseCheck(EncodeCheck(check))
		if !ok || got != check {
			t.Fatalf("round trip of %+v gave %+v ok=%v", check, got, ok)
		}
	}
	for _, bad := range []string{"", "1", "1:2", "x:1:v", "1:y:v", "-1:0:v"} {
		if _, ok := ParseCheck(bad); ok {
			t.Fatalf("%q must not parse", bad)
		}
	}
}

func TestDueFollowsBackoffAndCapsAtTheSourceLimit(t *testing.T) {
	check := Check{Day: 100, Streak: 0, Version: "1"}
	if check.Due("m", 100, 0, 14) || !check.Due("m", 101, 0, 14) {
		t.Fatal("a package seen unchanged once is due after a day")
	}
	long := Check{Day: 100, Streak: 30, Version: "1"}
	if long.Due("m", 100+13, 0, 14) || !long.Due("m", 100+14, 0, 14) {
		t.Fatal("the interval must cap at the source limit")
	}
	if !long.Due("m", 101, 101, 14) {
		t.Fatal("a check older than the due floor (a resync) is always due")
	}
	next, changed := Next(Check{Day: 5, Streak: 2, Version: "1"}, true, "1", "", 9)
	if changed || next.Streak != 3 || next.Day != 9 {
		t.Fatalf("unchanged: %+v changed=%v", next, changed)
	}
	next, changed = Next(Check{Day: 5, Streak: 2, Version: "1"}, true, "2", "", 9)
	if !changed || next.Streak != 0 {
		t.Fatalf("changed version must restart the backoff: %+v", next)
	}
}

func openRefreshStore(t *testing.T, path string, now func() time.Time) *BoltStore {
	t.Helper()
	store, err := OpenBoltStore(path, StoreOptions{Now: now})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func fixedClock(day int) func() time.Time {
	return func() time.Time { return time.Unix(int64(day)*86400+3600, 0) }
}

func TestCommitKeepsObservationsOfUnchangedResultAndAdvancesCheck(t *testing.T) {
	store := openRefreshStore(t, filepath.Join(t.TempDir(), "crawl.db"), fixedClock(200))
	row := Observation{Command: "demo", Ecosystem: "pypi", Package: "demo", Source: "s", Version: "1.0"}
	if err := store.Import(t.Context(), ImportSnapshot{CatalogSize: 1, Observations: []Observation{row}}); err != nil {
		t.Fatal(err)
	}
	// Importing seeds a check from the stored row so the first re-check can already skip.
	checks, _ := store.Checks(t.Context(), []string{"demo"})
	if checks["demo"].Version != "1.0" || checks["demo"].Day >= 200 || checks["demo"].Streak >= ColdStaggerStreaks {
		t.Fatalf("seed=%+v", checks)
	}
	work := ModuleWork{Module: "demo", Refresh: true, CatalogIndex: 0, Known: "1.0"}
	unchanged := ModuleResult{Work: work}.AsUnchanged("1.0")
	if err := store.Commit(t.Context(), []ModuleResult{unchanged}); err != nil {
		t.Fatal(err)
	}
	snapshot, _ := store.Snapshot(t.Context())
	if len(snapshot.Observations) != 1 {
		t.Fatalf("an unchanged look must keep the stored rows: %+v", snapshot.Observations)
	}
	if check, _ := ParseCheck(snapshot.Checks["demo"]); check.Day != 200 || check.Streak != 1 {
		t.Fatalf("check=%+v", snapshot.Checks)
	}
	// A changed version replaces the rows and restarts the streak.
	changed := ModuleResult{Work: ModuleWork{Module: "demo", Refresh: true, CatalogIndex: 0}, Verdict: VerdictSuccess, Latest: "2.0",
		Observations: []Observation{{Command: "demo2", Ecosystem: "pypi", Package: "demo", Source: "s", Version: "2.0"}}}
	if err := store.Commit(t.Context(), []ModuleResult{changed}); err != nil {
		t.Fatal(err)
	}
	snapshot, _ = store.Snapshot(t.Context())
	if len(snapshot.Observations) != 1 || snapshot.Observations[0].Command != "demo2" {
		t.Fatalf("observations=%+v", snapshot.Observations)
	}
	if check, _ := ParseCheck(snapshot.Checks["demo"]); check.Streak != 0 || check.Version != "2.0" {
		t.Fatalf("check=%+v", snapshot.Checks)
	}
}

func TestCommitDropsCheckAndRowsOfAPermanentFailure(t *testing.T) {
	store := openRefreshStore(t, filepath.Join(t.TempDir(), "crawl.db"), fixedClock(200))
	row := Observation{Command: "demo", Ecosystem: "rubygems", Package: "demo", Source: "s", Version: "1.0"}
	if err := store.Import(t.Context(), ImportSnapshot{CatalogSize: 1, Observations: []Observation{row}}); err != nil {
		t.Fatal(err)
	}
	gone := ModuleResult{Work: ModuleWork{Module: "demo", Refresh: true}, Verdict: VerdictPermanent, Error: "gem is yanked"}
	if err := store.Commit(t.Context(), []ModuleResult{gone}); err != nil {
		t.Fatal(err)
	}
	snapshot, _ := store.Snapshot(t.Context())
	if len(snapshot.Observations) != 0 || len(snapshot.Checks) != 0 || snapshot.Unavailable["demo"] == "" {
		t.Fatalf("yanked/deleted package must leave rows and checks: %+v", snapshot)
	}
}

func TestSkippedEntriesAdvanceTheCursorAndChangeNothing(t *testing.T) {
	store := openRefreshStore(t, filepath.Join(t.TempDir(), "crawl.db"), fixedClock(200))
	if err := store.Import(t.Context(), ImportSnapshot{
		CatalogSize: 2, Cursor: 2, Checks: map[string]string{"a": EncodeCheck(Check{Day: 199, Version: "1"})},
		Unavailable: map[string]string{"b": "gone"},
	}); err != nil {
		t.Fatal(err)
	}
	skipped := []ModuleResult{
		{Work: ModuleWork{Module: "a", Refresh: true, CatalogIndex: 0, Skip: true}, Verdict: VerdictSuccess, Unchanged: true},
		{Work: ModuleWork{Module: "b", Refresh: true, CatalogIndex: 1, Skip: true}, Verdict: VerdictSuccess, Unchanged: true},
	}
	if err := store.Commit(t.Context(), skipped); err != nil {
		t.Fatal(err)
	}
	snapshot, _ := store.Snapshot(t.Context())
	if snapshot.RefreshCursor != 0 && snapshot.RefreshCursor != 2 {
		t.Fatalf("refresh cursor=%d", snapshot.RefreshCursor)
	}
	if snapshot.Checks["a"] != EncodeCheck(Check{Day: 199, Version: "1"}) {
		t.Fatalf("a skipped entry must not rewrite its check (shard churn): %v", snapshot.Checks)
	}
	if snapshot.Unavailable["b"] != "gone" {
		t.Fatal("a skipped entry must not clear an unavailable mark")
	}
}

func TestFeedCursorAndQueueSurviveReopenAndAreRemovedWithTheirObservations(t *testing.T) {
	path := filepath.Join(t.TempDir(), "crawl.db")
	store, err := OpenBoltStore(path, StoreOptions{Now: fixedClock(300)})
	if err != nil {
		t.Fatal(err)
	}
	seed := Observation{Command: "c", Ecosystem: "pypi", Package: "known", Source: "s", Version: "1"}
	if err := store.Import(t.Context(), ImportSnapshot{CatalogSize: 1, Cursor: 1, Observations: []Observation{seed}}); err != nil {
		t.Fatal(err)
	}
	page := FeedPage{Cursor: "42", Events: []FeedEvent{
		{Name: "known", Time: 1000, Kind: "new release"}, {Name: "stranger", Time: 1001}, {Name: "known", Time: 900},
	}}
	enqueued, err := store.ApplyFeed(t.Context(), page, FeedOptions{})
	if err != nil || enqueued != 1 {
		t.Fatalf("an unknown name must be rejected when unknown names are not admitted: n=%d err=%v", enqueued, err)
	}
	if enqueued, err = store.ApplyFeed(t.Context(), FeedPage{Cursor: "43", Events: []FeedEvent{{Name: "stranger", Time: 1001}}}, FeedOptions{AdmitUnknown: true}); err != nil || enqueued != 1 {
		t.Fatalf("n=%d err=%v", enqueued, err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	// A crash before the observations are committed: the queue and the cursor are both
	// durable, so the next run replays the announcements instead of losing them.
	store = openRefreshStore(t, path, fixedClock(300))
	cursor, _ := store.FeedCursor(t.Context())
	pending, total, err := store.PendingFeed(t.Context(), time.Unix(5000, 0), time.Minute, 10)
	if err != nil || cursor != "43" || total != 2 || len(pending) != 2 || pending[0] != "known" {
		t.Fatalf("cursor=%s pending=%v total=%d err=%v", cursor, pending, total, err)
	}
	// Committing the observations removes the announcement in the same transaction.
	done := ModuleResult{Work: ModuleWork{Module: "known", Feed: true}, Verdict: VerdictSuccess, Latest: "2",
		Observations: []Observation{{Command: "c", Ecosystem: "pypi", Package: "known", Source: "s", Version: "2"}}}
	if err := store.Commit(t.Context(), []ModuleResult{done}); err != nil {
		t.Fatal(err)
	}
	snapshot, _ := store.Snapshot(t.Context())
	if _, still := snapshot.FeedPending["known"]; still || len(snapshot.FeedPending) != 1 {
		t.Fatalf("pending=%v", snapshot.FeedPending)
	}
	if snapshot.Cursor != 1 {
		t.Fatalf("feed work must not move the catalog cursor: %d", snapshot.Cursor)
	}
	// A retry verdict hands the module to the retry bucket and leaves the queue too.
	retry := ModuleResult{Work: ModuleWork{Module: "stranger", Feed: true, Attempt: 1}, Verdict: VerdictRetry, Error: "boom"}
	if err := store.Commit(t.Context(), []ModuleResult{retry}); err != nil {
		t.Fatal(err)
	}
	snapshot, _ = store.Snapshot(t.Context())
	if len(snapshot.FeedPending) != 0 || snapshot.Retries["stranger"].Attempts != 1 {
		t.Fatalf("pending=%v retries=%v", snapshot.FeedPending, snapshot.Retries)
	}
}

func TestPendingFeedHoldsBackAnnouncementsYoungerThanTheFloor(t *testing.T) {
	store := openRefreshStore(t, filepath.Join(t.TempDir(), "crawl.db"), fixedClock(300))
	if err := store.Import(t.Context(), ImportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	_, err := store.ApplyFeed(t.Context(), FeedPage{Cursor: "1", Events: []FeedEvent{
		{Name: "fresh", Time: 1000}, {Name: "old", Time: 100},
	}}, FeedOptions{AdmitUnknown: true})
	if err != nil {
		t.Fatal(err)
	}
	ready, total, _ := store.PendingFeed(t.Context(), time.Unix(1030, 0), time.Minute, 10)
	if len(ready) != 1 || ready[0] != "old" || total != 2 {
		t.Fatalf("ready=%v total=%d: an announcement must wait for the registry cache floor", ready, total)
	}
	ready, _, _ = store.PendingFeed(t.Context(), time.Unix(1061, 0), time.Minute, 10)
	if len(ready) != 2 {
		t.Fatalf("ready=%v", ready)
	}
}

func TestResyncMakesEveryCheckDue(t *testing.T) {
	store := openRefreshStore(t, filepath.Join(t.TempDir(), "crawl.db"), fixedClock(400))
	if err := store.Import(t.Context(), ImportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ApplyFeed(t.Context(), FeedPage{Cursor: "9", Resync: true}, FeedOptions{}); err != nil {
		t.Fatal(err)
	}
	floor, _ := store.DueFloor(t.Context())
	stale := Check{Day: 399, Streak: 20, Version: "1"}
	if floor != 400 || !stale.Due("m", 400, floor, BackoffMaxDaysFeed) {
		t.Fatalf("floor=%d: a resync must make even a long-backoff package due", floor)
	}
	if _, err := store.ApplyFeed(t.Context(), FeedPage{}, FeedOptions{}); err == nil {
		t.Fatal("a page without a cursor must be rejected so the cursor cannot be erased")
	}
}

func TestCatalogRefreshQueuesKnownModulesTheIndexAnnounced(t *testing.T) {
	directory := t.TempDir()
	catalogPath := filepath.Join(directory, "go-modules.txt")
	if err := os.WriteFile(catalogPath, []byte("example.com/a\nexample.com/idle\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := openRefreshStore(t, filepath.Join(directory, "crawl.db"), fixedClock(500))
	seed := Observation{Command: "a", Ecosystem: "go", Package: "example.com/a", Source: "s", Version: "v1.0.0"}
	if err := store.Import(t.Context(), ImportSnapshot{CatalogSize: 2, Cursor: 2, CatalogSince: "2026-08-20T00:00:00Z", Observations: []Observation{seed}}); err != nil {
		t.Fatal(err)
	}
	fetcher := &stubCatalogFetcher{pages: []CatalogPage{
		{Entries: []CatalogEntry{
			{Path: "example.com/a", Version: "v1.1.0", Timestamp: "2026-08-21T00:00:00Z"},
			{Path: "example.com/idle", Version: "v0.1.0", Timestamp: "2026-08-21T00:00:01Z"},
			{Path: "example.com/new", Version: "v0.0.1", Timestamp: "2026-08-21T00:00:02Z"},
		}},
		{Complete: true},
	}}
	if _, err := RefreshCatalog(t.Context(), catalogPath, store, fetcher, CatalogRefreshOptions{MaxPages: 2, PageSize: 3}); err != nil {
		t.Fatal(err)
	}
	pending, _, _ := store.PendingFeed(t.Context(), time.Now(), 0, 10)
	if len(pending) != 1 || pending[0] != "example.com/a" {
		t.Fatalf("only an already-inspected module is queued (new ones are walked, unseen ones wait for rotation): %v", pending)
	}
	if since, _ := store.Progress(t.Context()); since.CatalogSince != "2026-08-21T00:00:02Z" {
		t.Fatalf("since=%s", since.CatalogSince)
	}
}

func TestStateRoundTripKeepsFeedAndNeverExportsChecks(t *testing.T) {
	directory := t.TempDir()
	statePath := filepath.Join(directory, "registry-state")
	catalog := filepath.Join(directory, "names.txt")
	if err := os.WriteFile(catalog, []byte("demo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store := openRefreshStore(t, filepath.Join(directory, "crawl.db"), fixedClock(600))
	profile := CompatibilityProfileFor("pypi")
	if err := store.Import(t.Context(), ImportSnapshot{
		CatalogSize: 1, Cursor: 1, CatalogComplete: true, ModulesFile: catalog,
		Checks: map[string]string{"demo": EncodeCheck(Check{Day: 590, Streak: 2, Version: "1.0"})}, FeedCursor: "77", DueFloor: 3,
		FeedPending: map[string]FeedEntry{"demo": {Time: 5, Kind: "new release"}},
	}); err != nil {
		t.Fatal(err)
	}
	paths := ExportPaths{State: statePath, Observations: filepath.Join(directory, "pypi.jsonl"), Report: filepath.Join(directory, "report.json")}
	if err := ExportSourceStoreCompatibility(t.Context(), paths, StateDocument{}, store, PassReport{FinishedAt: time.Now(), StartedAt: time.Now()}, profile); err != nil {
		t.Fatal(err)
	}
	imported, _, err := LoadSourceCompatibility(statePath, paths.Observations, catalog, profile)
	if err != nil {
		t.Fatal(err)
	}
	// Checks are cache: the exported state carries none (the history/cache split).
	if len(imported.Checks) != 0 || imported.FeedCursor != "77" || imported.DueFloor != 3 || imported.FeedPending["demo"].Kind != "new release" {
		t.Fatalf("imported=%+v", imported)
	}
	// State written by older extraction logic cannot vouch for stored rows.
	defer func(previous int) { ExtractionRevision = previous }(ExtractionRevision)
	ExtractionRevision++
	imported, _, err = LoadSourceCompatibility(statePath, paths.Observations, catalog, profile)
	if err != nil || len(imported.Checks) != 0 || imported.FeedCursor != "77" {
		t.Fatalf("the feed position stays across an extraction revision bump: %+v err=%v", imported, err)
	}
}

func TestLegacyCheckedStateIsReadOnceAndDroppedOnTheNextSave(t *testing.T) {
	directory := t.TempDir()
	statePath := filepath.Join(directory, "registry-state")
	catalog := filepath.Join(directory, "names.txt")
	os.WriteFile(catalog, []byte("demo\n"), 0o644)
	profile := CompatibilityProfileFor("pypi")
	legacy := StateDocument{"version": json.RawMessage("1"), "sources": json.RawMessage(
		`{"pypi":{"catalog_complete":true,"catalog_size":1,"cursor":1,"checked":{"demo":"590:2:1.0"},"extraction_revision":` +
			strconv.Itoa(ExtractionRevision) + `}}`)}
	if err := WriteStateDocument(statePath, legacy); err != nil {
		t.Fatal(err)
	}
	imported, document, err := LoadSourceCompatibility(statePath, filepath.Join(directory, "none.jsonl"), catalog, profile)
	if err != nil || imported.Checks["demo"] != "590:2:1.0" {
		t.Fatalf("legacy checks are still read for one release: %+v %v", imported.Checks, err)
	}
	store := openRefreshStore(t, filepath.Join(directory, "crawl.db"), fixedClock(600))
	if err := store.Import(t.Context(), imported); err != nil {
		t.Fatal(err)
	}
	paths := ExportPaths{State: statePath, Observations: filepath.Join(directory, "pypi.jsonl"), Report: filepath.Join(directory, "report.json")}
	if err := ExportSourceStoreCompatibility(t.Context(), paths, document, store, PassReport{FinishedAt: time.Now(), StartedAt: time.Now()}, profile); err != nil {
		t.Fatal(err)
	}
	after, _, _ := LoadSourceCompatibility(statePath, paths.Observations, catalog, profile)
	if len(after.Checks) != 0 {
		t.Fatalf("checked must be gone after one save: %v", after.Checks)
	}
}

func TestCacheRoundTripsAndColdCacheIsHarmless(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cache", "pypi.cache.gz")
	if _, found, err := ReadCache(path, 1); found || err != nil {
		t.Fatalf("a missing cache is a cold start: found=%v err=%v", found, err)
	}
	want := Cache{Cursor: 42, Checks: map[string]string{"a": "10:0:1.0", "b": "11:3:2.0"}}
	if err := WriteCache(path, want, 1); err != nil {
		t.Fatal(err)
	}
	got, found, err := ReadCache(path, 1)
	if err != nil || !found || got.Cursor != 42 || len(got.Checks) != 2 || got.Checks["b"] != "11:3:2.0" {
		t.Fatalf("got=%+v found=%v err=%v", got, found, err)
	}
	if _, found, _ := ReadCache(path, 2); found {
		t.Fatal("a cache written by another extraction revision must be ignored")
	}
	if err := os.WriteFile(path, []byte("not gzip"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, found, err := ReadCache(path, 1); found || err == nil {
		t.Fatalf("a corrupt cache is reported and treated as cold: found=%v err=%v", found, err)
	}
}

func TestColdStartSpreadsDueDatesAndTheRotationStartMoves(t *testing.T) {
	streaks := map[int]int{}
	for index := range 400 {
		streaks[ColdCheck(fmt.Sprintf("pkg-%d", index), "1", 1000).Streak]++
	}
	if len(streaks) != ColdStaggerStreaks {
		t.Fatalf("cold checks must use every stagger tier: %v", streaks)
	}
	now := time.Unix(1_800_000_000, 0)
	first := ColdRefreshStart(870_000, 3000, now)
	second := ColdRefreshStart(870_000, 3000, now.Add(6*time.Hour))
	if first == second || second != (first+3000)%870_000 {
		t.Fatalf("each six-hour window starts one budget further: %d %d", first, second)
	}
}

// The golden directory is written by Python's registry_state.save_state (see
// tests/test_registry_state.py): both encoders must produce the same bytes for the
// change-driven fields, or a Python publisher re-saving a Go checkpoint would churn it.
func TestStateGoldenWithChecksIsByteIdentical(t *testing.T) {
	golden := "testdata/refresh/state-golden"
	document, found, err := ReadStateDocument(golden)
	if err != nil || !found {
		t.Fatalf("found=%v err=%v", found, err)
	}
	rewritten := filepath.Join(t.TempDir(), "state")
	if err := WriteStateDocument(rewritten, document); err != nil {
		t.Fatal(err)
	}
	readAll := func(root string) map[string]string {
		files := map[string]string{}
		err := filepath.WalkDir(root, func(path string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil || entry.IsDir() {
				return walkErr
			}
			body, err := os.ReadFile(path)
			relative, _ := filepath.Rel(root, path)
			files[relative] = string(body)
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
		return files
	}
	want, got := readAll(golden), readAll(rewritten)
	if len(want) < 3 || len(got) != len(want) {
		t.Fatalf("files: golden=%d rewritten=%d", len(want), len(got))
	}
	for name, body := range want {
		if got[name] != body {
			t.Errorf("%s differs between the Python and Go encoders", name)
		}
	}
}

func TestColdChecksAndCacheFileMatchThePythonGoldens(t *testing.T) {
	body, err := os.ReadFile("testdata/refresh/cold-golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		Cases []struct {
			Module, Version, Check string
			Day                    int
		} `json:"cases"`
	}
	if err := json.Unmarshal(body, &golden); err != nil {
		t.Fatal(err)
	}
	for _, c := range golden.Cases {
		if got := EncodeCheck(ColdCheck(c.Module, c.Version, c.Day)); got != c.Check {
			t.Errorf("ColdCheck(%q) = %s, Python says %s", c.Module, got, c.Check)
		}
	}
	path := filepath.Join(t.TempDir(), "c.gz")
	cache := Cache{Cursor: 77, Checks: map[string]string{"alpha": "20001:2n:1.0", "b": "20002:0c:2.0", "zeta": "19990:5:9.9.9"}}
	if err := WriteCache(path, cache, 1); err != nil {
		t.Fatal(err)
	}
	file, _ := os.Open(path)
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		t.Fatal(err)
	}
	text, _ := io.ReadAll(reader)
	want, _ := os.ReadFile("testdata/refresh/schedule-cache-golden.txt")
	if string(text) != string(want) {
		t.Fatalf("cache text differs from the Python golden:\n%s\nwant\n%s", text, want)
	}
}

func TestCacheFileIsReadableByOtherUsers(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cache", "pypi.cache.gz")
	if err := WriteCache(path, Cache{Checks: map[string]string{"a": "1:0:1"}}, 1); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	// CreateTemp is 0600: a root crawler container's cache must still be readable by the
	// runner user that saves it (2026-10-09 "tar: Cannot open: Permission denied").
	if info.Mode().Perm() != 0o644 {
		t.Fatalf("mode %v", info.Mode().Perm())
	}
}

func TestOutcomesRoundTripStayBackwardCompatibleAndMatchPython(t *testing.T) {
	for value, want := range map[string]Check{
		"20000:3n:1.2.3": {Day: 20000, Streak: 3, Outcome: NoCommands, Version: "1.2.3"},
		"20000:3c:v1:x":  {Day: 20000, Streak: 3, Outcome: HasCommands, Version: "v1:x"},
		"20000:3:1.2.3":  {Day: 20000, Streak: 3, Version: "1.2.3"},
	} {
		got, ok := ParseCheck(value)
		if !ok || got != want || EncodeCheck(want) != value {
			t.Errorf("%s -> %+v %v", value, got, ok)
		}
	}
	for _, bad := range []string{"20000:n:1", "x:1c:1", "1:1"} {
		if _, ok := ParseCheck(bad); ok {
			t.Errorf("%s must not parse", bad)
		}
	}
	next, _ := Next(Check{Day: 10, Version: "1", Outcome: NoCommands}, true, "1", OutcomeUnknown, 11)
	if next != (Check{Day: 11, Streak: 1, Outcome: NoCommands, Version: "1"}) {
		t.Fatalf("%+v", next)
	}
	counts := checkOutcomes(map[string]string{"a": "1:0n:1", "b": "1:0c:1", "c": "1:0:1"})
	if counts["no_commands"] != 1 || counts["has_commands"] != 1 || counts["unknown"] != 1 {
		t.Fatalf("%v", counts)
	}
}
