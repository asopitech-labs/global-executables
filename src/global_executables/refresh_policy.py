"""Change-driven refresh policy shared by the Python crawlers.

The latest version of a package is looked up again on a rotation (``refresh_cursor``).
Without this module every visit re-read the package's artifacts whether or not its
latest version had changed.  The policy records, per package, the last successful
check in the *schedule cache* (see "History and cache" in docs/OPERATIONS.md) and uses
it three ways.  Checks are bookkeeping, not history: they live in a cache file that is
never committed and may be lost at any time, so an unchanged package never writes to
the registry state (#66; #65 stored them in the state's ``checked`` map, which is still
read for one release):

* P0, version-equal skip: a visit that finds the recorded latest version keeps the
  stored rows and skips the artifact download.
* P2, backoff: each unchanged visit doubles the package's re-check interval, up to a
  per-source cap, and the rotation passes over packages that are not due without
  spending budget.
* P3, token bucket with reservation and a per-source feed floor.

``internal/gocrawl/policy.go`` implements the same interval function for the Go
crawler; ``internal/gocrawl/testdata/refresh/policy-golden.json`` is checked by both test suites.
A check is the string ``"<day>:<streak><outcome>:<version>"`` (outcome ``n``/``c``, optional).  ETags never enter the state.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
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
# Keys of a source's in-memory state that belong to the cache and are stripped before
# the state is saved (``registry_artifact._save_state``).
CACHE_KEYS = (CHECKED_FIELD, "refresh_cursor", "refresh_cursor_cold")
CACHE_MAGIC = "# global-executables schedule cache v1"
# 3 million checks are about 25 MB compressed; past it the oldest checks are dropped
# and simply become due.
MAX_CACHE_ENTRIES = 3_000_000
COLD_STAGGER_STREAKS = 4
# Identifies the logic that turns a registry document into rows.  State written by a
# lower revision is not trusted to skip artifact reads; raise it when extraction changes.
EXTRACTION_REVISION = 1


def today(now: datetime | None = None) -> int:
    """Days since the Unix epoch (UTC), the unit of a check's day."""
    now = now or datetime.now(timezone.utc)
    return int(now.timestamp() // 86400)


NO_COMMANDS = "n"   # the version was read and ships no command (most of a registry)
HAS_COMMANDS = "c"  # the version was read and has rows
OUTCOMES = {NO_COMMANDS: "no_commands", HAS_COMMANDS: "has_commands"}


def pack(day: int, streak: int, version: str, outcome: str = "") -> str:
    """``"<day>:<streak><outcome>:<version>"``; the outcome letter is absent when unknown."""
    return f"{day}:{streak}{outcome}:{version}"


def unpack_full(value: Any) -> tuple[int, int, str, str] | None:
    """``(day, streak, version, outcome)`` of a check, or None for garbage."""
    if not isinstance(value, str):
        return None
    parts = value.split(":", 2)
    if len(parts) != 3:
        return None
    streak_text, outcome = parts[1], ""
    if len(streak_text) > 1 and streak_text[-1] in OUTCOMES:
        streak_text, outcome = streak_text[:-1], streak_text[-1]
    try:
        day, streak = int(parts[0]), int(streak_text)
    except ValueError:
        return None
    if day < 0 or streak < 0:
        return None
    return day, streak, parts[2], outcome


def unpack(value: Any) -> tuple[int, int, str] | None:
    entry = unpack_full(value)
    return entry[:3] if entry else None


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


def record_check(checked: dict[str, Any], key: str, latest: str, day: int, outcome: str = "") -> bool:
    """Record a successful look that found ``latest``; True when the version changed.

    ``outcome`` is NO_COMMANDS or HAS_COMMANDS; empty keeps the previous outcome of an
    unchanged version.  Every inspected package records one, with or without commands.
    """
    previous = unpack_full(checked.get(key))
    if previous is not None and previous[2] == latest:
        checked[key] = pack(day, previous[1] + 1, latest, outcome or previous[3])
        return False
    checked[key] = pack(day, 0, latest, outcome)
    return True


def forget(checked: dict[str, Any], key: str) -> None:
    checked.pop(key, None)


def cold_check(key: str, version: str, day: int) -> str:
    """The check assumed for a package history knows but the cache does not.

    Due now, with a streak spread by name over the first backoff tiers so the next looks
    do not all land together.  Identical to ``gocrawl.ColdCheck``.
    """
    streak = int.from_bytes(hashlib.sha256(("cold:" + key).encode("utf-8")).digest()[:2], "big") % COLD_STAGGER_STREAKS
    return pack(day - recheck_interval(key, streak, BACKOFF_MAX_DAYS_PLAIN), streak, version, HAS_COMMANDS)


def cold_refresh_start(size: int, budget: int, now: datetime | None = None) -> int:
    """Where the rotation begins without a cache: one budget further per six-hour window,
    so repeated cold starts still sweep the whole catalogue.  Same as ``gocrawl.ColdRefreshStart``."""
    if size <= 0 or budget <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    return (int(now.timestamp() // (6 * 3600)) * budget) % size


def seed_from_rows(checked: dict[str, Any], rows: list[dict[str, Any]],
                   key_of: Callable[[dict[str, Any]], str] | None = None, day: int | None = None) -> int:
    """Seed checks from stored observation rows so a cold start can still skip artifacts.

    A seeded check is due at once (see ``cold_check``), yet a visit that finds the same
    version avoids the artifact download.
    """
    day = today() if day is None else day
    seeded = 0
    for row in rows:
        key = key_of(row) if key_of else row.get("package")
        version = row.get("latest_version") or row.get("version")
        if not key or not version or key in checked:
            continue
        checked[key] = cold_check(key, str(version), day)
        seeded += 1
    return seeded


def read_cache(path: Path, revision: int = EXTRACTION_REVISION) -> tuple[dict[str, str], int, bool]:
    """Load a schedule cache: ``(checks, rotation cursor, found)``.

    A missing file, another format or another extraction revision is a cold start
    (``found`` False), never an error; so is an unreadable file.  Format shared with
    ``gocrawl.ReadCache``.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            if stream.readline().rstrip("\n") != CACHE_MAGIC:
                return {}, 0, False
            checks: dict[str, str] = {}
            cursor = 0
            for line in stream:
                line = line.rstrip("\n")
                if line.startswith("# extraction "):
                    if line[len("# extraction "):] != str(revision):
                        return {}, 0, False
                elif line.startswith("# cursor "):
                    cursor = int(line[len("# cursor "):] or 0)
                elif not line.startswith("#") and "\t" in line:
                    name, value = line.split("\t", 1)
                    if unpack(value) is not None:
                        checks[name] = value
            return checks, cursor, True
    except (OSError, EOFError, ValueError):
        return {}, 0, False


def write_cache(path: Path, checks: dict[str, Any], cursor: int, revision: int = EXTRACTION_REVISION) -> None:
    """Store the cache atomically; names sorted so equal caches are equal files."""
    names = [name for name, value in checks.items()
             if name and "\t" not in name and "\n" not in name and unpack(value) is not None]
    if len(names) > MAX_CACHE_ENTRIES:
        names.sort(key=lambda name: unpack(checks[name])[0], reverse=True)
        names = names[:MAX_CACHE_ENTRIES]
    names.sort()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cache-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as stream:
            stream.write(f"{CACHE_MAGIC}\n# extraction {revision}\n# cursor {cursor}\n".encode("utf-8"))
            for name in names:
                stream.write(f"{name}\t{checks[name]}\n".encode("utf-8"))
        os.chmod(temporary, 0o644)  # readable by the CI cache action even if the writer ran as another user
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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


def outcome_summary(checked: dict[str, Any]) -> dict[str, int]:
    """How many recorded checks found no command, found commands, or predate outcomes."""
    counts = {"no_commands": 0, "has_commands": 0, "unknown": 0}
    for value in checked.values():
        entry = unpack_full(value)
        if entry is not None:
            counts[OUTCOMES.get(entry[3], "unknown")] += 1
    return counts
