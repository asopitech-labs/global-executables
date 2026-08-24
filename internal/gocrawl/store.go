package gocrawl

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	metaBucket        = []byte("meta")
	retryBucket       = []byte("retries")
	unavailableBucket = []byte("unavailable")
	observationBucket = []byte("observations")
	catalogBucket     = []byte("catalog")
)

type StoreOptions struct {
	FailureAttemptLimit int
}

type BoltStore struct {
	db                  *bolt.DB
	failureAttemptLimit int
	catalog             *CatalogIndex
}

func OpenBoltStore(path string, options StoreOptions) (*BoltStore, error) {
	if options.FailureAttemptLimit <= 0 {
		options.FailureAttemptLimit = 3
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	db, err := bolt.Open(path, 0o600, &bolt.Options{Timeout: 5 * time.Second})
	if err != nil {
		return nil, err
	}
	store := &BoltStore{db: db, failureAttemptLimit: options.FailureAttemptLimit}
	if err := db.Update(func(tx *bolt.Tx) error {
		for _, name := range [][]byte{metaBucket, retryBucket, unavailableBucket, observationBucket, catalogBucket} {
			if _, err := tx.CreateBucketIfNotExists(name); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		_ = db.Close()
		return nil, err
	}
	return store, nil
}

func (s *BoltStore) Close() error {
	if s.catalog != nil {
		if err := s.catalog.Close(); err != nil {
			_ = s.db.Close()
			return err
		}
	}
	return s.db.Close()
}

func (s *BoltStore) Initialized(ctx context.Context) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	var initialized bool
	err := s.db.View(func(tx *bolt.Tx) error {
		initialized = tx.Bucket(metaBucket).Get([]byte("initialized")) != nil
		return nil
	})
	return initialized, err
}

func (s *BoltStore) Import(ctx context.Context, snapshot ImportSnapshot) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		meta := tx.Bucket(metaBucket)
		if meta.Get([]byte("initialized")) != nil {
			return nil
		}
		if err := putUint(meta, "cursor", snapshot.Cursor); err != nil {
			return err
		}
		if err := putInt64(meta, "catalog_offset", snapshot.CatalogOffset); err != nil {
			return err
		}
		if err := putUint(meta, "catalog_size", snapshot.CatalogSize); err != nil {
			return err
		}
		if err := putBool(meta, "catalog_complete", snapshot.CatalogComplete); err != nil {
			return err
		}
		if err := meta.Put([]byte("catalog_since"), []byte(snapshot.CatalogSince)); err != nil {
			return err
		}
		if err := meta.Put([]byte("modules_file"), []byte(snapshot.ModulesFile)); err != nil {
			return err
		}
		if err := putUint(meta, "generation", 0); err != nil {
			return err
		}
		if err := putUint(meta, "processed", 0); err != nil {
			return err
		}
		if err := putUint(meta, "downloaded_bytes", 0); err != nil {
			return err
		}
		if err := meta.Put([]byte("initialized"), []byte{1}); err != nil {
			return err
		}
		for module, entry := range snapshot.Retries {
			if err := putJSON(tx.Bucket(retryBucket), module, entry); err != nil {
				return err
			}
		}
		for module, reason := range snapshot.Unavailable {
			if err := tx.Bucket(unavailableBucket).Put([]byte(module), []byte(reason)); err != nil {
				return err
			}
		}
		for _, observation := range snapshot.Observations {
			if err := putObservation(tx.Bucket(observationBucket), observation); err != nil {
				return err
			}
		}
		return nil
	})
}

func (s *BoltStore) Commit(ctx context.Context, results []ModuleResult) error {
	if len(results) == 0 {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.db.Update(func(tx *bolt.Tx) error {
		meta := tx.Bucket(metaBucket)
		if meta.Get([]byte("initialized")) == nil {
			return errors.New("store is not initialized")
		}
		cursor := getUint(meta, "cursor")
		offset := getInt64(meta, "catalog_offset")
		var downloaded uint64
		for _, result := range results {
			if result.Verdict == VerdictCanceled {
				return context.Canceled
			}
			if !result.Work.Retry {
				if result.Work.CatalogIndex != cursor {
					return fmt.Errorf("cursor hole: got catalog index %d, want %d", result.Work.CatalogIndex, cursor)
				}
				if result.Work.CatalogOffset < offset {
					return fmt.Errorf("catalog offset regressed: got %d, have %d", result.Work.CatalogOffset, offset)
				}
				cursor++
				offset = result.Work.CatalogOffset
			}
			if result.DownloadedBytes > 0 {
				downloaded += uint64(result.DownloadedBytes)
			}
			if err := s.applyVerdict(tx, result); err != nil {
				return err
			}
			for _, observation := range result.Observations {
				if err := putObservation(tx.Bucket(observationBucket), observation); err != nil {
					return err
				}
			}
		}
		if err := putUint(meta, "cursor", cursor); err != nil {
			return err
		}
		if err := putInt64(meta, "catalog_offset", offset); err != nil {
			return err
		}
		if err := putUint(meta, "generation", getUint(meta, "generation")+1); err != nil {
			return err
		}
		if err := putUint(meta, "processed", getUint(meta, "processed")+uint64(len(results))); err != nil {
			return err
		}
		return putUint(meta, "downloaded_bytes", getUint(meta, "downloaded_bytes")+downloaded)
	})
}

func (s *BoltStore) applyVerdict(tx *bolt.Tx, result ModuleResult) error {
	retries := tx.Bucket(retryBucket)
	unavailable := tx.Bucket(unavailableBucket)
	module := []byte(result.Work.Module)
	switch result.Verdict {
	case VerdictSuccess:
		if err := retries.Delete(module); err != nil {
			return err
		}
		return unavailable.Delete(module)
	case VerdictPermanent:
		if err := retries.Delete(module); err != nil {
			return err
		}
		return unavailable.Put(module, []byte(result.Error))
	case VerdictRetry:
		attempts := result.Work.Attempt
		if attempts <= 0 {
			var previous RetryEntry
			if value := retries.Get(module); value != nil {
				_ = json.Unmarshal(value, &previous)
			}
			attempts = previous.Attempts + 1
		}
		if attempts >= s.failureAttemptLimit {
			if err := retries.Delete(module); err != nil {
				return err
			}
			reason := fmt.Sprintf("gave up after %d attempts: %s", attempts, result.Error)
			return unavailable.Put(module, []byte(reason))
		}
		return putJSON(retries, result.Work.Module, RetryEntry{Error: result.Error, Attempts: attempts})
	default:
		return fmt.Errorf("unknown verdict %q", result.Verdict)
	}
}

func (s *BoltStore) Snapshot(ctx context.Context) (Snapshot, error) {
	return s.snapshot(ctx, true)
}

// Progress returns scheduling metadata without materializing the observation set.
func (s *BoltStore) Progress(ctx context.Context) (Snapshot, error) {
	return s.snapshot(ctx, false)
}

func (s *BoltStore) snapshot(ctx context.Context, includeObservations bool) (Snapshot, error) {
	if err := ctx.Err(); err != nil {
		return Snapshot{}, err
	}
	var snapshot Snapshot
	err := s.db.View(func(tx *bolt.Tx) error {
		var loadErr error
		snapshot, loadErr = snapshotFromTx(tx, includeObservations)
		return loadErr
	})
	sort.Slice(snapshot.Observations, func(i, j int) bool {
		left, right := snapshot.Observations[i], snapshot.Observations[j]
		return strings.Join([]string{left.Command, left.Package, left.Source}, "\x00") <
			strings.Join([]string{right.Command, right.Package, right.Source}, "\x00")
	})
	return snapshot, err
}

type ObservationSequence func(func(Observation) error) error

// ViewSnapshot keeps metadata and the ordered observation stream on one read
// transaction, so every compatibility artifact is derived from one generation.
func (s *BoltStore) ViewSnapshot(
	ctx context.Context,
	visit func(Snapshot, ObservationSequence) error,
) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	return s.db.View(func(tx *bolt.Tx) error {
		snapshot, err := snapshotFromTx(tx, false)
		if err != nil {
			return err
		}
		observations := func(yield func(Observation) error) error {
			return tx.Bucket(observationBucket).ForEach(func(_, value []byte) error {
				if err := ctx.Err(); err != nil {
					return err
				}
				var observation Observation
				if err := json.Unmarshal(value, &observation); err != nil {
					return err
				}
				return yield(observation)
			})
		}
		return visit(snapshot, observations)
	})
}

func snapshotFromTx(tx *bolt.Tx, includeObservations bool) (Snapshot, error) {
	snapshot := Snapshot{ImportSnapshot: ImportSnapshot{
		Retries: make(map[string]RetryEntry), Unavailable: make(map[string]string),
	}}
	meta := tx.Bucket(metaBucket)
	snapshot.Cursor = getUint(meta, "cursor")
	snapshot.CatalogOffset = getInt64(meta, "catalog_offset")
	snapshot.CatalogSize = getUint(meta, "catalog_size")
	snapshot.CatalogComplete = getBool(meta, "catalog_complete")
	snapshot.CatalogSince = string(meta.Get([]byte("catalog_since")))
	snapshot.ModulesFile = string(meta.Get([]byte("modules_file")))
	snapshot.Generation = getUint(meta, "generation")
	snapshot.Processed = getUint(meta, "processed")
	snapshot.DownloadedBytes = getUint(meta, "downloaded_bytes")
	if err := tx.Bucket(retryBucket).ForEach(func(key, value []byte) error {
		var entry RetryEntry
		if err := json.Unmarshal(value, &entry); err != nil {
			return err
		}
		snapshot.Retries[string(key)] = entry
		return nil
	}); err != nil {
		return Snapshot{}, err
	}
	if err := tx.Bucket(unavailableBucket).ForEach(func(key, value []byte) error {
		snapshot.Unavailable[string(key)] = string(value)
		return nil
	}); err != nil {
		return Snapshot{}, err
	}
	if !includeObservations {
		return snapshot, nil
	}
	err := tx.Bucket(observationBucket).ForEach(func(_, value []byte) error {
		var observation Observation
		if err := json.Unmarshal(value, &observation); err != nil {
			return err
		}
		snapshot.Observations = append(snapshot.Observations, observation)
		return nil
	})
	return snapshot, err
}

func putObservation(bucket *bolt.Bucket, observation Observation) error {
	key := strings.Join([]string{observation.Command, observation.Ecosystem, observation.Package, observation.Source}, "\x00")
	return putJSON(bucket, key, observation)
}

func putJSON(bucket *bolt.Bucket, key string, value any) error {
	body, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return bucket.Put([]byte(key), body)
}

func putUint(bucket *bolt.Bucket, key string, value uint64) error {
	encoded := make([]byte, 8)
	binary.BigEndian.PutUint64(encoded, value)
	return bucket.Put([]byte(key), encoded)
}

func getUint(bucket *bolt.Bucket, key string) uint64 {
	value := bucket.Get([]byte(key))
	if len(value) != 8 {
		return 0
	}
	return binary.BigEndian.Uint64(value)
}

func putInt64(bucket *bolt.Bucket, key string, value int64) error {
	return putUint(bucket, key, uint64(value))
}

func getInt64(bucket *bolt.Bucket, key string) int64 {
	return int64(getUint(bucket, key))
}

func putBool(bucket *bolt.Bucket, key string, value bool) error {
	if value {
		return bucket.Put([]byte(key), []byte{1})
	}
	return bucket.Put([]byte(key), []byte{0})
}

func getBool(bucket *bolt.Bucket, key string) bool {
	value := bucket.Get([]byte(key))
	return len(value) == 1 && value[0] == 1
}
