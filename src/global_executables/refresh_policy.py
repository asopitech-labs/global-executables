"""Change-driven refresh policy shared by the Python crawlers.

The latest version of a package is looked up again on a rotation (``refresh_cursor``).
Without this module every visit re-read the package's artifacts whether or not its
latest version had changed.  The policy records, per package, the last successful
check in the ``checked`` map of the registry state and uses it three ways:

* P0, version-equal skip: a visit that finds the recorded latest version keeps the
  stored rows and skips the artifact download.
* P2, backoff: each unchanged visit doubles the package's re-check interval, up to a
  per-source cap, and the rotation passes over packages that are not due without
  spending budget.
* P3, token bucket with reservation and a per-source feed floor.

``internal/gocrawl/policy.go`` implements the same interval function for the Go
crawler; ``internal/gocrawl/testdata/refresh/policy-golden.json`` is checked by both test suites.
A check is the string ``"<day>:<streak>:<version>"``: compact, and one scalar so the
registry state shards it like any other map.  ETags never enter the state.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

BACKOFF_MIN_DAYS = 1
# A source whose changes are also announced by a feed may back off further: a missed
# announcement costs at most this long.  A source without a complete feed is bounded
# tighter.
BACKOFF_MAX_DAYS_FEED = 60
BACKOFF_MAX_DAYS_PLAIN = 14
# Hard TTL: a check older than this is reported as stale whatever the backoff says.
HARD_TTL_DAYS = 120
CHECKED_FIELD = "checked"
# Identifies the logic that turns a registry document into rows.  State written by a
# lower revision is not trusted to skip artifact reads; raise it when extraction changes.
EXTRACTION_REVISION = 1


def today(now: datetime | None = None) -> int:
    """Days since the Unix epoch (UTC), the unit of a check's day."""
    now = now or datetime.now(timezone.utc)
    return int(now.timestamp() // 86400)


def pack(day: int, streak: int, version: str) -> str:
    return f"{day}:{streak}:{version}"


def unpack(value: Any) -> tuple[int, int, str] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(":", 2)
    if len(parts) != 3:
        return None
    try:
        day, streak = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if day < 0 or streak < 0:
        return None
    return day, streak, parts[2]


def recheck_interval(key: str, streak: int, max_days: int) -> int:
    """Days to wait after ``streak`` consecutive unchanged looks.

    ``2**streak`` days capped at ``max_days`` with a deterministic -10%..+10% spread
    derived from the key, so packages first seen together do not all fall due on one
    day.  Integer arithmetic only: identical to ``gocrawl.RecheckInterval``.
    """
    base = BACKOFF_MIN_DAYS
    for _ in range(streak):
        base *= 2
        if base >= max_days:
            base = max_days
            break
    base = min(base, max_days)
    permille = 900 + hashlib.sha256(key.encode("utf-8")).digest()[0] * 200 // 255
    return min(max_days, max(BACKOFF_MIN_DAYS, (base * permille + 500) // 1000))


def is_due(checked: dict[str, Any], key: str, day: int, max_days: int, due_floor: int = 0) -> bool:
    """A package with no usable check, or one older than ``due_floor``, is always due."""
    entry = unpack(checked.get(key))
    if entry is None:
        return True
    last, streak, _ = entry
    if last < due_floor:
        return True
    return day - last >= recheck_interval(key, streak, max_days)


def known_version(checked: dict[str, Any], key: str) -> str | None:
    entry = unpack(checked.get(key))
    return entry[2] if entry and entry[2] else None


def record_check(checked: dict[str, Any], key: str, latest: str, day: int) -> bool:
    """Record a successful look that found ``latest``; True when the version changed."""
    previous = unpack(checked.get(key))
    if previous is not None and previous[2] == latest:
        checked[key] = pack(day, previous[1] + 1, latest)
        return False
    checked[key] = pack(day, 0, latest)
    return True


def forget(checked: dict[str, Any], key: str) -> None:
    checked.pop(key, None)


def seed_from_rows(checked: dict[str, Any], rows: list[dict[str, Any]],
                   key_of: Callable[[dict[str, Any]], str] | None = None) -> int:
    """Seed checks from stored observation rows so the first re-check can already skip.

    A seeded check has day 0: it is due at once, yet a visit that finds the same
    version avoids the artifact download.
    """
    seeded = 0
    for row in rows:
        key = key_of(row) if key_of else row.get("package")
        version = row.get("latest_version") or row.get("version")
        if not key or not version or key in checked:
            continue
        checked[key] = pack(0, 0, str(version))
        seeded += 1
    return seeded


def classify(age_days: int, soft_days: int, hard_days: int = HARD_TTL_DAYS) -> str:
    """Soft/hard TTL: fresh until the backoff interval, due until the hard limit, then stale."""
    if age_days < soft_days:
        return "fresh"
    return "due" if age_days < hard_days else "stale"


def ttl_summary(checked: dict[str, Any], day: int, max_days: int, hard_days: int = HARD_TTL_DAYS) -> dict[str, int]:
    counts = {"fresh": 0, "due": 0, "stale": 0, "unchecked": 0}
    for key, value in checked.items():
        entry = unpack(value)
        if entry is None:
            counts["unchecked"] += 1
            continue
        counts[classify(day - entry[0], recheck_interval(key, entry[1], max_days), hard_days)] += 1
    return counts


def feed_ready_at(event_unix: float, floor_seconds: float) -> float:
    """Earliest time an announcement may be acted on (registries cache documents for the floor)."""
    return event_unix + floor_seconds


class TokenBucket:
    """Token bucket with reservation.

    ``reserve()`` always succeeds and returns how long the caller must wait; tokens are
    claimed at once, so concurrent callers queue behind each other instead of racing.
    ``rate`` is tokens per second and ``burst`` the bucket size.  ``clock`` and ``sleep``
    are injectable for tests.
    """

    def __init__(self, rate: float, burst: int = 1, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be positive and burst at least 1")
        self.rate, self.burst, self._clock, self._sleep = rate, burst, clock, sleep
        self._tokens = float(burst)
        self._stamp = clock()
        self._lock = threading.Lock()

    def reserve(self, tokens: int = 1) -> float:
        with self._lock:
            now = self._clock()
            self._tokens = min(float(self.burst), self._tokens + (now - self._stamp) * self.rate)
            self._stamp = now
            self._tokens -= tokens
            return max(0.0, -self._tokens / self.rate)

    def wait(self, tokens: int = 1) -> float:
        delay = self.reserve(tokens)
        if delay > 0:
            self._sleep(delay)
        return delay
