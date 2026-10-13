package gocrawl

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	bolt "go.etcd.io/bbolt"
)

func TestQuarantineStorePreservesRepeatedStoresInTheSameSecond(t *testing.T) {
	path := filepath.Join(t.TempDir(), "crawl.db")
	now := time.Date(2026, 10, 13, 9, 0, 0, 0, time.FixedZone("JST", 9*60*60))
	base := path + ".corrupt-20261013T000000Z"
	kept := make(map[string]string)
	for index := range 3 {
		content := fmt.Sprintf("damaged store %d", index)
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		moved, err := QuarantineStore(path, now.Add(time.Duration(index)*time.Nanosecond))
		if err != nil {
			t.Fatal(err)
		}
		want := base
		if index > 0 {
			want = fmt.Sprintf("%s-%d", base, index)
		}
		if moved != want {
			t.Fatalf("want %s, got %s", want, moved)
		}
		kept[moved] = content
		if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("original name must be free: %v", err)
		}
		for name, want := range kept {
			got, err := os.ReadFile(name)
			if err != nil || string(got) != want {
				t.Fatalf("quarantine %s: want %q, got %q (%v)", name, want, got, err)
			}
		}
	}
}

func TestQuarantineStorePreservesExistingFiles(t *testing.T) {
	path := filepath.Join(t.TempDir(), "crawl.db")
	now := time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC)
	base := path + ".corrupt-20261013T000000Z"
	for _, name := range []string{base, base + "-1"} {
		if err := os.WriteFile(name, []byte(name), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(path, []byte("new damaged store"), 0o600); err != nil {
		t.Fatal(err)
	}
	moved, err := QuarantineStore(path, now)
	if err != nil || moved != base+"-2" {
		t.Fatalf("want next free quarantine name, got %s (%v)", moved, err)
	}
	for _, name := range []string{base, base + "-1"} {
		got, err := os.ReadFile(name)
		if err != nil || string(got) != name {
			t.Fatalf("existing file %s changed: %q (%v)", name, got, err)
		}
	}
	got, err := os.ReadFile(moved)
	if err != nil || string(got) != "new damaged store" {
		t.Fatalf("new quarantine content: %q (%v)", got, err)
	}
}

func TestQuarantineStorePreservesExistingSymlinks(t *testing.T) {
	for _, dangling := range []bool{false, true} {
		t.Run(fmt.Sprintf("dangling=%t", dangling), func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "crawl.db")
			base := path + ".corrupt-20261013T000000Z"
			referent := path + ".inspection"
			if !dangling {
				if err := os.WriteFile(referent, []byte("inspection data"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			if err := os.Symlink(referent, base); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(path, []byte("damaged store"), 0o600); err != nil {
				t.Fatal(err)
			}
			moved, err := QuarantineStore(path, time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC))
			if err != nil || moved != base+"-1" {
				t.Fatalf("want a separate quarantine, got %s (%v)", moved, err)
			}
			if got, err := os.Readlink(base); err != nil || got != referent {
				t.Fatalf("existing symlink changed: %s (%v)", got, err)
			}
			if !dangling {
				if got, err := os.ReadFile(referent); err != nil || string(got) != "inspection data" {
					t.Fatalf("symlink referent changed: %q (%v)", got, err)
				}
			}
			if got, err := os.ReadFile(moved); err != nil || string(got) != "damaged store" {
				t.Fatalf("quarantined content: %q (%v)", got, err)
			}
		})
	}
}

func TestQuarantineStoreKeepsSourceOnLinkFailure(t *testing.T) {
	// A directory cannot be hard-linked, providing a deterministic failure even as root.
	path := filepath.Join(t.TempDir(), "crawl.db")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(path, "data")
	if err := os.WriteFile(marker, []byte("retained"), 0o600); err != nil {
		t.Fatal(err)
	}
	moved, err := QuarantineStore(path, time.Date(2026, 10, 13, 0, 0, 0, 0, time.UTC))
	if err == nil || moved != "" {
		t.Fatalf("want link failure, got %s (%v)", moved, err)
	}
	if got, err := os.ReadFile(marker); err != nil || string(got) != "retained" {
		t.Fatalf("source must remain intact: %q (%v)", got, err)
	}
	if matches, err := filepath.Glob(path + ".corrupt-*"); err != nil || len(matches) != 0 {
		t.Fatalf("failed link must leave no quarantine: %v (%v)", matches, err)
	}
}

func TestQuarantineStoreMissingSource(t *testing.T) {
	path := filepath.Join(t.TempDir(), "missing.db")
	moved, err := QuarantineStore(path, time.Now())
	if moved != "" || !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("want missing source error, got %s (%v)", moved, err)
	}
	if matches, err := filepath.Glob(path + ".corrupt-*"); err != nil || len(matches) != 0 {
		t.Fatalf("missing source must leave no quarantine: %v (%v)", matches, err)
	}
}

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
