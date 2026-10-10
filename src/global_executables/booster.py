"""Local booster: ownership, leases and delta merging for a source crawled from two places.

A source such as PyPI has a backlog (588,000 never-visited packages) that the scheduled
Actions refresh cannot cover in a reasonable time.  A *booster* is a crawler on a user's
own machine that works through that backlog in parallel with Actions.  Three things keep
the two from fighting (docs/OPERATIONS.md "Local booster"):

* **Ownership by bucket.**  A package belongs to bucket ``sha256(name)[0]`` (0-255, the
  same function as ``gocrawl.OwnerBucket``).  A booster *leases* a range of buckets; while
  the lease is alive the Actions refresh leaves the rotation of those buckets to it
  (``--rotation-exclude``).  Feed work and retries stay with Actions.
* **A lease with a heartbeat.**  ``data/production/booster/<source>.json`` on
  ``artifact-data`` lists leases ``{ranges, heartbeat, ttl_hours}``.  A booster renews the
  heartbeat whenever it publishes; a lease whose heartbeat is older than its TTL is dead,
  so a stopped or sleeping machine returns its range to Actions without anybody doing
  anything and coverage never stalls.
* **Delta publication.**  Publishing the whole source would let the last writer erase the
  other's work.  A writer instead publishes only what *it* changed since the snapshot it
  last published (``base``): per-package map entries, retry-list members and the rows of
  the packages that differ.  Applying the same delta twice gives the same result, and the
  delta of one writer never touches a package the other changed.

Bookkeeping (the schedule cache) is local to each writer and never published.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

BUCKETS = 256
LEASE_VERSION = 1
DEFAULT_TTL_HOURS = 12.0
# A lease nobody has renewed for this long is dropped from the file altogether.
PRUNE_AFTER_DAYS = 14
LEASE_PATH = "data/production/booster/{source}.json"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class LeaseConflict(Exception):
    """Another live lease already owns part of the requested range."""


def owner_bucket(name: str) -> int:
    return hashlib.sha256(name.encode("utf-8")).digest()[0]


def parse_ranges(text: str) -> frozenset[int]:
    """``"0-63,128-191"`` -> set of buckets; an empty string is the empty set."""
    buckets: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        low, _, high = part.partition("-")
        try:
            first, last = int(low), int(high or low)
        except ValueError:
            raise ValueError(f"invalid bucket range {part!r}") from None
        if not (0 <= first <= last < BUCKETS):
            raise ValueError(f"invalid bucket range {part!r} (want lo-hi within 0-{BUCKETS - 1})")
        buckets.update(range(first, last + 1))
    return frozenset(buckets)


def format_ranges(buckets: Iterable[int]) -> str:
    """Canonical text of a bucket set: ``"0-63,128-191"``."""
    ordered = sorted(set(buckets))
    parts: list[str] = []
    start = previous = None
    for bucket in ordered + [None]:  # type: ignore[list-item]
        if start is None:
            start = previous = bucket
        elif bucket is not None and bucket == previous + 1:
            previous = bucket
        else:
            parts.append(str(start) if start == previous else f"{start}-{previous}")
            start = previous = bucket
    return ",".join(parts)


def slice_ranges(index: int, count: int) -> str:
    """The ``index``-th of ``count`` equal slices of the buckets (for extra machines)."""
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"slice {index}/{count} is not valid")
    low, high = index * BUCKETS // count, (index + 1) * BUCKETS // count - 1
    return format_ranges(range(low, high + 1))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def empty_document() -> dict[str, Any]:
    return {"version": LEASE_VERSION, "leases": {}}


def read_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return empty_document()
    try:
        document = json.loads(path.read_text())
    except ValueError:
        return empty_document()
    if not isinstance(document, dict) or not isinstance(document.get("leases"), dict):
        return empty_document()
    return document


def write_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def is_live(lease: Any, now: datetime) -> bool:
    """A lease is alive until ``ttl_hours`` after its last heartbeat."""
    if not isinstance(lease, dict):
        return False
    beat = _parse_time(lease.get("heartbeat"))
    try:
        ttl = float(lease.get("ttl_hours", DEFAULT_TTL_HOURS))
    except (TypeError, ValueError):
        return False
    return beat is not None and now - beat < timedelta(hours=ttl)


def live_ranges(document: dict[str, Any], now: datetime, exclude: str | None = None) -> frozenset[int]:
    """Buckets held by live leases (optionally ignoring one lease id)."""
    buckets: set[int] = set()
    for lease_id, lease in document.get("leases", {}).items():
        if lease_id != exclude and is_live(lease, now):
            try:
                buckets |= parse_ranges(str(lease.get("ranges", "")))
            except ValueError:
                continue
    return frozenset(buckets)


def actions_exclusion(document: dict[str, Any], now: datetime | None = None) -> str:
    """What the Actions refresh must leave alone right now (empty: it owns everything)."""
    return format_ranges(live_ranges(document, now or now_utc()))


def acquire(document: dict[str, Any], lease_id: str, ranges: str, now: datetime | None = None,
            ttl_hours: float = DEFAULT_TTL_HOURS, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create or renew ``lease_id`` over ``ranges``; refuse an overlap with another live lease."""
    now = now or now_utc()
    if not _ID.match(lease_id):
        raise ValueError(f"lease id {lease_id!r} must be letters, digits, '.', '_' or '-'")
    wanted = parse_ranges(ranges)
    if not wanted:
        raise ValueError("a lease needs at least one bucket")
    clash = wanted & live_ranges(document, now, exclude=lease_id)
    if clash:
        raise LeaseConflict(f"buckets {format_ranges(clash)} are held by another live lease")
    leases = dict(document.get("leases", {}))
    previous = leases.get(lease_id) if isinstance(leases.get(lease_id), dict) else {}
    entry = {"ranges": format_ranges(wanted), "heartbeat": now.isoformat(), "ttl_hours": ttl_hours,
             "started_at": previous.get("started_at") if is_live(previous, now) and previous.get("started_at") else now.isoformat()}
    if progress is not None:
        entry["progress"] = progress
    elif "progress" in previous:
        entry["progress"] = previous["progress"]
    leases[lease_id] = entry
    cutoff = now - timedelta(days=PRUNE_AFTER_DAYS)
    leases = {key: value for key, value in leases.items()
              if key == lease_id or (_parse_time(value.get("heartbeat")) or now) > cutoff}
    return {"version": LEASE_VERSION, "leases": leases}


def release(document: dict[str, Any], lease_id: str) -> dict[str, Any]:
    leases = {key: value for key, value in document.get("leases", {}).items() if key != lease_id}
    return {"version": LEASE_VERSION, "leases": leases}


# --- delta merge -------------------------------------------------------------------------

def _is_retry_list(key: str, value: Any) -> bool:
    return key.startswith("retry_") and isinstance(value, list)


def merge_source_delta(published: dict[str, Any], local: dict[str, Any], base: dict[str, Any],
                       scalars: bool) -> tuple[dict[str, Any], bool]:
    """Apply what ``local`` changed relative to ``base`` onto ``published``.

    Per-package maps (``unavailable``, ``failures``, ``feed_pending`` ...) are merged key by
    key, retry lists member by member, so the other writer's entries survive.  With
    ``scalars`` the local checkpoint fields (cursors, catalogue identity, feed cursor)
    are taken as well, as the Actions writer does; a booster passes ``scalars=False`` and
    leaves them to Actions.  Returns the merged source and whether anything changed.
    """
    merged = dict(published)
    changed = False
    for key in sorted(set(local) | set(base)):
        value, before = local.get(key), base.get(key)
        if isinstance(value, dict) or isinstance(before, dict):
            new, old = value if isinstance(value, dict) else {}, before if isinstance(before, dict) else {}
            target = dict(merged.get(key) if isinstance(merged.get(key), dict) else {})
            for name in set(new) | set(old):
                if new.get(name) == old.get(name):
                    continue
                if name in new:
                    if target.get(name) != new[name]:
                        target[name] = new[name]
                        changed = True
                elif name in target:
                    del target[name]
                    changed = True
            if target or key in merged:
                merged[key] = target
        elif _is_retry_list(key, value) or _is_retry_list(key, before):
            new, old = list(value or []), list(before or [])
            added = [name for name in new if name not in old]
            removed = {name for name in old if name not in new}
            target = [name for name in (merged.get(key) or []) if name not in removed]
            target += [name for name in added if name not in target]
            if target != (merged.get(key) or []):
                changed = True
            merged[key] = target
        elif scalars and key in local and merged.get(key) != value and key != "snapshot_generation":
            merged[key] = value
            changed = True
    if changed:
        generation = max(int(published.get("snapshot_generation") or 0),
                         int(local.get("snapshot_generation") or 0) if scalars else 0)
        merged["snapshot_generation"] = generation + 1
    return merged, changed


def _package_of(line: str) -> str | None:
    try:
        value = json.loads(line)
    except ValueError:
        return None
    package = value.get("package") if isinstance(value, dict) else None
    return package if isinstance(package, str) else None


def package_digests(path: Path | None) -> dict[str, str]:
    """``package -> digest of its sorted rows`` of a JSONL file (empty when absent)."""
    lines: dict[str, list[str]] = {}
    if path is not None and path.is_file():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.rstrip("\n")
                package = _package_of(line) if line else None
                if package is not None:
                    lines.setdefault(package, []).append(line)
    return {package: hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest() for package, rows in lines.items()}


def merge_rows_delta(published: Path | None, local: Path, base: Path | None, output: Path) -> tuple[int, int]:
    """Write ``published`` with the rows of every package ``local`` changed since ``base``.

    Returns ``(touched packages, rows written for them)``.  Untouched rows keep their order;
    the touched packages' rows follow, sorted, so applying the same delta again reproduces
    the same bytes.
    """
    before, after = package_digests(base), package_digests(local)
    touched = {package for package in set(before) | set(after) if before.get(package) != after.get(package)}
    replacement: dict[str, list[str]] = {}
    with local.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.rstrip("\n")
            package = _package_of(line) if line else None
            if package in touched:
                replacement.setdefault(package, []).append(line)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    written = 0
    with temporary.open("w", encoding="utf-8") as sink:
        if published is not None and published.is_file():
            with published.open(encoding="utf-8") as stream:
                for line in stream:
                    stripped = line.rstrip("\n")
                    if stripped and _package_of(stripped) in touched:
                        continue
                    sink.write(line if line.endswith("\n") else line + "\n")
        for package in sorted(replacement):
            for line in sorted(replacement[package]):
                sink.write(line + "\n")
                written += 1
    temporary.replace(output)
    return len(touched), written


Operation = Callable[[dict[str, Any]], dict[str, Any]]
