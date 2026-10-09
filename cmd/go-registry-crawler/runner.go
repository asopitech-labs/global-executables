package main

import (
	"bufio"
	"cmp"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
	"github.com/asopitech-labs/global-executables/internal/goproxy"
	"github.com/asopitech-labs/global-executables/internal/registryinspect"
)

// clock is the pass clock; tests replace it to move a run to a later day.
var clock = time.Now

type crawlConfig struct {
	Source           string
	StatePath        string
	ObservationsPath string
	ReportPath       string
	CatalogPath      string
	DatabasePath     string
	// CachePath is the schedule cache (checks and rotation position): never committed,
	// safe to lose. See docs/OPERATIONS.md "History and cache".
	CachePath       string
	ProxyURL        string
	IndexURL        string
	RegistryURL     string
	FeedURL         string
	registryDefault bool
	CatalogPages    int
	PackageBudget   int
	ByteBudget      int64
	Workers         int
	MaxInFlight     int
	CommitBatch     int
	RequestTimeout  time.Duration
	ModuleTimeout   time.Duration
	Continuous      bool
}

// passPolicy carries what the change-driven refresh adds to a pass: the checks that
// decide which rotation entries are due, and the modules a change feed announced.
type passPolicy struct {
	Checks   func([]string) (map[string]gocrawl.Check, error)
	Today    int
	DueFloor int
	MaxDays  int
	Feed     []string
}

func buildPassWorks(catalogPath string, before gocrawl.Snapshot, budget int, refresh bool) ([]gocrawl.ModuleWork, error) {
	return planPassWorks(catalogPath, before, budget, refresh, passPolicy{})
}

// planPassWorks orders a pass: announced changes first, then the catalog walk, then
// the rotation backstop, then retries. The rotation always keeps a tenth of the
// budget once the walk is complete, however many changes a feed announces, and the
// walk keeps half of it while the catalog is still being read.
func planPassWorks(catalogPath string, before gocrawl.Snapshot, budget int, refresh bool, policy passPolicy) ([]gocrawl.ModuleWork, error) {
	if budget <= 0 {
		return nil, nil
	}
	retryModules := slices.Sorted(maps.Keys(before.Retries))
	retryBudget := min(len(retryModules), max(1, budget/4))
	remaining := budget - retryBudget
	walking := before.Cursor < before.CatalogSize
	feedUse := 0
	if len(policy.Feed) > 0 {
		share := remaining / 2
		if !walking {
			share = remaining
			if refresh && before.CatalogSize > 0 {
				share -= max(1, budget/10)
			}
		}
		feedUse = max(0, min(len(policy.Feed), share))
	}
	catalogBudget := remaining - feedUse
	var works []gocrawl.ModuleWork
	for _, module := range policy.Feed[:feedUse] {
		works = append(works, gocrawl.ModuleWork{Module: module, Feed: true, Attempt: 1})
	}
	var walk []gocrawl.ModuleWork
	if walking {
		var err error
		walk, err = gocrawl.ReadCatalogBatch(catalogPath, before.Cursor, before.CatalogOffset, catalogBudget, 0)
		if err != nil {
			return nil, err
		}
	}
	works = append(works, walk...)
	if refresh && len(walk) < catalogBudget && before.Cursor >= before.CatalogSize && before.CatalogSize > 0 {
		refreshWorks, err := planRefresh(catalogPath, before, catalogBudget-len(walk), policy)
		if err != nil {
			return nil, err
		}
		works = append(works, refreshWorks...)
	}
	for _, module := range retryModules[:retryBudget] {
		if len(works) >= budget {
			break
		}
		entry := before.Retries[module]
		works = append(works, gocrawl.ModuleWork{
			Order: uint64(len(works)), Module: module, Retry: true, Attempt: entry.Attempts + 1,
		})
	}
	if err := attachKnown(works, policy); err != nil {
		return nil, err
	}
	for index := range works {
		works[index].Order = uint64(index)
	}
	return works, nil
}

// planRefresh reads the next rotation entries. With checks available it reads further
// than the budget and marks entries that are not due as skipped: they advance the
// rotation cursor without a request, so the budget is spent on packages that are due.
func planRefresh(catalogPath string, before gocrawl.Snapshot, target int, policy passPolicy) ([]gocrawl.ModuleWork, error) {
	refreshCursor, refreshOffset := before.RefreshCursor, before.RefreshCatalogOffset
	if refreshCursor >= before.CatalogSize {
		refreshCursor, refreshOffset = 0, 0
	}
	scan := target
	if policy.Checks != nil {
		scan = target * gocrawl.MaxScanFactor
	}
	batch, err := gocrawl.ReadCatalogBatch(catalogPath, refreshCursor, refreshOffset, scan, 0)
	if err != nil {
		return nil, err
	}
	var checks map[string]gocrawl.Check
	if policy.Checks != nil {
		modules := make([]string, len(batch))
		for index, work := range batch {
			modules[index] = work.Module
		}
		if checks, err = policy.Checks(modules); err != nil {
			return nil, err
		}
	}
	due, cut := 0, len(batch)
	for index := range batch {
		batch[index].Refresh = true
		if check, recorded := checks[batch[index].Module]; recorded &&
			!check.Due(batch[index].Module, policy.Today, policy.DueFloor, policy.MaxDays) {
			batch[index].Skip = true
			continue
		}
		due++
		if due >= target {
			cut = index + 1
			break
		}
	}
	return batch[:cut], nil
}

// attachKnown records on each work the version its last check found.
func attachKnown(works []gocrawl.ModuleWork, policy passPolicy) error {
	if policy.Checks == nil || len(works) == 0 {
		return nil
	}
	modules := make([]string, 0, len(works))
	for _, work := range works {
		if !work.Skip {
			modules = append(modules, work.Module)
		}
	}
	checks, err := policy.Checks(modules)
	if err != nil {
		return err
	}
	for index := range works {
		if check, recorded := checks[works[index].Module]; recorded && !works[index].Skip {
			works[index].Known = check.Version
		}
	}
	return nil
}

func (c *crawlConfig) applyDefaults() error {
	if c.Source == "" {
		c.Source = "go"
	}
	defaults := map[string]struct {
		catalog, database, observations, registry string
		workers                                   int
		requestTimeout, packageTimeout            time.Duration
	}{
		// Go reads whole module archives rather than a metadata document, and its
		// catalog is already complete, so the widest modules get a generous budget
		// instead of timing out and retiring while the crawl only refreshes.
		"go":        {"data/production/go-modules.txt", "data/production/go-crawl.db", "data/production/intermediate/go.jsonl", "https://proxy.golang.org", 32, 45 * time.Second, 10 * time.Minute},
		"npm":       {"data/production/npm-critical-packages.txt", "data/production/npm-crawl.db", "data/production/intermediate/npm.jsonl", "https://registry.npmjs.org", 64, 45 * time.Second, 5 * time.Minute},
		"pypi":      {"data/production/pypi-projects.txt", "data/production/pypi-crawl.db", "data/production/intermediate/pypi.jsonl", "https://pypi.org", 24, 45 * time.Second, 2 * time.Minute},
		"rubygems":  {"data/production/rubygems-names.txt", "data/production/rubygems-crawl.db", "data/production/intermediate/rubygems.jsonl", "https://rubygems.org", 16, 45 * time.Second, 2 * time.Minute},
		"packagist": {"data/production/packagist-packages.txt", "data/production/packagist-crawl.db", "data/production/intermediate/packagist.jsonl", "https://repo.packagist.org", 24, 45 * time.Second, 2 * time.Minute},
	}
	selected, exists := defaults[c.Source]
	if !exists {
		return fmt.Errorf("unsupported source %q", c.Source)
	}
	if c.StatePath == "" {
		c.StatePath = "data/production/registry-state"
	}
	if c.ReportPath == "" {
		c.ReportPath = "reports/registry-artifact-crawl.json"
	}
	if c.CatalogPath == "" {
		c.CatalogPath = selected.catalog
	}
	if c.DatabasePath == "" {
		c.DatabasePath = selected.database
	}
	if c.CachePath == "" {
		c.CachePath = gocrawl.DefaultCachePath(c.StatePath, c.Source)
	}
	if c.ObservationsPath == "" {
		c.ObservationsPath = selected.observations
	}
	if c.RegistryURL == "" {
		c.RegistryURL = selected.registry
		c.registryDefault = true
	}
	if c.ProxyURL == "" {
		c.ProxyURL = "https://proxy.golang.org"
	}
	if c.IndexURL == "" {
		c.IndexURL = "https://index.golang.org/index"
	}
	if c.Workers <= 0 {
		c.Workers = selected.workers
	}
	if c.RequestTimeout <= 0 {
		c.RequestTimeout = selected.requestTimeout
	}
	if c.ModuleTimeout <= 0 {
		c.ModuleTimeout = selected.packageTimeout
	}
	return nil
}

type crawlAdapter struct {
	profile   gocrawl.CompatibilityProfile
	inspector gocrawl.Inspector
	refresh   func(context.Context, string, *gocrawl.BoltStore) (gocrawl.CatalogRefreshReport, error)
	metrics   func() registryinspect.Metrics
	// feed announces changes since the last poll; nil when the registry has no
	// complete feed. feedFloor is how long to wait after an announcement before
	// reading the registry, and maxBackoffDays bounds the rotation's skipping.
	feed           gocrawl.Feed
	feedOptions    gocrawl.FeedOptions
	feedFloor      time.Duration
	maxBackoffDays int
}

func buildAdapter(config crawlConfig) (crawlAdapter, error) {
	staticCatalog := func(ctx context.Context, path string, store *gocrawl.BoltStore) (gocrawl.CatalogRefreshReport, error) {
		if err := store.SyncCatalog(ctx, path); err != nil {
			return gocrawl.CatalogRefreshReport{}, err
		}
		progress, err := store.Progress(ctx)
		return gocrawl.CatalogRefreshReport{Complete: progress.CatalogComplete}, err
	}
	initialHostConcurrency := config.Workers
	if config.Source == "npm" {
		initialHostConcurrency = min(8, config.Workers)
	}
	registryConfig := registryinspect.Config{
		BaseURL: config.RegistryURL, RequestTimeout: config.RequestTimeout, PackageTimeout: config.ModuleTimeout,
		InitialHostConcurrency: initialHostConcurrency, MaxHostConcurrency: config.Workers,
	}
	if config.Source == "npm" {
		// This runner's egress path received Retry-After under an unpaced burst. A
		// burst-free 200ms floor avoids repeating that local observation; it is not
		// a global npm quota shared by other runners.
		registryConfig.MinRequestInterval = 200 * time.Millisecond
	}
	switch config.Source {
	case "go":
		packageIndexURL, cachedOnlyURL := "", ""
		if config.ProxyURL == "https://proxy.golang.org" {
			packageIndexURL = "https://pkg.go.dev"
			cachedOnlyURL = config.ProxyURL + "/cached-only"
		}
		return crawlAdapter{
			profile: gocrawl.CompatibilityProfileFor("go"),
			inspector: goproxy.NewInspector(goproxy.Config{
				BaseURL: config.ProxyURL, PackageIndexURL: packageIndexURL, CachedOnlyURL: cachedOnlyURL,
				PackageIndexInterval: 25 * time.Millisecond,
				RequestTimeout:       config.RequestTimeout, ModuleTimeout: config.ModuleTimeout,
			}),
			refresh: func(ctx context.Context, path string, store *gocrawl.BoltStore) (gocrawl.CatalogRefreshReport, error) {
				return gocrawl.RefreshCatalog(ctx, path, store,
					goproxy.NewIndexClient(config.IndexURL, goproxy.Config{RequestTimeout: config.RequestTimeout}),
					gocrawl.CatalogRefreshOptions{MaxPages: config.CatalogPages, PageSize: 2000})
			},
			metrics:        func() registryinspect.Metrics { return registryinspect.Metrics{} },
			feedFloor:      time.Minute,
			maxBackoffDays: gocrawl.BackoffMaxDaysFeed,
		}, nil
	case "npm":
		inspector := registryinspect.NewNPMInspector(registryConfig)
		catalogNames, err := loadNameSet(config.CatalogPath)
		if err != nil {
			return crawlAdapter{}, err
		}
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("npm"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics,
			feed: registryinspect.NewNPMFeed(registryConfig, config.FeedURL),
			// npm's scope is the critical set: announcements for other packages are ignored.
			feedOptions: gocrawl.FeedOptions{Known: func(name string) bool { _, in := catalogNames[name]; return in }},
			feedFloor:   5 * time.Minute, maxBackoffDays: gocrawl.BackoffMaxDaysFeed}, nil
	case "pypi":
		inspector := registryinspect.NewPyPIInspector(registryConfig)
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("pypi"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics,
			feed:        registryinspect.NewPyPIFeed(registryConfig),
			feedOptions: gocrawl.FeedOptions{AdmitUnknown: true},
			feedFloor:   time.Minute, maxBackoffDays: gocrawl.BackoffMaxDaysFeed}, nil
	case "rubygems":
		inspector := registryinspect.NewRubyGemsInspector(registryConfig)
		// RubyGems has no complete feed (just_updated lists only the newest 50 gems), so
		// the rotation stays the only trigger and its skipping is bounded tighter.
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("rubygems"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics, maxBackoffDays: gocrawl.BackoffMaxDaysPlain}, nil
	case "packagist":
		inspector := registryinspect.NewPackagistInspector(registryConfig)
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("packagist"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics,
			feed:        registryinspect.NewPackagistFeed(registryConfig, config.FeedURL),
			feedOptions: gocrawl.FeedOptions{AdmitUnknown: true},
			feedFloor:   time.Minute, maxBackoffDays: gocrawl.BackoffMaxDaysFeed}, nil
	default:
		return crawlAdapter{}, fmt.Errorf("unsupported source %q", config.Source)
	}
}

type measuringCommitter struct {
	store      *gocrawl.BoltStore
	byteBudget int64
	cancel     context.CancelFunc
	processed  uint64
	refreshed  uint64
	records    uint64
	unchanged  uint64
	skipped    uint64
	feedWorks  uint64
	downloaded uint64
	exhausted  bool
}

type passExecutor func(context.Context, crawlConfig) (gocrawl.PassReport, error)

func executeLoop(
	ctx context.Context,
	config crawlConfig,
	passes int,
	pause time.Duration,
	output io.Writer,
	execute passExecutor,
) error {
	for pass := 1; ; pass++ {
		report, err := execute(ctx, config)
		_, _ = fmt.Fprintf(output, "pass=%d processed=%d records=%d downloaded=%d\n", pass, report.Processed, report.Records, report.DownloadedBytes)
		if err != nil {
			return err
		}
		if report.Complete && !config.Continuous {
			_, _ = fmt.Fprintln(output, "catalog is exhaustive")
			return nil
		}
		if passes > 0 && pass >= passes {
			return nil
		}
		if pause <= 0 {
			continue
		}
		timer := time.NewTimer(pause)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}

func (c *measuringCommitter) Commit(ctx context.Context, results []gocrawl.ModuleResult) error {
	if err := c.store.Commit(ctx, results); err != nil {
		return err
	}
	c.processed += uint64(len(results))
	for _, result := range results {
		if result.Work.Refresh {
			c.refreshed++
		}
		c.records += uint64(len(result.Observations))
		switch {
		case result.Work.Skip:
			c.skipped++
		case result.Unchanged:
			c.unchanged++
		}
		if result.Work.Feed {
			c.feedWorks++
		}
		if result.DownloadedBytes > 0 {
			c.downloaded += uint64(result.DownloadedBytes)
		}
	}
	if c.byteBudget > 0 && c.downloaded >= uint64(c.byteBudget) {
		c.exhausted = true
		c.cancel()
	}
	return nil
}

func executePass(ctx context.Context, config crawlConfig) (gocrawl.PassReport, error) {
	started := time.Now().UTC()
	if err := config.applyDefaults(); err != nil {
		return gocrawl.PassReport{}, err
	}
	if config.PackageBudget <= 0 {
		config.PackageBudget = 100
	}
	if config.MaxInFlight < config.Workers {
		config.MaxInFlight = config.Workers * 4
	}
	if config.CommitBatch <= 0 {
		config.CommitBatch = 32
	}
	if config.CatalogPages <= 0 {
		config.CatalogPages = 10
	}
	adapter, err := buildAdapter(config)
	if err != nil {
		return gocrawl.PassReport{}, err
	}

	store, err := gocrawl.OpenBoltStore(config.DatabasePath, gocrawl.StoreOptions{FailureAttemptLimit: 3, Now: clock})
	if err != nil {
		return gocrawl.PassReport{}, err
	}
	defer store.Close()
	initialized, err := store.Initialized(ctx)
	if err != nil {
		return gocrawl.PassReport{}, err
	}
	var document gocrawl.StateDocument
	if initialized {
		document, err = gocrawl.LoadStateDocument(config.StatePath)
	} else {
		var imported gocrawl.ImportSnapshot
		imported, document, err = gocrawl.LoadSourceCompatibility(config.StatePath, config.ObservationsPath, config.CatalogPath, adapter.profile)
		if err == nil {
			applyScheduleCache(&imported, config, clock())
			err = store.Import(ctx, imported)
		}
	}
	if err != nil {
		return gocrawl.PassReport{}, err
	}
	catalogReport, catalogErr := adapter.refresh(ctx, config.CatalogPath, store)
	if errors.Is(catalogErr, context.Canceled) || errors.Is(catalogErr, context.DeadlineExceeded) {
		return gocrawl.PassReport{}, catalogErr
	}
	// A feed poll and the queue it fills only run in the continuous refresh; a failed
	// poll leaves the cursor where it was and the next run replays it.
	var feedReport gocrawl.FeedReport
	var feedModules []string
	if adapter.feed != nil && config.Continuous && (config.registryDefault || config.FeedURL != "") {
		feedReport, err = gocrawl.PollFeed(ctx, store, adapter.feed, adapter.feedOptions)
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			return gocrawl.PassReport{}, err
		}
		feedModules, _, err = store.PendingFeed(ctx, clock(), adapter.feedFloor, config.PackageBudget)
		if err != nil {
			return gocrawl.PassReport{}, err
		}
	}
	before, err := store.Progress(ctx)
	if err != nil {
		return gocrawl.PassReport{}, err
	}
	policy := passPolicy{Feed: feedModules}
	if config.Continuous {
		dueFloor, floorErr := store.DueFloor(ctx)
		if floorErr != nil {
			return gocrawl.PassReport{}, floorErr
		}
		policy.Today, policy.DueFloor = gocrawl.Today(clock()), dueFloor
		policy.MaxDays = cmp.Or(adapter.maxBackoffDays, gocrawl.BackoffMaxDaysPlain)
	}
	policy.Checks = func(modules []string) (map[string]gocrawl.Check, error) { return store.Checks(ctx, modules) }
	works, err := planPassWorks(config.CatalogPath, before, config.PackageBudget, config.Continuous, policy)
	if err != nil {
		return gocrawl.PassReport{}, err
	}

	passCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	committer := &measuringCommitter{
		store: store, byteBudget: config.ByteBudget, cancel: cancel,
		downloaded: catalogReport.DownloadedBytes,
	}
	coordinator := gocrawl.Coordinator{
		Workers: config.Workers, MaxInFlight: config.MaxInFlight, CommitBatch: config.CommitBatch,
	}
	runErr := coordinator.Run(passCtx, works, adapter.inspector, committer)
	if committer.exhausted && errors.Is(runErr, context.Canceled) {
		runErr = nil
	}
	metrics := adapter.metrics()
	report := gocrawl.PassReport{
		StartedAt: started, FinishedAt: time.Now().UTC(), Processed: committer.processed,
		Records: committer.records, DownloadedBytes: committer.downloaded,
		BudgetExhausted: committer.exhausted, Interrupted: ctx.Err() != nil, Workers: config.Workers,
		CatalogDiscovered: catalogReport.Discovered, CatalogRequests: catalogReport.Requests,
		PackageBudget: config.PackageBudget, Refreshed: committer.refreshed, Requests: metrics.Requests,
		RateLimited: metrics.RateLimited, Timeouts: metrics.Timeouts,
		CircuitOpens: metrics.CircuitOpens, HostConcurrency: metrics.HostConcurrency,
		Unchanged: committer.unchanged, Skipped: committer.skipped, FeedWorks: committer.feedWorks,
		FeedEvents: uint64(feedReport.Events), FeedEnqueued: uint64(feedReport.Enqueued),
		FeedRequests: uint64(feedReport.Requests), FeedBytes: uint64(feedReport.DownloadedBytes),
		FeedResync: feedReport.Resync, FeedError: feedReport.Error,
	}
	if catalogErr != nil {
		report.CatalogError = catalogErr.Error()
	}
	if runErr != nil && !errors.Is(runErr, context.Canceled) {
		report.Error = runErr.Error()
	}
	afterProgress, progressErr := store.Progress(context.Background())
	if progressErr != nil {
		return gocrawl.PassReport{}, progressErr
	}
	report.Complete = catalogErr == nil && afterProgress.CatalogComplete &&
		afterProgress.Cursor >= afterProgress.CatalogSize && len(afterProgress.Retries) == 0
	// Bookkeeping goes to the cache only; losing it costs requests, never history.
	if checks, checksErr := store.AllChecks(context.Background()); checksErr == nil {
		cache := gocrawl.Cache{Checks: checks, Cursor: afterProgress.RefreshCursor}
		if err := gocrawl.WriteCache(config.CachePath, cache, gocrawl.ExtractionRevision); err != nil {
			fmt.Fprintf(os.Stderr, "schedule cache not saved: %v\n", err)
		}
	}
	if err := gocrawl.ExportSourceStoreCompatibility(context.Background(), gocrawl.ExportPaths{
		State: config.StatePath, Observations: config.ObservationsPath, Report: config.ReportPath,
	}, document, store, report, adapter.profile); err != nil {
		return report, err
	}
	if runErr != nil {
		return report, runErr
	}
	return report, nil
}

// applyScheduleCache fills what the state no longer carries: the checks and the rotation
// position. A missing, stale or unreadable cache is a cold start: checks come from the
// stored rows (due now, staggered) and the rotation begins at a time-derived position so
// repeated cold starts still sweep the whole catalog.
func applyScheduleCache(imported *gocrawl.ImportSnapshot, config crawlConfig, now time.Time) {
	cache, warm, err := gocrawl.ReadCache(config.CachePath, gocrawl.ExtractionRevision)
	if err != nil {
		fmt.Fprintf(os.Stderr, "schedule cache ignored: %v\n", err)
	}
	cursor := gocrawl.ColdRefreshStart(imported.CatalogSize, config.PackageBudget, now)
	if warm {
		if imported.Checks == nil {
			imported.Checks = map[string]string{}
		}
		maps.Copy(imported.Checks, cache.Checks)
		cursor = cache.Cursor
	}
	if cursor >= imported.CatalogSize {
		cursor = 0
	}
	offset, err := gocrawl.LocateCatalogOffset(config.CatalogPath, cursor)
	if err != nil {
		cursor, offset = 0, 0
	}
	imported.RefreshCursor, imported.RefreshCatalogOffset = cursor, offset
}

// loadNameSet reads a catalog file into a set of names.
func loadNameSet(path string) (map[string]struct{}, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	names := make(map[string]struct{})
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		if name := strings.TrimSpace(scanner.Text()); name != "" {
			names[name] = struct{}{}
		}
	}
	return names, scanner.Err()
}
