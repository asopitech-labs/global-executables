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
	target := fmt.Sprintf("%s.corrupt-%s", path, now.UTC().Format("20060102T150405Z"))
	if err := os.Rename(path, target); err != nil {
		return "", err
	}
	return target, nil
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
