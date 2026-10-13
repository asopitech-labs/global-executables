package gocrawl

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
)

// corruptPages overwrites data pages of a closed bbolt file the way a torn copy does.
func corruptPages(t *testing.T, path string) {
	t.Helper()
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	info, _ := file.Stat()
	page := int64(os.Getpagesize())
	garbage := []byte(strings.Repeat("\x0a", int(page)))
	for offset := 2 * page; offset+page <= info.Size(); offset += 2 * page {
		if _, err := file.WriteAt(garbage, offset); err != nil {
			t.Fatal(err)
		}
	}
}

func TestADamagedStoreIsDetectedQuarantinedAndNeverDeleted(t *testing.T) {
	path := filepath.Join(t.TempDir(), "crawl.db")
	db, err := bolt.Open(path, 0o600, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Update(func(tx *bolt.Tx) error {
		bucket, err := tx.CreateBucketIfNotExists([]byte("b"))
		for index := range 5000 {
			if err == nil {
				err = bucket.Put([]byte(strings.Repeat("k", 20)+time.Duration(index).String()), []byte(strings.Repeat("v", 200)))
			}
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
	_ = db.Close()
	healthy, err := OpenBoltStore(path, StoreOptions{Verify: true})
	if err != nil {
		t.Fatalf("a healthy store must pass the check: %v", err)
	}
	_ = healthy.Close()
	corruptPages(t, path)
	if _, err := OpenBoltStore(path, StoreOptions{Verify: true}); !errors.Is(err, ErrCorruptStore) {
		t.Fatalf("want ErrCorruptStore, got %v", err)
	}
	moved, err := QuarantineStore(path, time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC))
	if err != nil || !strings.HasSuffix(moved, ".corrupt-20261013T000000Z") {
		t.Fatalf("%s %v", moved, err)
	}
	if _, err := os.Stat(moved); err != nil {
		t.Fatal("the damaged store is kept for inspection")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("the original name is free for a fresh store")
	}
	for _, text := range []string{"invalid page type: 97544: 10", "page 12 already freed"} {
		if !IsStoreCorruption(text) {
			t.Errorf("%q is store corruption", text)
		}
	}
	if IsStoreCorruption("runtime error: index out of range [3] with length 2") {
		t.Error("an ordinary bug must not be mistaken for corruption")
	}
}
