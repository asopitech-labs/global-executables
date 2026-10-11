package gocrawl

import (
	"crypto/sha256"
	"fmt"
	"strconv"
	"strings"
)

// OwnerBuckets is how many partitions the catalogue is split into for ownership: the
// first byte of sha256(name). A local booster declares the bucket ranges it owns; the
// Actions refresh leaves those rotations to it while its lease is alive
// (docs/OPERATIONS.md "Local booster"). Identical in Python (`booster.owner_bucket`);
// testdata/refresh/owner-golden.json is checked by both test suites.
const OwnerBuckets = 256

// OwnerBucket returns the partition of a package name.
func OwnerBucket(name string) int {
	return int(sha256.Sum256([]byte(name))[0])
}

// RangeSet is a set of buckets written "lo-hi,lo-hi" (inclusive); a single number is a
// one-bucket range.
type RangeSet struct {
	bits [OwnerBuckets]bool
	any  bool
}

// ParseRanges parses "0-63,128-191". The empty string is the empty set.
func ParseRanges(text string) (RangeSet, error) {
	var set RangeSet
	for part := range strings.SplitSeq(text, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		low, high, found := strings.Cut(part, "-")
		if !found {
			high = low
		}
		first, errLow := strconv.Atoi(low)
		last, errHigh := strconv.Atoi(high)
		if errLow != nil || errHigh != nil || first < 0 || last >= OwnerBuckets || first > last {
			return RangeSet{}, fmt.Errorf("invalid bucket range %q (want lo-hi within 0-%d)", part, OwnerBuckets-1)
		}
		for bucket := first; bucket <= last; bucket++ {
			set.bits[bucket] = true
		}
		set.any = true
	}
	return set, nil
}

// Empty reports whether the set has no bucket.
func (s RangeSet) Empty() bool { return !s.any }

// Contains reports whether the package name falls in the set.
func (s RangeSet) Contains(name string) bool { return s.bits[OwnerBucket(name)] }
