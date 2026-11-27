package main

import (
	"bufio"
	"cmp"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"math/rand/v2"
	"os"
	"slices"
	"strings"
	"sync/atomic"
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
	// Local booster controls (docs/OPERATIONS.md "Local booster"). RotationInclude limits
	// the rotation to buckets this machine owns, RotationExclude leaves buckets to a
	// booster whose lease is alive; both are "lo-hi,lo-hi" over gocrawl.OwnerBucket.
	// NoFeed leaves the change feed (and its cursor) to the Actions refresh.
	RotationInclude   string
	RotationExclude   string
	NoFeed            bool
	UserAgent         string
	MaxBytesPerSecond int64
	PauseJitter       float64
	// VerifyStore checks the whole transaction store once, before the first pass of this
	// process; a damaged one is quarantined and rebuilt from the exported state.
	VerifyStore bool
	// ProgressFile, when set, is touched whenever a commit contains a package that was
	// actually read from the registry (not skipped, not failed), so a supervisor can tell a
	// crawler that only times out from one that works.
	ProgressFile string
}

// passPolicy carries what the change-driven refresh adds to a pass: the checks that
// decide which rotation entries are due, and the modules a change feed announced.
type passPolicy struct {
	Checks   func([]string) (map[string]gocrawl.Check, error)
	Today    int
	DueFloor int
	MaxDays  int
	Feed     []string
	// Owns, when set, decides which rotation entries this run visits; the others advance
	// the cursor without a request.
	Owns func(module string) bool
	// FeedReader: this run reads the change feed, so it also owns the retries of packages
	// missing from the catalogue (see ownedRetries).
	FeedReader bool
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
	retryModules, err := ownedRetries(before.Retries, policy, catalogPath)
	if err != nil {
		return nil, err
	}
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
	scheduled := make(map[string]struct{}, feedUse)
	for _, module := range policy.Feed[:feedUse] {
		scheduled[module] = struct{}{}
	}
	// Only works that make a request count against the budget. The rotation also lists
	// the entries it skips (another owner's bucket, not yet due) so that its cursor
	// advances past them; with a booster holding every bucket, or a long unchanged
	// streak, those can be many times the budget and must not crowd the retries out.
	active := 0
	for _, work := range works {
		if !work.Skip {
			active++
		}
	}
	for _, module := range retryModules[:retryBudget] {
		if active >= budget {
			break
		}
		if _, ok := scheduled[module]; ok {
			continue // already read this pass as a feed work, which also counts the attempt
		}
		entry := before.Retries[module]
		works = append(works, gocrawl.ModuleWork{
			Order: uint64(len(works)), Module: module, Retry: true, Attempt: entry.Attempts + 1,
		})
		active++
	}
	if err := attachKnown(works, policy); err != nil {
		return nil, err
	}
	for index := range works {
		works[index].Order = uint64(index)
	}
	return works, nil
}

// recoveryRules name, per source, the failures a newer crawler handles: packages given up
// (unavailable) or waiting (retries) for such a reason are requeued with a fresh attempt
// count. Only those packages change; once rechecked their reason is a different one, so
// a rule matches nothing on later passes.
var recoveryRules = map[string]func(reason string) bool{
	// Project JSON over the 8 MiB metadata limit (pydantic_core, grpcio, ddtrace ...):
	// read since through the release document (registryinspect largeProject).
	"pypi": func(reason string) bool {
		return strings.Contains(reason, "bytes exceeds 8388608 bytes") || strings.Contains(reason, "response exceeded 8388608 bytes")
	},
}

// ownedRetries lists the retries this writer is responsible for, sorted. Ownership is
// decided per package so that exactly one writer retries it:
//   - no ownership filter (no booster): every retry is this writer's;
//   - a package the change feed announced this pass: the feed reader's (it reads it anyway);
//   - a package in the catalogue: the owner of its bucket (policy.Owns), as for the
//     rotation, so the booster retries its buckets and Actions the rest;
//   - a package missing from the catalogue (born from the change feed after the catalogue
//     was taken, e.g. libcuopt-*): the feed reader's. A booster only knows its catalogue and
//     reads no feed, so such a package would otherwise have no owner at all.
func ownedRetries(retries map[string]gocrawl.RetryEntry, policy passPolicy, catalogPath string) ([]string, error) {
	modules := slices.Sorted(maps.Keys(retries))
	if policy.Owns == nil || len(modules) == 0 {
		return modules, nil
	}
	listed, err := catalogMembers(catalogPath, modules)
	if err != nil {
		return nil, err
	}
	announced := make(map[string]struct{}, len(policy.Feed))
	for _, module := range policy.Feed {
		announced[module] = struct{}{}
	}
	owned := modules[:0]
	for _, module := range modules {
		_, isAnnounced := announced[module]
		_, inCatalog := listed[module]
		switch {
		case isAnnounced:
			owned = append(owned, module)
		case inCatalog && policy.Owns(module):
			owned = append(owned, module)
		case !inCatalog && policy.FeedReader:
			owned = append(owned, module)
		}
	}
	return owned, nil
}

// catalogMembers returns which of names the catalogue file lists (one name per line,
// optionally followed by a tab and more fields). It streams the file once.
func catalogMembers(catalogPath string, names []string) (map[string]struct{}, error) {
	wanted := make(map[string]struct{}, len(names))
	for _, name := range names {
		wanted[name] = struct{}{}
	}
	found := make(map[string]struct{}, len(names))
	file, err := os.Open(catalogPath)
	if errors.Is(err, os.ErrNotExist) {
		return found, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() && len(found) < len(wanted) {
		name, _, _ := strings.Cut(scanner.Text(), "\t")
		name = strings.TrimSpace(name)
		if _, ok := wanted[name]; ok {
			found[name] = struct{}{}
		}
	}
	return found, scanner.Err()
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
		if policy.Owns != nil && !policy.Owns(batch[index].Module) {
			batch[index].Skip = true
			continue
		}
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
		"packagist": {"data/production/packagist-packages.txt", "data/production/packagist-crawl.db", "data/production/intermediate/packagist.jsonl", "https://repo.packagist.org", 20, 45 * time.Second, 2 * time.Minute},
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
	registryConfig := registryClientConfig(config)
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
		// PyPI asks new integrations not to use its deprecated XML-RPC APIs. The
		// bounded exhaustive rotation is the supported polling strategy here: it
		// eventually revisits every project and does not depend on a lossy RSS window.
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("pypi"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics,
			maxBackoffDays: gocrawl.BackoffMaxDaysPlain}, nil
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

func registryClientConfig(config crawlConfig) registryinspect.Config {
	initialHostConcurrency := config.Workers
	if config.Source == "npm" {
		initialHostConcurrency = min(8, config.Workers)
	}
	registryConfig := registryinspect.Config{
		BaseURL: config.RegistryURL, RequestTimeout: config.RequestTimeout, PackageTimeout: config.ModuleTimeout,
		InitialHostConcurrency: initialHostConcurrency, MaxHostConcurrency: config.Workers,
		UserAgent: config.UserAgent, MaxBytesPerSecond: config.MaxBytesPerSecond,
	}
	if config.Source == "npm" {
		// This runner's egress path received Retry-After under an unpaced burst. A
		// burst-free 200ms floor avoids repeating that local observation; it is not
		// a global npm quota shared by other runners.
		registryConfig.MinRequestInterval = 200 * time.Millisecond
	}
	if config.Source == "rubygems" {
		// RubyGems publishes a 10 requests/second API limit. The requester gate is
		// shared by every worker, so this interval limits the whole process rather
		// than allowing each worker its own quota.
		registryConfig.MinRequestInterval = 100 * time.Millisecond
	}
	return registryConfig
}

type measuringCommitter struct {
	store                            *gocrawl.BoltStore
	byteBudget                       int64
	cancel                           context.CancelFunc
	processed                        uint64
	refreshed                        uint64
	records                          uint64
	unchanged                        uint64
	readNoCommands, readWithCommands uint64
	skipped                          uint64
	feedWorks                        uint64
	downloaded                       uint64
	exhausted                        bool
	progressFile                     string
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
		if config.ProgressFile != "" {
			touchProgress(config.ProgressFile) // a finished pass is progress, even one of skips
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
		timer := time.NewTimer(jittered(pause, config.PauseJitter))
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
	if c.progressFile != "" && madeProgress(results) {
		touchProgress(c.progressFile)
	}
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
		case result.Verdict == gocrawl.VerdictSuccess && len(result.Observations) == 0:
			c.readNoCommands++
		case result.Verdict == gocrawl.VerdictSuccess:
			c.readWithCommands++
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

	store, err := openStore(config)
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
	if rule := recoveryRules[config.Source]; rule != nil {
		requeued, err := store.Requeue(ctx, rule)
		if err != nil {
			return gocrawl.PassReport{}, err
		}
		if requeued > 0 {
			fmt.Fprintf(os.Stderr, "requeued %d %s packages that failed for a reason the crawler now handles\n", requeued, config.Source)
		}
	}
	catalogReport, catalogErr := adapter.refresh(ctx, config.CatalogPath, store)
	if errors.Is(catalogErr, context.Canceled) || errors.Is(catalogErr, context.DeadlineExceeded) {
		return gocrawl.PassReport{}, catalogErr
	}
	// A feed poll and the queue it fills only run in the continuous refresh; a failed
	// poll leaves the cursor where it was and the next run replays it.
	var feedReport gocrawl.FeedReport
	var feedModules []string
	readsFeed := adapter.feed != nil && config.Continuous && !config.NoFeed && (config.registryDefault || config.FeedURL != "")
	if readsFeed {
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
	owns, err := ownership(config)
	if err != nil {
		return gocrawl.PassReport{}, err
	}
	policy.Owns = owns
	policy.FeedReader = readsFeed
	policy.Checks = func(modules []string) (map[string]gocrawl.Check, error) { return store.Checks(ctx, modules) }
	works, err := planPassWorks(config.CatalogPath, before, config.PackageBudget, config.Continuous, policy)
	if err != nil {
		return gocrawl.PassReport{}, err
	}

	passCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	committer := &measuringCommitter{
		store: store, byteBudget: config.ByteBudget, cancel: cancel, progressFile: config.ProgressFile,
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
		Unchanged: committer.unchanged, Skipped: committer.skipped,
		ReadNoCommands: committer.readNoCommands, ReadWithCommands: committer.readWithCommands, FeedWorks: committer.feedWorks,
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

// ownership turns the include/exclude bucket ranges into the rotation filter, or nil when
// this run owns the whole catalogue.
func ownership(config crawlConfig) (func(string) bool, error) {
	include, err := gocrawl.ParseRanges(config.RotationInclude)
	if err != nil {
		return nil, fmt.Errorf("-rotation-include: %w", err)
	}
	exclude, err := gocrawl.ParseRanges(config.RotationExclude)
	if err != nil {
		return nil, fmt.Errorf("-rotation-exclude: %w", err)
	}
	if include.Empty() && exclude.Empty() {
		return nil, nil
	}
	return func(module string) bool {
		return (include.Empty() || include.Contains(module)) && !exclude.Contains(module)
	}, nil
}

// jittered spreads a pause by +-fraction so several machines do not poll in step.
func jittered(pause time.Duration, fraction float64) time.Duration {
	if fraction <= 0 || pause <= 0 {
		return pause
	}
	fraction = min(fraction, 0.9)
	return time.Duration(float64(pause) * (1 + fraction*(2*rand.Float64()-1)))
}

// storeVerified makes the (slow, full) store check run once per process, not every pass.
var storeVerified atomic.Bool

// openStore opens the transaction store. A store that is damaged (a torn copy, a lost
// write) is moved aside and a fresh one is opened: the pass then imports the exported
// state, observations and schedule cache, which are written atomically, so at most the
// work since the last completed pass is crawled again.
func openStore(config crawlConfig) (*gocrawl.BoltStore, error) {
	options := gocrawl.StoreOptions{FailureAttemptLimit: 3, Now: clock}
	options.Verify = config.VerifyStore && !storeVerified.Load()
	if options.Verify {
		storeVerified.Store(true)
	}
	store, err := gocrawl.OpenBoltStore(config.DatabasePath, options)
	if errors.Is(err, gocrawl.ErrCorruptStore) {
		moved, moveErr := gocrawl.QuarantineStore(config.DatabasePath, clock())
		if moveErr != nil {
			return nil, fmt.Errorf("%w (and it could not be moved aside: %v)", err, moveErr)
		}
		fmt.Fprintf(os.Stderr, "%v; moved it to %s and rebuilding from the exported state\n", err, moved)
		options.Verify = false
		store, err = gocrawl.OpenBoltStore(config.DatabasePath, options)
	}
	return store, err
}

// madeProgress reports whether a batch holds a package the registry answered for
// (read, or definitively absent); skips and retryable failures are not progress.
func madeProgress(results []gocrawl.ModuleResult) bool {
	for _, result := range results {
		if !result.Work.Skip && (result.Verdict == gocrawl.VerdictSuccess || result.Verdict == gocrawl.VerdictPermanent) {
			return true
		}
	}
	return false
}

// touchProgress bumps the progress file's modification time, creating it if needed.
func touchProgress(path string) {
	now := time.Now()
	if err := os.Chtimes(path, now, now); err == nil {
		return
	}
	if file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
		_ = file.Close()
	}
}
