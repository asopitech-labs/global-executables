package gocrawl

import (
	"crypto/sha256"
	"strconv"
	"strings"
	"time"
)

// ExtractionRevision identifies the logic that turns a registry document into
// observations. A recorded check is only trusted to skip re-reading artifacts while
// the state was written by the same revision; raising this number makes the next
// run re-inspect every package once and rebuild the checks (see LoadSourceCompatibility).
var ExtractionRevision = 1

// Backoff bounds, in days. A package whose latest version did not change since the
// previous check is re-checked after twice the previous interval, up to the cap.
const (
	BackoffMinDays = 1
	// BackoffMaxDaysFeed bounds the interval for a source whose changes are also
	// announced by a feed: a missed announcement costs at most this long.
	BackoffMaxDaysFeed = 60
	// BackoffMaxDaysPlain bounds it for a source without a complete change feed.
	BackoffMaxDaysPlain = 14
	// MaxScanFactor bounds how many catalog entries one pass reads, as a multiple of
	// its package budget, while looking for entries that are due.
	MaxScanFactor = 20
)

// Check records the last successful look at a package: the day (days since the Unix
// epoch, UTC), how many consecutive looks found the same version, and that version.
type Check struct {
	Day     int
	Streak  int
	Version string
}

// EncodeCheck renders a check as "<day>:<streak>:<version>", the compact value stored
// in the shared registry state.
func EncodeCheck(check Check) string {
	return strconv.Itoa(check.Day) + ":" + strconv.Itoa(check.Streak) + ":" + check.Version
}

// ParseCheck is the inverse of EncodeCheck.
func ParseCheck(value string) (Check, bool) {
	dayText, rest, ok := strings.Cut(value, ":")
	if !ok {
		return Check{}, false
	}
	streakText, version, ok := strings.Cut(rest, ":")
	if !ok {
		return Check{}, false
	}
	day, dayErr := strconv.Atoi(dayText)
	streak, streakErr := strconv.Atoi(streakText)
	if dayErr != nil || streakErr != nil || day < 0 || streak < 0 {
		return Check{}, false
	}
	return Check{Day: day, Streak: streak, Version: version}, true
}

// Today returns the day number used by checks.
func Today(now time.Time) int {
	return int(now.UTC().Unix() / 86400)
}

// RecheckInterval returns the days to wait before the package is looked at again
// after `streak` consecutive unchanged looks: 2^streak days capped at maxDays, with a
// deterministic spread of -10%..+10% derived from the package name so packages that
// were first seen together do not all fall due on the same day. Integer arithmetic
// only, so the Python implementation returns the same value.
func RecheckInterval(module string, streak, maxDays int) int {
	base := BackoffMinDays
	for range streak {
		base *= 2
		if base >= maxDays {
			base = maxDays
			break
		}
	}
	base = min(base, maxDays)
	digest := sha256.Sum256([]byte(module))
	permille := 900 + int(digest[0])*200/255
	return min(maxDays, max(BackoffMinDays, (base*permille+500)/1000))
}

// Due reports whether a package must be looked at again on `today`. A package with no
// check, or one checked before dueFloor (a resynchronisation), is always due.
func (c Check) Due(module string, today, dueFloor, maxDays int) bool {
	if c.Day < dueFloor {
		return true
	}
	return today-c.Day >= RecheckInterval(module, c.Streak, maxDays)
}

// Next returns the check after a successful look that found `latest`. changed reports
// whether the version differs from the previous check.
func Next(previous Check, hadPrevious bool, latest string, today int) (Check, bool) {
	if hadPrevious && previous.Version == latest {
		return Check{Day: today, Streak: previous.Streak + 1, Version: latest}, false
	}
	return Check{Day: today, Streak: 0, Version: latest}, true
}

// FeedReadyAt is the earliest time a feed event may be acted on: registries serve the
// document through a cache for `floor`, so reading it sooner can return the old version.
func FeedReadyAt(eventUnix int64, floor time.Duration) time.Time {
	return time.Unix(eventUnix, 0).Add(floor)
}
