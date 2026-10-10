package gocrawl

import (
	"bufio"
	"compress/gzip"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"
)

// The schedule cache is the *cache* half of the history/cache split (docs/OPERATIONS.md
// "History and cache"). It holds only bookkeeping about when a package was last looked
// at: the check ("<day>:<streak>:<version>") of every package and the position of the
// refresh rotation. It is never committed to git and may be lost at any time: a missing
// or stale cache makes every package due again (staggered, see ColdCheck) and the rows
// in the history are never touched because of it.
//
// File format (gzip, UTF-8 text, shared with Python's refresh_policy.read_cache):
//
//	# global-executables schedule cache v1
//	# extraction <ExtractionRevision>
//	# cursor <rotation position>
//	<name>\t<day>:<streak>:<version>
const (
	cacheMagic = "# global-executables schedule cache v1"
	// MaxCacheEntries bounds the file: 3 million checks are about 25 MB compressed.
	// Past it the entries checked longest ago are dropped; they simply become due.
	MaxCacheEntries = 3_000_000
	// ColdStaggerStreaks spreads packages without a cache entry over the first backoff
	// tiers (0..3, i.e. next looks 1..8 days after the first), so a cold start does not
	// make them all fall due on the same day again.
	ColdStaggerStreaks = 4
)

// Cache is the content of a schedule cache file.
type Cache struct {
	Checks map[string]string
	Cursor uint64
}

// DefaultCachePath is where a source's cache lives next to its state directory.
func DefaultCachePath(statePath, source string) string {
	return filepath.Join(filepath.Dir(statePath), "cache", source+".cache.gz")
}

// ReadCache loads a cache file. A missing file, a different format version or a
// different extraction revision yields an empty cache and found=false: that is a cold
// start, not an error. Only unreadable files are errors worth reporting, and callers
// treat those as cold too.
func ReadCache(path string, revision int) (Cache, bool, error) {
	file, err := os.Open(path)
	if errors.Is(err, fs.ErrNotExist) {
		return Cache{}, false, nil
	}
	if err != nil {
		return Cache{}, false, err
	}
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		return Cache{}, false, fmt.Errorf("cache %s: %w", path, err)
	}
	defer reader.Close()
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	cache := Cache{Checks: map[string]string{}}
	header := 0
	for scanner.Scan() {
		line := scanner.Text()
		if header == 0 {
			if line != cacheMagic {
				return Cache{}, false, nil
			}
			header = 1
			continue
		}
		if value, ok := strings.CutPrefix(line, "# extraction "); ok {
			if stored, err := strconv.Atoi(value); err != nil || stored != revision {
				return Cache{}, false, nil
			}
			continue
		}
		if value, ok := strings.CutPrefix(line, "# cursor "); ok {
			cache.Cursor, _ = strconv.ParseUint(value, 10, 64)
			continue
		}
		name, check, ok := strings.Cut(line, "\t")
		if !ok || strings.HasPrefix(line, "#") {
			continue
		}
		if _, valid := ParseCheck(check); valid {
			cache.Checks[name] = check
		}
	}
	if err := scanner.Err(); err != nil {
		return Cache{}, false, fmt.Errorf("cache %s: %w", path, err)
	}
	return cache, header == 1, nil
}

// WriteCache stores the cache atomically, newest checks first when it must be cut to
// MaxCacheEntries. Names are written sorted so equal caches are equal files.
func WriteCache(path string, cache Cache, revision int) error {
	names := make([]string, 0, len(cache.Checks))
	for name := range cache.Checks {
		if name != "" && !strings.ContainsAny(name, "\t\n") {
			names = append(names, name)
		}
	}
	if len(names) > MaxCacheEntries {
		day := func(name string) int { check, _ := ParseCheck(cache.Checks[name]); return check.Day }
		slices.SortStableFunc(names, func(a, b string) int { return day(b) - day(a) })
		names = names[:MaxCacheEntries]
	}
	slices.Sort(names)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".cache-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(temporary.Name())
	// CreateTemp makes the file 0600. The crawler may run as root in a container while
	// the CI cache action runs as the runner user and must be able to read the file
	// (2026-10-09: "tar: Cannot open: Permission denied", every run was a cold start).
	if err := temporary.Chmod(0o644); err != nil {
		return err
	}
	writer := gzip.NewWriter(temporary)
	buffered := bufio.NewWriter(writer)
	fmt.Fprintf(buffered, "%s\n# extraction %d\n# cursor %d\n", cacheMagic, revision, cache.Cursor)
	for _, name := range names {
		buffered.WriteString(name + "\t" + cache.Checks[name] + "\n")
	}
	if err := buffered.Flush(); err != nil {
		return err
	}
	if err := writer.Close(); err != nil {
		return err
	}
	if err := temporary.Sync(); err != nil {
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporary.Name(), path)
}

// ColdCheck is the check assumed for a package that history knows (it has a row at
// `version`) but the cache does not: due now, with a streak spread deterministically by
// name so the next looks do not all land together.
func ColdCheck(module, version string, today int) Check {
	streak := int(coldDigest(module) % ColdStaggerStreaks)
	return Check{Day: today - RecheckInterval(module, streak, BackoffMaxDaysPlain), Streak: streak, Version: version}
}

// ColdRefreshStart is where the rotation begins when no cache says where it was: a
// position that advances by one budget per six-hour window, so repeated cold starts
// still sweep the whole catalog instead of re-reading its first entries.
func ColdRefreshStart(catalogSize uint64, budget int, now time.Time) uint64 {
	if catalogSize == 0 || budget <= 0 {
		return 0
	}
	window := uint64(now.UTC().Unix() / (6 * 3600))
	return (window * uint64(budget)) % catalogSize
}
