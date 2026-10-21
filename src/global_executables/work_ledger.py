"""Coordinator-free work reservations persisted in Git history.

Every worker reads the same ledger, chooses an unfinished unit, and attempts to push its
reservation. A push race is retried against the new head by the CLI. No process assigns
work: Git is only the compare-and-swap log for reservations, checkpoints, and evidence.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .booster import BUCKETS, owner_bucket

VERSION = 1
DEFAULT_TTL_HOURS = 12.0
PRUNE_AFTER_DAYS = 30
LEDGER_PATH = "data/production/work-ledger/{source}.json"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")


class LedgerConflict(RuntimeError):
    """A stale worker attempted to change work now owned by somebody else."""


class NoWork(RuntimeError):
    """The current catalogue generation has no unreserved unfinished unit."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unit_id(bucket: int) -> str:
    if not 0 <= bucket < BUCKETS:
        raise ValueError(f"unit must be within 0-{BUCKETS - 1}")
    return f"{bucket:02x}"


def catalogue_counts(path: Path, prefix: int = 0) -> tuple[dict[str, int], dict[str, int]]:
    """Return total and already-processed package counts for every hash bucket."""
    totals = {unit_id(bucket): 0 for bucket in range(BUCKETS)}
    positions = dict(totals)
    with path.open(encoding="utf-8") as stream:
        for index, raw in enumerate(stream):
            name = raw.rstrip("\r\n")
            if not name:
                raise ValueError(f"empty package name at catalogue index {index}")
            key = unit_id(owner_bucket(name))
            totals[key] += 1
            if index < prefix:
                positions[key] += 1
    return totals, positions


def empty_document(source: str) -> dict[str, Any]:
    if not _ID.fullmatch(source):
        raise ValueError("source must use 1-128 letters, digits, '.', '_' or '-'")
    return {"version": VERSION, "source": source, "catalog": None, "units": {},
            "reservations": {}, "history": []}


def _live(reservation: Any, now: datetime) -> bool:
    if not isinstance(reservation, dict):
        return False
    beat = _time(reservation.get("heartbeat"))
    try:
        ttl = float(reservation.get("ttl_hours", DEFAULT_TTL_HOURS))
    except (TypeError, ValueError):
        return False
    return beat is not None and now - beat < timedelta(hours=ttl)


def _validate_catalog(catalog: dict[str, Any]) -> None:
    if not _DIGEST.fullmatch(str(catalog.get("digest", ""))):
        raise ValueError("catalog digest must be sha256:<64 lowercase hex characters>")
    if type(catalog.get("size")) is not int or catalog["size"] < 0:
        raise ValueError("catalog size must be a non-negative integer")


def ensure_catalog(document: dict[str, Any], source: str, catalog: dict[str, Any],
                   sizes: dict[str, int], initial: dict[str, int]) -> dict[str, Any]:
    """Initialize or roll the ledger to a reproducible catalogue generation."""
    if document.get("version") != VERSION or document.get("source") != source:
        raise ValueError("ledger version or source does not match this operation")
    _validate_catalog(catalog)
    if set(sizes) != {unit_id(bucket) for bucket in range(BUCKETS)}:
        raise ValueError("catalogue sizes must describe all 256 units")
    if sum(sizes.values()) != catalog["size"]:
        raise ValueError("unit sizes do not add up to catalog size")
    current = document.get("catalog")
    if isinstance(current, dict) and current.get("digest") == catalog["digest"]:
        if any(document.get("units", {}).get(key, {}).get("size") != size
               for key, size in sizes.items()):
            raise ValueError("ledger unit sizes differ from the same catalog digest")
        return document

    previous_units = document.get("units", {}) if isinstance(document.get("units"), dict) else {}
    units: dict[str, dict[str, Any]] = {}
    for key, size in sorted(sizes.items()):
        old_position = previous_units.get(key, {}).get("position", 0)
        seed_position = initial.get(key, 0)
        position = min(size, max(old_position if isinstance(old_position, int) else 0,
                                 seed_position if isinstance(seed_position, int) else 0))
        units[key] = {"size": size, "position": position,
                      "status": "completed" if position == size else "available",
                      "failures": 0, "attempts": 0}
    history = list(document.get("history", [])) if isinstance(document.get("history"), list) else []
    if isinstance(current, dict):
        history.append({"catalog": current, "completed_units": sum(
            entry.get("status") == "completed" for entry in previous_units.values()
            if isinstance(entry, dict))})
    return {"version": VERSION, "source": source, "catalog": dict(catalog),
            "units": units, "reservations": {}, "history": history[-20:]}


def _prune(document: dict[str, Any], now: datetime) -> dict[str, Any]:
    reservations = document.get("reservations", {})
    cutoff = now - timedelta(days=PRUNE_AFTER_DAYS)
    kept = {actor: value for actor, value in reservations.items()
            if isinstance(value, dict) and (_time(value.get("heartbeat")) or now) > cutoff}
    return {**document, "reservations": kept}


def claim(document: dict[str, Any], actor: str, *, count: int = 1,
          ttl_hours: float = DEFAULT_TTL_HOURS, now: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reserve unfinished units without a coordinator; return the updated ledger and lease."""
    now = now or now_utc()
    if not _ID.fullmatch(actor):
        raise ValueError("actor must use 1-128 letters, digits, '.', '_' or '-'")
    if count < 1 or count > BUCKETS:
        raise ValueError(f"count must be within 1-{BUCKETS}")
    if not 0 < ttl_hours <= 168:
        raise ValueError("ttl_hours must be greater than zero and at most 168")
    document = _prune(document, now)
    catalog = document.get("catalog")
    if not isinstance(catalog, dict):
        raise ValueError("ledger has no catalog generation")
    reservations = dict(document.get("reservations", {}))
    existing = reservations.get(actor)
    if _live(existing, now) and existing.get("catalog_digest") == catalog.get("digest"):
        return document, existing
    held = {unit for owner, reservation in reservations.items() if owner != actor and _live(reservation, now)
            and reservation.get("catalog_digest") == catalog.get("digest")
            for unit in reservation.get("units", [])}
    units = document.get("units", {})
    available = [key for key, value in sorted(units.items())
                 if isinstance(value, dict) and value.get("status") != "completed" and key not in held]
    if not available:
        raise NoWork("no unreserved unfinished work remains for this catalog generation")
    start = int.from_bytes(hashlib.sha256(actor.encode()).digest()[:4], "big") % len(available)
    ordered = available[start:] + available[:start]
    selected = ordered[:count]
    reservation = {"units": selected, "catalog_digest": catalog["digest"],
                   "heartbeat": _stamp(now), "ttl_hours": ttl_hours,
                   "started_at": _stamp(now)}
    reservations[actor] = reservation
    updated = {**document, "reservations": reservations}
    return updated, reservation


def heartbeat(document: dict[str, Any], actor: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    reservations = dict(document.get("reservations", {}))
    reservation = reservations.get(actor)
    if not _live(reservation, now):
        raise LedgerConflict(f"actor {actor!r} has no live reservation")
    reservations[actor] = {**reservation, "heartbeat": _stamp(now)}
    return {**document, "reservations": reservations}


def release(document: dict[str, Any], actor: str) -> dict[str, Any]:
    reservations = {key: value for key, value in document.get("reservations", {}).items()
                    if key != actor}
    return {**document, "reservations": reservations}


def checkpoint(document: dict[str, Any], actor: str, unit: str, position: int, *,
               failures: int, complete: bool, result_commit: str,
               now: datetime | None = None) -> dict[str, Any]:
    """Advance one reserved unit after its crawl result is durable on the same Git branch."""
    now = now or now_utc()
    if not _COMMIT.fullmatch(result_commit):
        raise ValueError("result_commit must be a full Git object ID")
    reservations = dict(document.get("reservations", {}))
    reservation = reservations.get(actor)
    catalog = document.get("catalog", {})
    if (not _live(reservation, now) or reservation.get("catalog_digest") != catalog.get("digest")
            or unit not in reservation.get("units", [])):
        raise LedgerConflict(f"actor {actor!r} does not hold unit {unit}")
    units = {key: dict(value) for key, value in document.get("units", {}).items()}
    current = units.get(unit)
    if current is None:
        raise ValueError(f"unknown unit {unit}")
    if not current["position"] <= position <= current["size"]:
        raise ValueError("checkpoint position must advance monotonically within the unit")
    if failures < 0:
        raise ValueError("failure count cannot be negative")
    if complete and (position != current["size"] or failures):
        raise ValueError("a completed unit must reach its end with no retryable failures")
    current.update({"position": position, "failures": failures,
                    "status": "completed" if complete else "available",
                    "attempts": int(current.get("attempts", 0)) + 1,
                    "updated_at": _stamp(now), "actor": actor,
                    "result_commit": result_commit})
    units[unit] = current
    remaining = [item for item in reservation["units"] if item != unit]
    if remaining:
        reservations[actor] = {**reservation, "units": remaining, "heartbeat": _stamp(now)}
    else:
        reservations.pop(actor, None)
    return {**document, "units": units, "reservations": reservations}


def status(document: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    units = document.get("units", {})
    live = {actor: reservation for actor, reservation in document.get("reservations", {}).items()
            if _live(reservation, now)}
    return {"version": document.get("version"), "source": document.get("source"),
            "catalog": document.get("catalog"), "units": len(units),
            "completed_units": sum(value.get("status") == "completed" for value in units.values()),
            "remaining_packages": sum(max(0, value.get("size", 0) - value.get("position", 0))
                                      for value in units.values()),
            "retryable_failures": sum(value.get("failures", 0) for value in units.values()),
            "live_reservations": live}
