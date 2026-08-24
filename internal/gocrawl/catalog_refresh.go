package gocrawl

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

type CatalogEntry struct {
	Path      string
	Version   string
	Timestamp string
}

type CatalogPage struct {
	Entries         []CatalogEntry
	Complete        bool
	DownloadedBytes uint64
}

type CatalogFetcher interface {
	FetchCatalogPage(context.Context, string, int) (CatalogPage, error)
}

type CatalogRefreshOptions struct {
	MaxPages int
	PageSize int
}

type CatalogRefreshReport struct {
	Discovered      uint64
	Requests        uint64
	DownloadedBytes uint64
	Complete        bool
}

func RefreshCatalog(
	ctx context.Context,
	path string,
	store *BoltStore,
	fetcher CatalogFetcher,
	options CatalogRefreshOptions,
) (CatalogRefreshReport, error) {
	if options.MaxPages <= 0 {
		options.MaxPages = 10
	}
	if options.PageSize <= 0 || options.PageSize > 2000 {
		options.PageSize = 2000
	}
	if err := store.SyncCatalog(ctx, path); err != nil {
		return CatalogRefreshReport{}, err
	}
	progress, err := store.Progress(ctx)
	if err != nil {
		return CatalogRefreshReport{}, err
	}
	since := progress.CatalogSince
	report := CatalogRefreshReport{Complete: progress.CatalogComplete}
	pending := make([]string, 0)
	pendingSet := make(map[string]struct{})
	var fetchErr error
	for range options.MaxPages {
		page, err := fetcher.FetchCatalogPage(ctx, since, options.PageSize)
		if err != nil {
			fetchErr = err
			report.Complete = false
			break
		}
		report.Requests++
		report.DownloadedBytes += page.DownloadedBytes
		paths := make([]string, 0, len(page.Entries))
		for _, entry := range page.Entries {
			paths = append(paths, entry.Path)
			if entry.Timestamp != "" {
				since = entry.Timestamp
			}
		}
		newModules, err := store.FilterNewCatalogModules(ctx, paths)
		if err != nil {
			return report, err
		}
		for _, module := range newModules {
			if _, duplicate := pendingSet[module]; duplicate {
				continue
			}
			pendingSet[module] = struct{}{}
			pending = append(pending, module)
		}
		report.Complete = page.Complete
		if page.Complete {
			break
		}
	}
	fileEnd, err := replaceCatalogWithAppend(ctx, path, pending)
	if err != nil {
		return report, err
	}
	if fetchErr != nil {
		report.Complete = false
	}
	if err := store.CommitCatalogPage(ctx, pending, fileEnd, since, report.Complete); err != nil {
		return report, err
	}
	report.Discovered = uint64(len(pending))
	return report, fetchErr
}

func replaceCatalogWithAppend(ctx context.Context, path string, modules []string) (int64, error) {
	if len(modules) == 0 {
		stat, err := os.Stat(path)
		if err != nil {
			return 0, err
		}
		return stat.Size(), nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return 0, err
	}
	source, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer source.Close()
	stat, err := source.Stat()
	if err != nil {
		return 0, err
	}
	if stat.Size() > 0 {
		last := []byte{0}
		if _, err := source.ReadAt(last, stat.Size()-1); err != nil {
			return 0, err
		}
		if last[0] != '\n' {
			return 0, errors.New("catalog does not end with a newline")
		}
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*.tmp")
	if err != nil {
		return 0, err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(stat.Mode().Perm()); err != nil {
		_ = temporary.Close()
		return 0, err
	}
	buffer := make([]byte, 1024*1024)
	for {
		if err := ctx.Err(); err != nil {
			_ = temporary.Close()
			return 0, err
		}
		read, readErr := source.Read(buffer)
		if read > 0 {
			if _, err := temporary.Write(buffer[:read]); err != nil {
				_ = temporary.Close()
				return 0, err
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			_ = temporary.Close()
			return 0, readErr
		}
	}
	buffered := bufio.NewWriterSize(temporary, 256*1024)
	for _, module := range modules {
		if _, err := buffered.WriteString(module + "\n"); err != nil {
			_ = temporary.Close()
			return 0, err
		}
	}
	if err := buffered.Flush(); err != nil {
		_ = temporary.Close()
		return 0, err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return 0, err
	}
	updated, err := temporary.Stat()
	if err != nil {
		_ = temporary.Close()
		return 0, err
	}
	if err := temporary.Close(); err != nil {
		return 0, err
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return 0, err
	}
	directory, err := os.Open(filepath.Dir(path))
	if err != nil {
		return 0, err
	}
	if err := directory.Sync(); err != nil {
		_ = directory.Close()
		return 0, err
	}
	if err := directory.Close(); err != nil {
		return 0, err
	}
	if updated.Size() < stat.Size() {
		return 0, fmt.Errorf("catalog replacement shrank from %d to %d", stat.Size(), updated.Size())
	}
	return updated.Size(), nil
}
