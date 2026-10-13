package gocrawl

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

// ErrCorruptStore reports a transaction store that failed its integrity check. The store
// is a rebuildable cache of the exported state, observations and schedule cache (which
// are written atomically and verified on read), so the remedy is to quarantine it and
// import again; see docs/OPERATIONS.md "Local booster".
var ErrCorruptStore = errors.New("transaction store is corrupt")

// verifyStore walks every page of the store (bbolt's consistency check).
func verifyStore(db *bolt.DB) (err error) {
	defer func() {
		if recovered := recover(); recovered != nil {
			err = fmt.Errorf("check panicked: %v", recovered)
		}
	}()
	return db.View(func(tx *bolt.Tx) error {
		var first error
		for problem := range tx.Check() {
			if first == nil {
				first = problem
			}
		}
		return first
	})
}

// QuarantineStore moves a corrupt store aside (never deletes it) and returns the new name.
func QuarantineStore(path string, now time.Time) (string, error) {
	base := fmt.Sprintf("%s.corrupt-%s", path, now.UTC().Format("20060102T150405Z"))
	for sequence := 0; ; sequence++ {
		target := base
		if sequence > 0 {
			target = fmt.Sprintf("%s-%d", base, sequence)
		}
		// Link creates the destination atomically and fails if any entry already
		// occupies it. Keep both names until the corrupt file is safely preserved.
		if err := os.Link(path, target); err != nil {
			if errors.Is(err, os.ErrExist) {
				continue
			}
			return "", err
		}
		if err := os.Remove(path); err != nil {
			// Retain the quarantine link even if freeing the original name fails.
			return target, fmt.Errorf("store preserved at %s; remove original: %w", target, err)
		}
		return target, nil
	}
}

// IsStoreCorruption reports whether a recovered panic comes from bbolt finding a damaged
// page (bbolt panics instead of returning errors for these).
func IsStoreCorruption(recovered any) bool {
	text := fmt.Sprint(recovered)
	for _, marker := range []string{"invalid page type", "already freed", "unexpected page", "freelist", "pgid", "invalid database", "self identifies as"} {
		if strings.Contains(text, marker) {
			return true
		}
	}
	return false
}
