package main

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
)

func TestPlanPassWorksSkipsNotDueEntriesWithoutSpendingBudget(t *testing.T) {
	catalog := filepath.Join(t.TempDir(), "names.txt")
	var names strings.Builder
	for index := range 100 {
		fmt.Fprintf(&names, "pkg-%03d\n", index)
	}
	if err := os.WriteFile(catalog, []byte(names.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	checks := map[string]gocrawl.Check{}
	for index := range 100 {
		// The first 80 were checked today after a long unchanged streak; the rest are new.
		if index < 80 {
			checks[fmt.Sprintf("pkg-%03d", index)] = gocrawl.Check{Day: 1000, Streak: 20, Version: "1"}
		}
	}
	before := gocrawl.Snapshot{ImportSnapshot: gocrawl.ImportSnapshot{CatalogSize: 100, Cursor: 100, CatalogComplete: true}}
	policy := passPolicy{Today: 1001, MaxDays: 60, Checks: func(modules []string) (map[string]gocrawl.Check, error) {
		found := map[string]gocrawl.Check{}
		for _, module := range modules {
			if check, ok := checks[module]; ok {
				found[module] = check
			}
		}
		return found, nil
	}}
	works, err := planPassWorks(catalog, before, 10, true, policy)
	if err != nil {
		t.Fatal(err)
	}
	skipped, due := 0, 0
	for _, work := range works {
		if work.Skip {
			skipped++
			if !work.Refresh {
				t.Fatal("a skipped rotation entry must still advance the refresh cursor")
			}
		} else {
			due++
		}
	}
	if due != 10 || skipped != 80 {
		t.Fatalf("due=%d skipped=%d: the budget must be spent on due packages only", due, skipped)
	}
	for index, work := range works {
		if work.CatalogIndex != uint64(index) {
			t.Fatalf("rotation entries must stay contiguous: %d at %d", work.CatalogIndex, index)
		}
	}
}

func TestPlanPassWorksKeepsTheRotationBackstopWhenAFeedFloodsTheBudget(t *testing.T) {
	catalog := filepath.Join(t.TempDir(), "names.txt")
	var names strings.Builder
	for index := range 50 {
		fmt.Fprintf(&names, "pkg-%03d\n", index)
	}
	if err := os.WriteFile(catalog, []byte(names.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	flood := make([]string, 500)
	for index := range flood {
		flood[index] = fmt.Sprintf("announced-%03d", index)
	}
	before := gocrawl.Snapshot{ImportSnapshot: gocrawl.ImportSnapshot{CatalogSize: 50, Cursor: 50, CatalogComplete: true}}
	works, err := planPassWorks(catalog, before, 40, true, passPolicy{Feed: flood, MaxDays: 60})
	if err != nil {
		t.Fatal(err)
	}
	feed, refresh := 0, 0
	for _, work := range works {
		switch {
		case work.Feed:
			feed++
		case work.Refresh:
			refresh++
		}
	}
	if len(works) != 40 || refresh < 4 || feed != 36 {
		t.Fatalf("total=%d feed=%d refresh=%d: the rotation keeps a tenth of the budget", len(works), feed, refresh)
	}
	// While the catalog is still being walked, the walk keeps at least half.
	walking := gocrawl.Snapshot{ImportSnapshot: gocrawl.ImportSnapshot{CatalogSize: 50, Cursor: 0}}
	works, err = planPassWorks(catalog, walking, 40, true, passPolicy{Feed: flood, MaxDays: 60})
	if err != nil {
		t.Fatal(err)
	}
	walk := 0
	for _, work := range works {
		if !work.Feed && !work.Refresh && !work.Retry {
			walk++
		}
	}
	if walk < 20 {
		t.Fatalf("walk=%d: feed announcements must not starve the catalog walk", walk)
	}
}

// fakePyPI serves metadata, one wheel per project and the XML-RPC changelog, and
// counts what the crawler asks for.
type fakePyPI struct {
	mu         sync.Mutex
	versions   map[string]string
	serial     int64
	changes    [][]any
	metadata   int
	files      int
	fileSize   int64
	now        func() time.Time
	wheel      []byte
	plain      map[string]bool // projects whose wheel ships no command
	plainWheel []byte
}

func newFakePyPI(t *testing.T, projects int, now func() time.Time) (*fakePyPI, *httptest.Server) {
	t.Helper()
	var wheel bytes.Buffer
	writer := zip.NewWriter(&wheel)
	entry, _ := writer.Create("demo-1.dist-info/entry_points.txt")
	_, _ = entry.Write([]byte("[console_scripts]\ndemo = demo:main\n"))
	// Real wheels carry code and data; pad so a download costs what it would.
	padding, _ := writer.Create("demo/data.bin")
	_, _ = padding.Write(bytes.Repeat([]byte("x"), 20_000))
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	var plainWheel bytes.Buffer
	plainWriter := zip.NewWriter(&plainWheel)
	plainData, _ := plainWriter.Create("demo/data.bin")
	_, _ = plainData.Write(bytes.Repeat([]byte("x"), 20_000))
	if err := plainWriter.Close(); err != nil {
		t.Fatal(err)
	}
	fake := &fakePyPI{versions: map[string]string{}, now: now, wheel: wheel.Bytes(), serial: 1000,
		plain: map[string]bool{}, plainWheel: plainWheel.Bytes()}
	for index := range projects {
		fake.versions[fmt.Sprintf("p%02d", index)] = "1.0.0"
	}
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fake.mu.Lock()
		defer fake.mu.Unlock()
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/pypi":
			body, _ := io.ReadAll(r.Body)
			if strings.Contains(string(body), "changelog_last_serial") {
				fmt.Fprintf(w, `<?xml version='1.0'?><methodResponse><params><param><value><int>%d</int></value></param></params></methodResponse>`, fake.serial)
				return
			}
			var since int64
			fmt.Sscanf(string(body[strings.Index(string(body), "<int>")+5:]), "%d", &since)
			var rows strings.Builder
			for _, change := range fake.changes {
				if change[3].(int64) > since {
					fmt.Fprintf(&rows, `<value><array><data><value><string>%s</string></value><value><string>%s</string></value><value><int>%d</int></value><value><string>new release</string></value><value><int>%d</int></value></data></array></value>`,
						change[0], change[1], change[2], change[3])
				}
			}
			fmt.Fprintf(w, `<?xml version='1.0'?><methodResponse><params><param><value><array><data>%s</data></array></value></param></params></methodResponse>`, rows.String())
		case strings.HasSuffix(r.URL.Path, "/json"):
			name := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/pypi/"), "/json")
			version, ok := fake.versions[name]
			if !ok {
				http.NotFound(w, r)
				return
			}
			fake.metadata++
			_, _ = fmt.Fprintf(w, `{"info":{"name":%q,"version":%q},"urls":[{"packagetype":"bdist_wheel","filename":"%s-%s-py3-none-any.whl","size":%d,"url":%q}]}`,
				name, version, name, version, len(fake.wheel), server.URL+"/files/"+name+"/"+version+".whl")
		case strings.HasPrefix(r.URL.Path, "/files/"):
			fake.files++
			body := fake.wheel
			if fake.plain[strings.Split(strings.TrimPrefix(r.URL.Path, "/files/"), "/")[0]] {
				body = fake.plainWheel
			}
			http.ServeContent(w, r, "x.whl", time.Unix(0, 0), bytes.NewReader(body))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(server.Close)
	return fake, server
}

func (f *fakePyPI) release(name, version string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.versions[name] = version
}

func (f *fakePyPI) announce(name, version string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.serial++
	f.changes = append(f.changes, []any{name, version, f.now().Add(-10 * time.Minute).Unix(), f.serial})
}

func (f *fakePyPI) counts() (metadata, files int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.metadata, f.files
}

func readRows(t *testing.T, path string) map[string]string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	versions := map[string]string{}
	for line := range strings.SplitSeq(strings.TrimSpace(string(body)), "\n") {
		if line == "" {
			continue
		}
		var row struct{ Package, Version string }
		var raw map[string]any
		if err := json.Unmarshal([]byte(line), &raw); err != nil {
			t.Fatal(err)
		}
		row.Package, _ = raw["package"].(string)
		row.Version, _ = raw["version"].(string)
		versions[row.Package] = row.Version
	}
	return versions
}

// TestPyPIChangeDrivenRefreshReplay replays three days of a registry against the
// crawler and compares what it requests with what the fixed rotation would: the
// baseline is the first pass (every package read in full), which is also what every
// rotation visit cost before checks existed.
func TestPyPIChangeDrivenRefreshReplay(t *testing.T) {
	const projects = 40
	day := time.Date(2026, 10, 1, 3, 0, 0, 0, time.UTC)
	var offset atomic64
	defer func(previous func() time.Time) { clock = previous }(clock)
	clock = func() time.Time { return day.Add(time.Duration(offset.Load()) * 24 * time.Hour) }
	fake, server := newFakePyPI(t, projects, clock)

	directory := t.TempDir()
	config := crawlConfig{
		Source: "pypi", StatePath: filepath.Join(directory, "registry-state"),
		ObservationsPath: filepath.Join(directory, "pypi.jsonl"), ReportPath: filepath.Join(directory, "report.json"),
		CatalogPath: filepath.Join(directory, "projects.txt"), DatabasePath: filepath.Join(directory, "crawl.db"),
		RegistryURL: server.URL, FeedURL: server.URL, PackageBudget: projects, ByteBudget: 1 << 30, Workers: 4,
		MaxInFlight: 8, CommitBatch: 8, RequestTimeout: 2 * time.Second, ModuleTimeout: 5 * time.Second,
	}
	var catalog strings.Builder
	for _, name := range slices.Sorted(maps.Keys(fake.versions)) {
		catalog.WriteString(name + "\n")
	}
	if err := os.WriteFile(config.CatalogPath, []byte(catalog.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(config.StatePath+".json", []byte(`{"version":1,"sources":{"pypi":{"cursor":0,"catalog_complete":true}}}`), 0o644); err != nil {
		t.Fatal(err)
	}

	// Day 0: the walk reads every project in full (the cost of any visit without a check).
	walk, err := executePass(t.Context(), config)
	if err != nil || walk.Processed != projects {
		t.Fatalf("walk=%+v err=%v", walk, err)
	}
	baseMetadata, baseFiles := fake.counts()
	baseBytes := walk.DownloadedBytes
	if baseFiles != projects {
		t.Fatalf("baseline wheel downloads=%d", baseFiles)
	}

	// Day 2: continuous refresh. Nothing changed: every package is due, one metadata
	// request each, and no wheel is read. The feed poll only records its position.
	offset.Store(2)
	config.Continuous = true
	second, err := executePass(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	m2, f2 := fake.counts()
	if f2 != baseFiles || m2-baseMetadata != projects || second.Unchanged != projects {
		t.Fatalf("second=%+v metadata=%d files=%d", second, m2-baseMetadata, f2-baseFiles)
	}
	t.Logf("day-2 refresh of %d unchanged packages: %d requests / %d bytes (baseline %d requests / %d bytes)",
		projects, m2-baseMetadata, second.DownloadedBytes, baseMetadata+baseFiles, baseBytes)
	if second.DownloadedBytes*2 >= baseBytes {
		t.Fatalf("downloaded %d bytes, expected under half of the baseline %d", second.DownloadedBytes, baseBytes)
	}

	// Day 4: p07 releases and the feed announces it; p21 releases but the feed misses
	// it. The announced package is read first; the rotation finds the other.
	offset.Store(4)
	fake.release("p07", "2.0.0")
	fake.announce("p07", "2.0.0")
	fake.release("p21", "2.0.0")
	third, err := executePass(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	versions := readRows(t, config.ObservationsPath)
	if versions["p07"] != "2.0.0" {
		t.Fatalf("an announced release must be picked up: %v", versions["p07"])
	}
	// The rotation reaches the missed release within a few passes (one pass may spend
	// its budget on other due entries); that is the backstop's guarantee.
	for range 3 {
		if versions["p21"] == "2.0.0" {
			break
		}
		if _, err := executePass(t.Context(), config); err != nil {
			t.Fatal(err)
		}
		versions = readRows(t, config.ObservationsPath)
	}
	if versions["p21"] != "2.0.0" {
		t.Fatalf("the rotation backstop must find a release the feed missed: %v", versions["p21"])
	}
	if third.FeedWorks != 1 || third.FeedEnqueued != 1 {
		t.Fatalf("third=%+v", third)
	}
	// Day 5: everything was checked yesterday and backed off, except the two packages
	// whose version changed (their streak restarted). The rotation spends its requests
	// only on those; the rest advance the cursor without a request.
	offset.Store(5)
	m4, _ := fake.counts()
	var fourth gocrawl.PassReport
	skipped := uint64(0)
	for range 2 {
		fourth, err = executePass(t.Context(), config)
		if err != nil {
			t.Fatal(err)
		}
		skipped += fourth.Skipped
		if fourth.FeedError != "" || second.FeedError != "" {
			t.Fatalf("feed errors: %q %q", second.FeedError, fourth.FeedError)
		}
	}
	m5, _ := fake.counts()
	if m5-m4 > 2 || skipped < projects-3 {
		t.Fatalf("metadata %d -> %d, skipped %d: not-due packages must cost no request", m4, m5, skipped)
	}
	// History and cache are split: the state a publisher stores carries the feed
	// position but no check; the checks live in the schedule cache.
	document, _, err := gocrawl.ReadStateDocument(config.StatePath)
	if err != nil {
		t.Fatal(err)
	}
	var sources map[string]map[string]any
	_ = json.Unmarshal(document["sources"], &sources)
	if _, leaked := sources["pypi"]["checked"]; leaked || sources["pypi"]["feed_cursor"] == "" {
		t.Fatalf("state=%v", sources["pypi"])
	}
	cachePath := gocrawl.DefaultCachePath(config.StatePath, "pypi")
	cache, warm, err := gocrawl.ReadCache(cachePath, gocrawl.ExtractionRevision)
	if err != nil || !warm || len(cache.Checks) != projects {
		t.Fatalf("cache: warm=%v checks=%d err=%v", warm, len(cache.Checks), err)
	}

	// An all-unchanged stretch of days re-checks packages (they are due) but writes
	// nothing to the history: the state directory and the rows stay byte-identical.
	stateBefore, rowsBefore := readTree(t, config.StatePath), readTree(t, config.ObservationsPath)
	cacheBefore := readTree(t, cachePath)
	requestsBefore, _ := fake.counts()
	for _, day := range []int64{40, 80, 120} {
		offset.Store(day)
		for range 2 {
			if _, err := executePass(t.Context(), config); err != nil {
				t.Fatal(err)
			}
		}
	}
	if requestsAfter, _ := fake.counts(); requestsAfter == requestsBefore {
		t.Fatal("the unchanged days must still check packages")
	}
	if !maps.Equal(stateBefore, readTree(t, config.StatePath)) {
		t.Fatal("an all-unchanged run must leave the state directory byte-identical")
	}
	if !maps.Equal(rowsBefore, readTree(t, config.ObservationsPath)) {
		t.Fatal("an all-unchanged run must leave the rows byte-identical")
	}
	if maps.Equal(cacheBefore, readTree(t, cachePath)) {
		t.Fatal("the checks were recorded in the cache")
	}

	// A lost cache is a cold start: every package is due again, history is untouched.
	if err := os.Remove(cachePath); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(config.DatabasePath); err != nil {
		t.Fatal(err)
	}
	offset.Store(121)
	coldBefore, _ := fake.counts()
	for range 3 {
		if _, err := executePass(t.Context(), config); err != nil {
			t.Fatal(err)
		}
	}
	if coldAfter, _ := fake.counts(); coldAfter-coldBefore < projects {
		t.Fatalf("a cold cache must re-check every package: %d requests", coldAfter-coldBefore)
	}
	if !maps.Equal(rowsBefore, readTree(t, config.ObservationsPath)) {
		t.Fatal("a cold start must not alter the rows")
	}

	// One package releases: only its rows change. The state (everything but rows) is
	// byte-identical because a version change needs no state write either.
	fake.release("p03", "9.0.0")
	offset.Store(200)
	for range 3 {
		if _, err := executePass(t.Context(), config); err != nil {
			t.Fatal(err)
		}
	}
	changed := readRows(t, config.ObservationsPath)
	if changed["p03"] != "9.0.0" {
		t.Fatalf("the released package must be picked up: %v", changed["p03"])
	}
	before, after := strings.Split(rowsBefore["."], "\n"), strings.Split(readTree(t, config.ObservationsPath)["."], "\n")
	differing := 0
	for _, line := range after {
		if !slices.Contains(before, line) {
			differing++
			if !strings.Contains(line, `"p03"`) {
				t.Fatalf("a row other than p03 changed: %s", line)
			}
		}
	}
	// Only the history generation counter moves (manifest summary and source.json);
	// no shard of a map is rewritten.
	stateAfter := readTree(t, config.StatePath)
	for name, body := range stateAfter {
		if body != stateBefore[name] && name != "manifest.json" && name != filepath.Join("pypi", "source.json") {
			t.Fatalf("state file %s changed for a single released package", name)
		}
	}
	if differing == 0 {
		t.Fatal("expected the released package's row to change")
	}
}

// readTree returns the bytes of a file or of every file under a directory, by relative path.
func readTree(t *testing.T, root string) map[string]string {
	t.Helper()
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

type atomic64 struct {
	mu    sync.Mutex
	value int64
}

func (a *atomic64) Load() int64 { a.mu.Lock(); defer a.mu.Unlock(); return a.value }
func (a *atomic64) Store(v int64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.value = v
}

// TestPackagesWithoutCommandsAreSkippedAtTheirSecondVisit replays the common case: most
// of a registry ships no command. Their first visit reads the artifact; every later
// visit that finds the same version costs the metadata request, or nothing when the
// package is not due, and none of it touches the history.
func TestPackagesWithoutCommandsAreSkippedAtTheirSecondVisit(t *testing.T) {
	const projects, withCommands = 40, 10
	day := time.Date(2026, 10, 1, 3, 0, 0, 0, time.UTC)
	var offset atomic64
	defer func(previous func() time.Time) { clock = previous }(clock)
	clock = func() time.Time { return day.Add(time.Duration(offset.Load()) * 24 * time.Hour) }
	fake, server := newFakePyPI(t, projects, clock)
	names := slices.Sorted(maps.Keys(fake.versions))
	for _, name := range names[withCommands:] {
		fake.plain[name] = true
	}
	directory := t.TempDir()
	config := crawlConfig{
		Source: "pypi", StatePath: filepath.Join(directory, "registry-state"),
		ObservationsPath: filepath.Join(directory, "pypi.jsonl"), ReportPath: filepath.Join(directory, "report.json"),
		CatalogPath: filepath.Join(directory, "projects.txt"), DatabasePath: filepath.Join(directory, "crawl.db"),
		RegistryURL: server.URL, FeedURL: server.URL, PackageBudget: projects, ByteBudget: 1 << 30, Workers: 4,
		MaxInFlight: 8, CommitBatch: 8, RequestTimeout: 2 * time.Second, ModuleTimeout: 5 * time.Second,
	}
	if err := os.WriteFile(config.CatalogPath, []byte(strings.Join(names, "\n")+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(config.StatePath+".json", []byte(`{"version":1,"sources":{"pypi":{"cursor":0,"catalog_complete":true}}}`), 0o644); err != nil {
		t.Fatal(err)
	}
	cachePath := gocrawl.DefaultCachePath(config.StatePath, "pypi")

	walk, err := executePass(t.Context(), config)
	if err != nil || walk.Processed != projects {
		t.Fatalf("walk=%+v err=%v", walk, err)
	}
	if walk.ReadNoCommands != projects-withCommands || walk.ReadWithCommands != withCommands {
		t.Fatalf("first visit read %d without and %d with commands", walk.ReadNoCommands, walk.ReadWithCommands)
	}
	cache, warm, err := gocrawl.ReadCache(cachePath, gocrawl.ExtractionRevision)
	if err != nil || !warm || len(cache.Checks) != projects {
		t.Fatalf("every inspected package, with or without commands, must be in the cache: warm=%v checks=%d err=%v", warm, len(cache.Checks), err)
	}
	outcomes := map[string]int{}
	for _, value := range cache.Checks {
		check, _ := gocrawl.ParseCheck(value)
		outcomes[check.Outcome]++
	}
	if outcomes[gocrawl.NoCommands] != projects-withCommands || outcomes[gocrawl.HasCommands] != withCommands {
		t.Fatalf("outcomes %v", outcomes)
	}
	m0, f0 := fake.counts()
	historyBefore := readTree(t, config.StatePath)

	// Second visit, two days later: the metadata request only, for CLI and non-CLI alike.
	offset.Store(2)
	config.Continuous = true
	second, err := executePass(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	m1, f1 := fake.counts()
	if f1 != f0 || m1-m0 != projects || second.Unchanged != projects {
		t.Fatalf("second=%+v metadata=%d wheel downloads=%d", second, m1-m0, f1-f0)
	}
	// Only the feed position (first poll) may move; no package shard or row does.
	historyAfter := readTree(t, config.StatePath)
	for name, body := range historyAfter {
		if body != historyBefore[name] && name != "manifest.json" && name != filepath.Join("pypi", "source.json") {
			t.Fatalf("an all-unchanged visit rewrote %s", name)
		}
	}
	historyBefore = historyAfter

	// The same day again: nothing is due, so not even the metadata request is made.
	third, err := executePass(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	m2, f2 := fake.counts()
	if m2 != m1 || f2 != f1 || third.Unchanged != 0 {
		t.Fatalf("third=%+v metadata=%d wheel downloads=%d", third, m2-m1, f2-f1)
	}
	if !reflect.DeepEqual(historyBefore, readTree(t, config.StatePath)) {
		t.Fatal("a run that finds nothing due must leave the history byte-identical")
	}

	// A lost cache costs one more full read of what history cannot recall: the packages
	// without commands. Packages with rows are seeded from history and stay cheap.
	if err := os.Remove(cachePath); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(config.DatabasePath); err != nil { // a CI runner starts without the working database too
		t.Fatal(err)
	}
	offset.Store(3)
	cold, err := executePass(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	_, f3 := fake.counts()
	if cold.Unchanged != withCommands || cold.ReadNoCommands != projects-withCommands || f3-f2 == 0 || f3-f2 >= f0 {
		t.Fatalf("cold cache: unchanged=%d (want %d, seeded from rows) read=%d (want %d) wheel requests=%d (first visit %d)",
			cold.Unchanged, withCommands, cold.ReadNoCommands, projects-withCommands, f3-f2, f0)
	}
}

func TestRotationOwnershipSplitsTheCatalogueBetweenBoosterAndActions(t *testing.T) {
	catalog := filepath.Join(t.TempDir(), "names.txt")
	var names strings.Builder
	all := make([]string, 0, 600)
	for index := range 600 {
		all = append(all, fmt.Sprintf("pkg-%03d", index))
		names.WriteString(all[index] + "\n")
	}
	if err := os.WriteFile(catalog, []byte(names.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	before := gocrawl.Snapshot{ImportSnapshot: gocrawl.ImportSnapshot{CatalogSize: 600, Cursor: 600, CatalogComplete: true}}
	visited := func(config crawlConfig) map[string]bool {
		owns, err := ownership(config)
		if err != nil {
			t.Fatal(err)
		}
		works, err := planPassWorks(catalog, before, 600, true, passPolicy{Owns: owns, MaxDays: 60,
			Checks: func([]string) (map[string]gocrawl.Check, error) { return nil, nil }})
		if err != nil {
			t.Fatal(err)
		}
		seen := map[string]bool{}
		for _, work := range works {
			if !work.Refresh {
				t.Fatalf("not a rotation entry: %+v", work)
			}
			if !work.Skip {
				seen[work.Module] = true
			}
		}
		return seen
	}
	booster := visited(crawlConfig{RotationInclude: "0-99,200-255"})
	actions := visited(crawlConfig{RotationExclude: "0-99,200-255"})
	everything := visited(crawlConfig{})
	if len(everything) != 600 {
		t.Fatalf("no declaration must visit everything, got %d", len(everything))
	}
	for _, name := range all {
		if booster[name] == actions[name] {
			t.Fatalf("%s must be visited by exactly one of booster (%v) and actions (%v)", name, booster[name], actions[name])
		}
		if want := gocrawl.OwnerBucket(name) <= 99 || gocrawl.OwnerBucket(name) >= 200; booster[name] != want {
			t.Fatalf("%s: booster=%v, bucket %d", name, booster[name], gocrawl.OwnerBucket(name))
		}
	}
	if len(booster) == 0 || len(actions) == 0 {
		t.Fatalf("both sides must get work: %d / %d", len(booster), len(actions))
	}
	if _, err := ownership(crawlConfig{RotationExclude: "9-3"}); err == nil {
		t.Fatal("a bad range must be an error, not silently ignored")
	}
}

func TestJitteredPauseStaysWithinTheFraction(t *testing.T) {
	for range 200 {
		got := jittered(10*time.Second, 0.3)
		if got < 7*time.Second || got > 13*time.Second {
			t.Fatalf("%v", got)
		}
	}
	if jittered(10*time.Second, 0) != 10*time.Second {
		t.Fatal("no jitter unless asked")
	}
}
