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
	flags.StringVar(&config.StatePath, "state", "data/production/registry-state.json", "compatibility registry state")
	flags.StringVar(&config.ObservationsPath, "output", "data/production/intermediate/go.jsonl", "compatibility observations")
	flags.StringVar(&config.ReportPath, "report", "reports/registry-artifact-crawl.json", "crawl report")
	flags.StringVar(&config.CatalogPath, "catalog", "data/production/go-modules.txt", "Go module catalog")
	flags.StringVar(&config.DatabasePath, "database", "data/production/go-crawl.db", "transactional local store")
	flags.StringVar(&config.ProxyURL, "proxy", "https://proxy.golang.org", "Go module proxy")
	flags.StringVar(&config.IndexURL, "index", "https://index.golang.org/index", "Go module index endpoint")
	flags.IntVar(&config.CatalogPages, "catalog-pages", 10, "maximum index pages per pass")
	flags.IntVar(&config.PackageBudget, "package-budget", 3000, "maximum modules in this pass")
	flags.Int64Var(&config.ByteBudget, "byte-budget", 8_000_000_000, "maximum downloaded bytes in this pass")
	flags.IntVar(&config.Workers, "workers", 32, "concurrent inspectors")
	flags.IntVar(&config.MaxInFlight, "max-in-flight", 128, "bounded submitted work")
	flags.IntVar(&config.CommitBatch, "commit-batch", 32, "maximum results per transaction")
	flags.DurationVar(&config.RequestTimeout, "request-timeout", 45*time.Second, "deadline per HTTP attempt")
	flags.DurationVar(&config.ModuleTimeout, "module-timeout", 2*time.Minute, "deadline per module inspection")
	flags.IntVar(&passes, "passes", 1, "number of passes; zero runs until exhaustive")
	flags.DurationVar(&pause, "pause", 5*time.Second, "delay between passes")
	if err := flags.Parse(args[1:]); err != nil {
		return 2
	}
	if flags.NArg() != 0 {
		_, _ = fmt.Fprintf(stderr, "unexpected arguments: %v\n", flags.Args())
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
