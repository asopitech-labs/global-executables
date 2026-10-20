package gocrawl

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

type stubCatalogFetcher struct {
	pages []CatalogPage
	since []string
}

func (f *stubCatalogFetcher) FetchCatalogPage(_ context.Context, since string, _ int) (CatalogPage, error) {
	f.since = append(f.since, since)
	page := f.pages[0]
	f.pages = f.pages[1:]
	return page, nil
}

func TestRefreshCatalogAppendsOnlyNewModulesAndAdvancesFeed(t *testing.T) {
	directory := t.TempDir()
	catalogPath := filepath.Join(directory, "go-modules.txt")
	if err := os.WriteFile(catalogPath, []byte("example.com/a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	store, err := OpenBoltStore(filepath.Join(directory, "crawl.db"), StoreOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := store.Import(context.Background(), ImportSnapshot{
		CatalogSize: 1, CatalogSince: "2026-08-20T00:00:00Z",
	}); err != nil {
		t.Fatal(err)
	}
	fetcher := &stubCatalogFetcher{pages: []CatalogPage{
		{Entries: []CatalogEntry{
			{Path: "example.com/a", Timestamp: "2026-08-21T00:00:00Z"},
			{Path: "example.com/b", Timestamp: "2026-08-21T00:00:01Z"},
			{Path: "example.com/b", Timestamp: "2026-08-21T00:00:02Z"},
		}},
		{Complete: true},
	}}
	report, err := RefreshCatalog(context.Background(), catalogPath, store, fetcher, CatalogRefreshOptions{
		MaxPages: 2, PageSize: 3,
	})
	if err != nil {
		t.Fatal(err)
	}
	if report.Discovered != 1 || report.Requests != 2 || !report.Complete {
		t.Fatalf("report=%+v", report)
	}
	body, err := os.ReadFile(catalogPath)
	if err != nil {
		t.Fatal(err)
	}
	if string(body) != "example.com/a\nexample.com/b\n" {
		t.Fatalf("catalog=%q", body)
	}
	progress, err := store.Progress(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if progress.CatalogSize != 2 || progress.CatalogSince != "2026-08-21T00:00:02Z" || !progress.CatalogComplete {
		t.Fatalf("progress=%+v", progress)
	}
	if !reflect.DeepEqual(fetcher.since, []string{"2026-08-20T00:00:00Z", "2026-08-21T00:00:02Z"}) {
		t.Fatalf("since=%v", fetcher.since)
	}
}
