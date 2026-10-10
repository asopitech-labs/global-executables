package gocrawl

import (
	"bytes"
	"cmp"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

var (
	metaBucket              = []byte("meta")
	retryBucket             = []byte("retries")
	unavailableBucket       = []byte("unavailable")
	observationBucket       = []byte("observations")
	observationModuleBucket = []byte("observation_modules")
	catalogBucket           = []byte("catalog")
	checkBucket             = []byte("checks")
	feedBucket              = []byte("feed")
)

type StoreOptions struct {
	FailureAttemptLimit int
	// Now supplies the clock for check days; tests move it. Defaults to time.Now.
	Now func() time.Time
}

type BoltStore struct {
	now                 func() time.Time
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
	store := &BoltStore{db: db, failureAttemptLimit: options.FailureAttemptLimit, now: time.Now}
	if options.Now != nil {
		store.now = options.Now
	}
	if err := db.Update(func(tx *bolt.Tx) error {
		buildObservationIndex := tx.Bucket(observationModuleBucket) == nil
		for _, name := range [][]byte{metaBucket, retryBucket, unavailableBucket, observationBucket, observationModuleBucket, catalogBucket, checkBucket, feedBucket} {
			if _, err := tx.CreateBucketIfNotExists(name); err != nil {
				return err
			}
		}
		if buildObservationIndex {
			return indexExistingObservations(tx)
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
		if err := putUint(meta, "refresh_cursor", snapshot.RefreshCursor); err != nil {
			return err
		}
		if err := putInt64(meta, "refresh_catalog_offset", snapshot.RefreshCatalogOffset); err != nil {
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
			if err := putObservation(tx, observation); err != nil {
				return err
			}
		}
		return importChecks(tx, snapshot, Today(s.now()))
	})
}

// importChecks stores the recorded checks, the feed position and the announced
// changes. A package that has observations but no check is seeded with day zero, so it
// is due at once yet a re-check that finds the same version can skip its artifacts.
func importChecks(tx *bolt.Tx, snapshot ImportSnapshot, today int) error {
	meta := tx.Bucket(metaBucket)
	if err := meta.Put([]byte("feed_cursor"), []byte(snapshot.FeedCursor)); err != nil {
		return err
	}
	if err := putUint(meta, "due_floor", uint64(max(snapshot.DueFloor, 0))); err != nil {
		return err
	}
	checks := tx.Bucket(checkBucket)
	// Keys go in sorted and the pages are packed full: a million random inserts into a
	// B+tree took over ten minutes, the same million in order take seconds.
	checks.FillPercent = 1.0
	merged := make(map[string]string, len(snapshot.Checks))
	for module, value := range snapshot.Checks {
		if _, valid := ParseCheck(value); valid {
			merged[module] = value
		}
	}
	for _, observation := range snapshot.Observations {
		if _, known := merged[observation.Package]; observation.Version == "" || known {
			continue
		}
		merged[observation.Package] = EncodeCheck(ColdCheck(observation.Package, observation.Version, today))
	}
	for _, module := range slices.Sorted(maps.Keys(merged)) {
		if err := checks.Put([]byte(module), []byte(merged[module])); err != nil {
			return err
		}
	}
	for module, entry := range snapshot.FeedPending {
		if err := putJSON(tx.Bucket(feedBucket), module, entry); err != nil {
			return err
		}
	}
	return nil
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
		refreshCursor := getUint(meta, "refresh_cursor")
		refreshOffset := getInt64(meta, "refresh_catalog_offset")
		catalogSize := getUint(meta, "catalog_size")
		var downloaded uint64
		today := Today(s.now())
		// An all-unchanged batch must leave the exported history as it was, so the
		// generation counter (part of the state) moves only when a result changed it.
		historyChanged := false
		for _, result := range results {
			if !result.Work.Skip && !(result.Unchanged && result.Verdict == VerdictSuccess) {
				historyChanged = true
			}
			if result.Verdict == VerdictCanceled {
				return context.Canceled
			}
			if result.Work.Refresh {
				if refreshCursor >= catalogSize {
					refreshCursor, refreshOffset = 0, 0
				}
				if result.Work.CatalogIndex != refreshCursor {
					return fmt.Errorf("refresh cursor hole: got catalog index %d, want %d", result.Work.CatalogIndex, refreshCursor)
				}
				if result.Work.CatalogOffset < refreshOffset {
					return fmt.Errorf("refresh catalog offset regressed: got %d, have %d", result.Work.CatalogOffset, refreshOffset)
				}
				refreshCursor++
				refreshOffset = result.Work.CatalogOffset
				if refreshCursor >= catalogSize {
					refreshCursor, refreshOffset = 0, 0
				}
			} else if !result.Work.Retry && !result.Work.Feed {
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
			if err := applyCheck(tx, result, today); err != nil {
				return err
			}
			if (result.Verdict == VerdictSuccess || result.Verdict == VerdictPermanent) && !result.Unchanged {
				if err := deleteModuleObservations(tx, result.Work.Module); err != nil {
					return err
				}
			}
			for _, observation := range result.Observations {
				if err := putObservation(tx, observation); err != nil {
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
		if err := putUint(meta, "refresh_cursor", refreshCursor); err != nil {
			return err
		}
		if err := putInt64(meta, "refresh_catalog_offset", refreshOffset); err != nil {
			return err
		}
		if historyChanged {
			if err := putUint(meta, "generation", getUint(meta, "generation")+1); err != nil {
				return err
			}
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
	if result.Work.Skip {
		return nil
	}
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
		var previous RetryEntry
		if value := retries.Get(module); value != nil {
			_ = json.Unmarshal(value, &previous)
		}
		if result.UncountedRetry {
			return putJSON(retries, result.Work.Module, RetryEntry{Error: result.Error, Attempts: previous.Attempts})
		}
		attempts := result.Work.Attempt
		if attempts <= 0 {
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
	slices.SortFunc(snapshot.Observations, func(left, right Observation) int {
		return cmp.Compare(
			strings.Join([]string{left.Command, left.Package, left.Source}, "\x00"),
			strings.Join([]string{right.Command, right.Package, right.Source}, "\x00"),
		)
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
		if err := snapshotChecks(tx, &snapshot); err != nil {
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
				return yield(sanitizeObservation(observation))
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
	snapshot.RefreshCursor = getUint(meta, "refresh_cursor")
	snapshot.RefreshCatalogOffset = getInt64(meta, "refresh_catalog_offset")
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
	if err := snapshotChecks(tx, &snapshot); err != nil {
		return Snapshot{}, err
	}
	err := tx.Bucket(observationBucket).ForEach(func(_, value []byte) error {
		var observation Observation
		if err := json.Unmarshal(value, &observation); err != nil {
			return err
		}
		snapshot.Observations = append(snapshot.Observations, sanitizeObservation(observation))
		return nil
	})
	return snapshot, err
}

func snapshotChecks(tx *bolt.Tx, snapshot *Snapshot) error {
	meta := tx.Bucket(metaBucket)
	snapshot.FeedCursor = string(meta.Get([]byte("feed_cursor")))
	snapshot.DueFloor = int(getUint(meta, "due_floor"))
	snapshot.Checks = make(map[string]string)
	snapshot.FeedPending = make(map[string]FeedEntry)
	if err := tx.Bucket(checkBucket).ForEach(func(key, value []byte) error {
		snapshot.Checks[string(key)] = string(value)
		return nil
	}); err != nil {
		return err
	}
	return tx.Bucket(feedBucket).ForEach(func(key, value []byte) error {
		var entry FeedEntry
		if err := json.Unmarshal(value, &entry); err != nil {
			return err
		}
		snapshot.FeedPending[string(key)] = entry
		return nil
	})
}

// applyCheck records what a committed result learned about a module, and removes the
// module from the feed queue in the same transaction as its observations. A feed
// announcement therefore survives a crash until the observations it caused are durable.
func applyCheck(tx *bolt.Tx, result ModuleResult, today int) error {
	module := []byte(result.Work.Module)
	if result.Work.Feed || result.Verdict == VerdictSuccess || result.Verdict == VerdictPermanent {
		// A retry result is owned by the retry bucket from now on.
		if err := tx.Bucket(feedBucket).Delete(module); err != nil {
			return err
		}
	}
	checks := tx.Bucket(checkBucket)
	switch {
	case result.Verdict == VerdictPermanent:
		return checks.Delete(module)
	case result.Verdict != VerdictSuccess || result.Work.Skip || result.Latest == "":
		return nil
	}
	var previous Check
	var had bool
	if value := checks.Get(module); value != nil {
		previous, had = ParseCheck(string(value))
	}
	outcome := OutcomeUnknown // an unchanged result keeps the outcome of the version it re-confirmed
	if !result.Unchanged {
		outcome = NoCommands
		if len(result.Observations) > 0 {
			outcome = HasCommands
		}
	}
	next, _ := Next(previous, had, result.Latest, outcome, today)
	return checks.Put(module, []byte(EncodeCheck(next)))
}

// Checks returns the recorded check of each module that has one.
// AllChecks returns every recorded check, for writing the schedule cache.
func (s *BoltStore) AllChecks(ctx context.Context) (map[string]string, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var snapshot Snapshot
	err := s.db.View(func(tx *bolt.Tx) error { return snapshotChecks(tx, &snapshot) })
	return snapshot.Checks, err
}

func (s *BoltStore) Checks(ctx context.Context, modules []string) (map[string]Check, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	found := make(map[string]Check, len(modules))
	err := s.db.View(func(tx *bolt.Tx) error {
		checks := tx.Bucket(checkBucket)
		for _, module := range modules {
			if value := checks.Get([]byte(module)); value != nil {
				if check, valid := ParseCheck(string(value)); valid {
					found[module] = check
				}
			}
		}
		return nil
	})
	return found, err
}

func putObservation(tx *bolt.Tx, observation Observation) error {
	observation = sanitizeObservation(observation)
	key := strings.Join([]string{observation.Command, observation.Ecosystem, observation.Package, observation.Source}, "\x00")
	if err := putJSON(tx.Bucket(observationBucket), key, observation); err != nil {
		return err
	}
	return tx.Bucket(observationModuleBucket).Put(observationModuleKey(observation.Package, key), nil)
}

func observationModuleKey(module, observationKey string) []byte {
	return []byte(module + "\x00" + observationKey)
}

func deleteModuleObservations(tx *bolt.Tx, module string) error {
	index := tx.Bucket(observationModuleBucket)
	prefix := []byte(module + "\x00")
	cursor := index.Cursor()
	for key, _ := cursor.Seek(prefix); key != nil && bytes.HasPrefix(key, prefix); key, _ = cursor.Next() {
		if err := tx.Bucket(observationBucket).Delete(key[len(prefix):]); err != nil {
			return err
		}
		if err := cursor.Delete(); err != nil {
			return err
		}
	}
	return nil
}

func indexExistingObservations(tx *bolt.Tx) error {
	return tx.Bucket(observationBucket).ForEach(func(key, value []byte) error {
		var observation Observation
		if err := json.Unmarshal(value, &observation); err != nil {
			return err
		}
		return tx.Bucket(observationModuleBucket).Put(observationModuleKey(observation.Package, string(key)), nil)
	})
}

func sanitizeObservation(observation Observation) Observation {
	observation.Source = stripURLCredentials(observation.Source)
	if observation.Repository != nil {
		repository := stripURLCredentials(*observation.Repository)
		observation.Repository = &repository
	}
	return observation
}

func stripURLCredentials(value string) string {
	scheme := strings.Index(value, "://")
	if scheme < 0 {
		return value
	}
	authorityStart := scheme + 3
	authorityEnd := len(value)
	if offset := strings.IndexAny(value[authorityStart:], "/?#"); offset >= 0 {
		authorityEnd = authorityStart + offset
	}
	authority := value[authorityStart:authorityEnd]
	userinfo := strings.LastIndex(authority, "@")
	if userinfo < 0 {
		return value
	}
	return value[:authorityStart] + authority[userinfo+1:] + value[authorityEnd:]
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
