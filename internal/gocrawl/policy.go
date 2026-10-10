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

// Outcome says what the last look at a package found, so the cache also knows the
// packages that ship no command (most of every registry) and a visit that finds the same
// version skips their artifacts as well.
const (
	OutcomeUnknown = ""  // a check written before outcomes existed
	NoCommands     = "n" // the version was read and has no command
	HasCommands    = "c" // the version was read and has rows
)

// Check records the last successful look at a package: the day (days since the Unix
// epoch, UTC), how many consecutive looks found the same version, the outcome and that
// version.
type Check struct {
	Day     int
	Streak  int
	Outcome string
	Version string
}

// EncodeCheck renders a check as "<day>:<streak><outcome>:<version>", the compact value
// stored in the schedule cache; the outcome letter is absent on unknown.
func EncodeCheck(check Check) string {
	return strconv.Itoa(check.Day) + ":" + strconv.Itoa(check.Streak) + check.Outcome + ":" + check.Version
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
	outcome := OutcomeUnknown
	if last := len(streakText) - 1; last > 0 {
		if letter := streakText[last:]; letter == NoCommands || letter == HasCommands {
			outcome, streakText = letter, streakText[:last]
		}
	}
	day, dayErr := strconv.Atoi(dayText)
	streak, streakErr := strconv.Atoi(streakText)
	if dayErr != nil || streakErr != nil || day < 0 || streak < 0 {
		return Check{}, false
	}
	return Check{Day: day, Streak: streak, Outcome: outcome, Version: version}, true
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

// Next returns the check after a successful look that found `latest` with `outcome`
// (empty keeps what the previous check recorded for an unchanged version). changed
// reports whether the version differs from the previous check.
func Next(previous Check, hadPrevious bool, latest, outcome string, today int) (Check, bool) {
	if hadPrevious && previous.Version == latest {
		if outcome == OutcomeUnknown {
			outcome = previous.Outcome
		}
		return Check{Day: today, Streak: previous.Streak + 1, Outcome: outcome, Version: latest}, false
	}
	return Check{Day: today, Streak: 0, Outcome: outcome, Version: latest}, true
}

// FeedReadyAt is the earliest time a feed event may be acted on: registries serve the
// document through a cache for `floor`, so reading it sooner can return the old version.
func FeedReadyAt(eventUnix int64, floor time.Duration) time.Time {
	return time.Unix(eventUnix, 0).Add(floor)
}

func coldDigest(module string) uint32 {
	digest := sha256.Sum256([]byte("cold:" + module))
	return uint32(digest[0])<<8 | uint32(digest[1])
}
