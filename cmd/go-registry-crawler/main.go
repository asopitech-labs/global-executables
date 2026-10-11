package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var version = "dev"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 1 && args[0] == "--version" {
		_, _ = fmt.Fprintf(stdout, "go-registry-crawler %s\n", version)
		return 0
	}
	if len(args) == 0 || args[0] != "crawl" {
		_, _ = fmt.Fprintln(stderr, "go-registry-crawler: production routing is disabled; use the explicit crawl subcommand")
		return 2
	}

	flags := flag.NewFlagSet("crawl", flag.ContinueOnError)
	flags.SetOutput(stderr)
	config := crawlConfig{}
	passes := 1
	pause := 5 * time.Second
	flags.StringVar(&config.Source, "source", "go", "registry source: go, npm, pypi, rubygems, or packagist")
	flags.StringVar(&config.StatePath, "state", "", "compatibility registry state")
	flags.StringVar(&config.ObservationsPath, "output", "", "compatibility observations")
	flags.StringVar(&config.ReportPath, "report", "", "crawl report")
	flags.StringVar(&config.CatalogPath, "catalog", "", "package catalog")
	flags.StringVar(&config.DatabasePath, "database", "", "transactional source-local store")
	flags.StringVar(&config.CachePath, "cache", "", "schedule cache (never committed; default: cache/<source>.cache.gz beside the state directory)")
	flags.StringVar(&config.ProxyURL, "proxy", "https://proxy.golang.org", "Go module proxy")
	flags.StringVar(&config.IndexURL, "index", "https://index.golang.org/index", "Go module index endpoint")
	flags.StringVar(&config.RegistryURL, "registry", "", "registry base URL for npm, PyPI, RubyGems, or Packagist")
	flags.StringVar(&config.FeedURL, "feed", "", "change feed base URL for npm (replicate), PyPI is derived from -registry, or Packagist (packagist.org)")
	flags.IntVar(&config.CatalogPages, "catalog-pages", 10, "maximum index pages per pass")
	flags.IntVar(&config.PackageBudget, "package-budget", 3000, "maximum modules in this pass")
	flags.Int64Var(&config.ByteBudget, "byte-budget", 8_000_000_000, "maximum downloaded bytes in this pass")
	flags.IntVar(&config.Workers, "workers", 0, "concurrent inspectors (source-safe default when zero)")
	flags.IntVar(&config.MaxInFlight, "max-in-flight", 128, "bounded submitted work")
	flags.IntVar(&config.CommitBatch, "commit-batch", 32, "maximum results per transaction")
	flags.DurationVar(&config.RequestTimeout, "request-timeout", 0, "deadline per HTTP attempt (source-safe default when zero)")
	flags.DurationVar(&config.ModuleTimeout, "module-timeout", 0, "deadline per package inspection (source-safe default when zero)")
	flags.BoolVar(&config.Continuous, "continuous", false, "keep cycling through indexed packages after exhaustive coverage")
	flags.StringVar(&config.RotationInclude, "rotation-include", "", "local booster: visit only these owner buckets in the rotation (lo-hi,lo-hi of 0-255)")
	flags.StringVar(&config.RotationExclude, "rotation-exclude", "", "leave these owner buckets of the rotation to a local booster (lo-hi,lo-hi of 0-255)")
	flags.BoolVar(&config.NoFeed, "no-feed", false, "do not poll the change feed (its cursor stays with the Actions refresh)")
	flags.StringVar(&config.UserAgent, "user-agent", os.Getenv("GE_USER_AGENT"), "User-Agent to send (include a contact address when crawling from your own machine)")
	flags.Int64Var(&config.MaxBytesPerSecond, "max-bytes-per-second", 0, "cap the average download rate (0 = no cap)")
	flags.Float64Var(&config.PauseJitter, "pause-jitter", 0, "spread the pause between passes by +-this fraction")
	flags.IntVar(&passes, "passes", 1, "number of passes; zero runs until exhaustive")
	flags.DurationVar(&pause, "pause", 5*time.Second, "delay between passes")
	if err := flags.Parse(args[1:]); err != nil {
		return 2
	}
	if flags.NArg() != 0 {
		_, _ = fmt.Fprintf(stderr, "unexpected arguments: %v\n", flags.Args())
		return 2
	}
	if err := config.applyDefaults(); err != nil {
		_, _ = fmt.Fprintln(stderr, err)
		return 2
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	err := executeLoop(ctx, config, passes, pause, stdout, executePass)
	if err == nil {
		return 0
	}
	_, _ = fmt.Fprintln(stderr, err)
	if errors.Is(err, context.Canceled) {
		return 130
	}
	return 1
}
