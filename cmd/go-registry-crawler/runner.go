package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"sort"
	"time"

	"github.com/asopitech-labs/global-executables/internal/gocrawl"
	"github.com/asopitech-labs/global-executables/internal/goproxy"
	"github.com/asopitech-labs/global-executables/internal/registryinspect"
)

type crawlConfig struct {
	Source           string
	StatePath        string
	ObservationsPath string
	ReportPath       string
	CatalogPath      string
	DatabasePath     string
	ProxyURL         string
	IndexURL         string
	RegistryURL      string
	CatalogPages     int
	PackageBudget    int
	ByteBudget       int64
	Workers          int
	MaxInFlight      int
	CommitBatch      int
	RequestTimeout   time.Duration
	ModuleTimeout    time.Duration
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
		"go":   {"data/production/go-modules.txt", "data/production/go-crawl.db", "data/production/intermediate/go.jsonl", "https://proxy.golang.org", 32, 45 * time.Second, 2 * time.Minute},
		"npm":  {"data/production/npm-packages.txt", "data/production/npm-crawl.db", "data/production/intermediate/npm.jsonl", "https://registry.npmjs.org", 64, 45 * time.Second, 5 * time.Minute},
		"pypi": {"data/production/pypi-projects.txt", "data/production/pypi-crawl.db", "data/production/intermediate/pypi.jsonl", "https://pypi.org", 24, 45 * time.Second, 2 * time.Minute},
	}
	selected, exists := defaults[c.Source]
	if !exists {
		return fmt.Errorf("unsupported source %q", c.Source)
	}
	if c.StatePath == "" {
		c.StatePath = "data/production/registry-state.json"
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
	if c.ObservationsPath == "" {
		c.ObservationsPath = selected.observations
	}
	if c.RegistryURL == "" {
		c.RegistryURL = selected.registry
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
		// npm's Cloudflare edge advertises Retry-After once this host exceeds about
		// 300 requests/minute. A burst-free 200ms floor is faster in wall time than
		// triggering a two-minute ban and retrying the same package.
		registryConfig.MinRequestInterval = 200 * time.Millisecond
	}
	switch config.Source {
	case "go":
		return crawlAdapter{
			profile: gocrawl.CompatibilityProfileFor("go"),
			inspector: goproxy.NewInspector(goproxy.Config{
				BaseURL: config.ProxyURL, RequestTimeout: config.RequestTimeout, ModuleTimeout: config.ModuleTimeout,
			}),
			refresh: func(ctx context.Context, path string, store *gocrawl.BoltStore) (gocrawl.CatalogRefreshReport, error) {
				return gocrawl.RefreshCatalog(ctx, path, store,
					goproxy.NewIndexClient(config.IndexURL, goproxy.Config{RequestTimeout: config.RequestTimeout}),
					gocrawl.CatalogRefreshOptions{MaxPages: config.CatalogPages, PageSize: 2000})
			},
			metrics: func() registryinspect.Metrics { return registryinspect.Metrics{} },
		}, nil
	case "npm":
		inspector := registryinspect.NewNPMInspector(registryConfig)
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("npm"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics}, nil
	case "pypi":
		inspector := registryinspect.NewPyPIInspector(registryConfig)
		return crawlAdapter{profile: gocrawl.CompatibilityProfileFor("pypi"), inspector: inspector,
			refresh: staticCatalog, metrics: inspector.Metrics}, nil
	default:
		return crawlAdapter{}, fmt.Errorf("unsupported source %q", config.Source)
	}
}

type measuringCommitter struct {
	store      *gocrawl.BoltStore
	byteBudget int64
	cancel     context.CancelFunc
	processed  uint64
	records    uint64
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
		if report.Complete {
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
		c.records += uint64(len(result.Observations))
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

	store, err := gocrawl.OpenBoltStore(config.DatabasePath, gocrawl.StoreOptions{FailureAttemptLimit: 3})
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
	before, err := store.Progress(ctx)
	if err != nil {
		return gocrawl.PassReport{}, err
	}

	works := make([]gocrawl.ModuleWork, 0, config.PackageBudget)
	retryModules := make([]string, 0, len(before.Retries))
	for module := range before.Retries {
		retryModules = append(retryModules, module)
	}
	sort.Strings(retryModules)
	for _, module := range retryModules {
		if len(works) >= config.PackageBudget {
			break
		}
		entry := before.Retries[module]
		works = append(works, gocrawl.ModuleWork{
			Order: uint64(len(works)), Module: module, Retry: true, Attempt: entry.Attempts + 1,
		})
	}
	remaining := config.PackageBudget - len(works)
	if remaining > 0 {
		catalogWorks, err := gocrawl.ReadCatalogBatch(
			config.CatalogPath, before.Cursor, before.CatalogOffset, remaining, uint64(len(works)),
		)
		if err != nil {
			return gocrawl.PassReport{}, err
		}
		works = append(works, catalogWorks...)
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
		PackageBudget: config.PackageBudget, Requests: metrics.Requests,
		RateLimited: metrics.RateLimited, Timeouts: metrics.Timeouts,
		CircuitOpens: metrics.CircuitOpens, HostConcurrency: metrics.HostConcurrency,
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
