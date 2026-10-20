package gocrawl

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"time"

	bolt "go.etcd.io/bbolt"
)

// FeedEvent is one change a registry announced.
type FeedEvent struct {
	Name string
	Time int64
	Kind string
}

// FeedPage is the result of polling a change feed from a cursor.
type FeedPage struct {
	Events []FeedEvent
	// Cursor is the position to poll from next time.
	Cursor string
	// Resync means the feed cannot describe everything that changed since the cursor
	// (Packagist "resync", a serial gap too large to replay). Every package becomes due
	// for the next rotation visit; the cursor still moves forward.
	Resync bool
	// DownloadedBytes and Requests are reported for the run's savings accounting.
	DownloadedBytes int64
	Requests        int
}

// Feed polls a registry change feed. An empty cursor asks for the current position
// without events: the rotation, not the feed, is responsible for the history before it.
type Feed interface {
	Poll(ctx context.Context, cursor string) (FeedPage, error)
}

// FeedOptions controls how announced changes are admitted into the queue.
type FeedOptions struct {
	// AdmitUnknown admits names the crawler has never recorded. A source with a fixed
	// scope (npm's critical set) rejects them; one that discovers packages accepts them.
	AdmitUnknown bool
	// Known, when set, further restricts admission to names it accepts.
	Known func(string) bool
}

// FeedReport summarises one poll for the crawl report.
type FeedReport struct {
	Requests        int
	DownloadedBytes int64
	Events          int
	Enqueued        int
	Resync          bool
	Error           string
}

// PollFeed reads the feed and stores the announced names together with the new cursor
// in one transaction. The names stay queued until the transaction that commits their
// observations removes them, so a crash replays the work instead of losing it, and the
// cursor never gets ahead of what is durably queued.
func PollFeed(ctx context.Context, store *BoltStore, feed Feed, options FeedOptions) (FeedReport, error) {
	cursor, err := store.FeedCursor(ctx)
	if err != nil {
		return FeedReport{}, err
	}
	page, pollErr := feed.Poll(ctx, cursor)
	report := FeedReport{Requests: page.Requests, DownloadedBytes: page.DownloadedBytes, Events: len(page.Events), Resync: page.Resync}
	if pollErr != nil {
		report.Error = pollErr.Error()
		return report, pollErr
	}
	enqueued, err := store.ApplyFeed(ctx, page, options)
	report.Enqueued = enqueued
	return report, err
}

// FeedCursor returns the stored feed position, empty before the first poll.
func (s *BoltStore) FeedCursor(ctx context.Context) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	var cursor string
	err := s.db.View(func(tx *bolt.Tx) error {
		cursor = string(tx.Bucket(metaBucket).Get([]byte("feed_cursor")))
		return nil
	})
	return cursor, err
}

// ApplyFeed enqueues the page's names and stores its cursor in one transaction.
func (s *BoltStore) ApplyFeed(ctx context.Context, page FeedPage, options FeedOptions) (int, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	if page.Cursor == "" {
		return 0, errors.New("feed page has no cursor")
	}
	enqueued := 0
	err := s.db.Update(func(tx *bolt.Tx) error {
		meta := tx.Bucket(metaBucket)
		count, err := enqueueEvents(tx, page.Events, options)
		if err != nil {
			return err
		}
		enqueued = count
		if page.Resync {
			if err := putUint(meta, "due_floor", uint64(Today(s.now()))); err != nil {
				return err
			}
		}
		return meta.Put([]byte("feed_cursor"), []byte(page.Cursor))
	})
	return enqueued, err
}

// enqueueEvents queues the admitted events inside the caller's transaction and returns
// how many names were newly queued.
func enqueueEvents(tx *bolt.Tx, events []FeedEvent, options FeedOptions) (int, error) {
	queue := tx.Bucket(feedBucket)
	checks := tx.Bucket(checkBucket)
	enqueued := 0
	for _, event := range events {
		if event.Name == "" {
			continue
		}
		key := []byte(event.Name)
		known := checks.Get(key) != nil || hasObservations(tx, event.Name)
		if !known && !options.AdmitUnknown {
			continue
		}
		if options.Known != nil && !options.Known(event.Name) {
			continue
		}
		entry := FeedEntry{Time: event.Time, Kind: event.Kind}
		if value := queue.Get(key); value != nil {
			var previous FeedEntry
			if json.Unmarshal(value, &previous) == nil && previous.Time > entry.Time {
				entry.Time = previous.Time
			}
		} else {
			enqueued++
		}
		if err := putJSON(queue, event.Name, entry); err != nil {
			return 0, err
		}
	}
	return enqueued, nil
}

func hasObservations(tx *bolt.Tx, module string) bool {
	cursor := tx.Bucket(observationModuleBucket).Cursor()
	prefix := []byte(module + "\x00")
	key, _ := cursor.Seek(prefix)
	return key != nil && len(key) >= len(prefix) && string(key[:len(prefix)]) == string(prefix)
}

// DueFloor returns the day before which every check counts as due.
func (s *BoltStore) DueFloor(ctx context.Context) (int, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	var floor int
	err := s.db.View(func(tx *bolt.Tx) error {
		floor = int(getUint(tx.Bucket(metaBucket), "due_floor"))
		return nil
	})
	return floor, err
}

// PendingFeed returns up to limit queued modules whose announcement is old enough to
// act on (see FeedReadyAt), oldest announcement first.
func (s *BoltStore) PendingFeed(ctx context.Context, now time.Time, floor time.Duration, limit int) ([]string, int, error) {
	if err := ctx.Err(); err != nil {
		return nil, 0, err
	}
	type queued struct {
		module string
		time   int64
	}
	var ready []queued
	total := 0
	err := s.db.View(func(tx *bolt.Tx) error {
		return tx.Bucket(feedBucket).ForEach(func(key, value []byte) error {
			total++
			var entry FeedEntry
			if err := json.Unmarshal(value, &entry); err != nil {
				return err
			}
			if entry.Time == 0 || !FeedReadyAt(entry.Time, floor).After(now) {
				ready = append(ready, queued{string(key), entry.Time})
			}
			return nil
		})
	})
	slices.SortFunc(ready, func(a, b queued) int {
		if a.time != b.time {
			return int(a.time - b.time)
		}
		return strings.Compare(a.module, b.module)
	})
	modules := make([]string, 0, min(limit, len(ready)))
	for _, item := range ready[:min(limit, len(ready))] {
		modules = append(modules, item.module)
	}
	return modules, total, err
}
