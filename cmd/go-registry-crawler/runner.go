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
)

type crawlConfig struct {
	StatePath        string
	ObservationsPath string
	ReportPath       string
	CatalogPath      string
	DatabasePath     string
	ProxyURL         string
	IndexURL         string
	CatalogPages     int
	PackageBudget    int
	ByteBudget       int64
	Workers          int
	MaxInFlight      int
	CommitBatch      int
	RequestTimeout   time.Duration
	ModuleTimeout    time.Duration
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
	if config.PackageBudget <= 0 {
		config.PackageBudget = 100
	}
	if config.Workers <= 0 {
		config.Workers = 32
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
		imported, document, err = gocrawl.LoadCompatibility(config.StatePath, config.ObservationsPath, config.CatalogPath)
		if err == nil {
			err = store.Import(ctx, imported)
		}
	}
	if err != nil {
		return gocrawl.PassReport{}, err
	}
	catalogReport, catalogErr := gocrawl.RefreshCatalog(
		ctx,
		config.CatalogPath,
		store,
		goproxy.NewIndexClient(config.IndexURL, goproxy.Config{
			RequestTimeout: config.RequestTimeout,
		}),
		gocrawl.CatalogRefreshOptions{MaxPages: config.CatalogPages, PageSize: 2000},
	)
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
	inspector := goproxy.NewInspector(goproxy.Config{
		BaseURL: config.ProxyURL, RequestTimeout: config.RequestTimeout, ModuleTimeout: config.ModuleTimeout,
	})
	coordinator := gocrawl.Coordinator{
		Workers: config.Workers, MaxInFlight: config.MaxInFlight, CommitBatch: config.CommitBatch,
	}
	runErr := coordinator.Run(passCtx, works, inspector, committer)
	if committer.exhausted && errors.Is(runErr, context.Canceled) {
		runErr = nil
	}
	report := gocrawl.PassReport{
		StartedAt: started, FinishedAt: time.Now().UTC(), Processed: committer.processed,
		Records: committer.records, DownloadedBytes: committer.downloaded,
		BudgetExhausted: committer.exhausted, Interrupted: ctx.Err() != nil, Workers: config.Workers,
		CatalogDiscovered: catalogReport.Discovered, CatalogRequests: catalogReport.Requests,
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
	if err := gocrawl.ExportStoreCompatibility(context.Background(), gocrawl.ExportPaths{
		State: config.StatePath, Observations: config.ObservationsPath, Report: config.ReportPath,
	}, document, store, report); err != nil {
		return report, err
	}
	if runErr != nil {
		return report, runErr
	}
	return report, nil
}
