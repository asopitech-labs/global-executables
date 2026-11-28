#!/usr/bin/env python3
"""Publish and merge local-booster schedule-cache handovers.

Only explicitly enabled sources use a handover.  A handover is disposable scheduling
state: malformed, stale-format, missing, or unreadable input is always ignored rather
than failing a crawl.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from global_executables.refresh_policy import (  # noqa: E402
    EXTRACTION_REVISION,
    read_cache,
    unpack_full,
    write_cache,
)


HANDOVER_SOURCES = frozenset({"pypi"})
HANDOVER_INTERVAL_SECONDS = 24 * 60 * 60
MAX_HANDOVER_BYTES = 40_000_000


def enabled(source: str) -> bool:
    """Whether ``source`` publishes and consumes a schedule-cache handover."""
    return source in HANDOVER_SOURCES


def metadata_path(target: Path) -> Path:
    """Return the sidecar path beside a handover cache target."""
    suffix = ".cache.gz"
    stem = target.name.removesuffix(suffix)
    return target.with_name(f"{stem}.json")


def _published_at(metadata: Path) -> int | None:
    try:
        value = json.loads(metadata.read_text()).get("published_at")
    except (AttributeError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def should_publish(
    snapshot: Path,
    target: Path,
    now: int,
    force: bool,
) -> tuple[bool, str]:
    """Return ``(publish, reason)`` for a frozen cache snapshot."""
    try:
        size = snapshot.stat().st_size
    except OSError:
        return False, "missing-snapshot"
    if size > MAX_HANDOVER_BYTES:
        return False, "too-large"
    _, _, found = read_cache(snapshot)
    if not found:
        return False, "cold"
    try:
        if target.is_file() and snapshot.read_bytes() == target.read_bytes():
            return False, "unchanged"
    except OSError:
        return False, "unreadable-target"
    if not target.exists():
        return True, "missing"
    if force:
        return True, "forced"
    published_at = _published_at(metadata_path(target))
    if published_at is None or now - published_at > HANDOVER_INTERVAL_SECONDS:
        return True, "stale"
    return False, "fresh"


def stage(
    snapshot: Path,
    target: Path,
    source: str,
    now: int,
    force: bool,
) -> bool:
    """Copy an eligible frozen snapshot into the artifact-data worktree."""
    if not enabled(source):
        print(f"handover {source}: skipped (source-disabled)")
        return False
    publish, reason = should_publish(snapshot, target, now, force)
    if not publish:
        print(f"handover {source}: skipped ({reason})")
        return False
    checks, _, found = read_cache(snapshot)
    if not found:
        print(f"handover {source}: skipped (cold)")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata = metadata_path(target)
    cache_temp = target.with_name(f".{target.name}.tmp")
    metadata_temp = metadata.with_name(f".{metadata.name}.tmp")
    try:
        shutil.copyfile(snapshot, cache_temp)
        document = {
            "published_at": now,
            "entries": len(checks),
            "extraction_revision": EXTRACTION_REVISION,
            "sha256": hashlib.sha256(cache_temp.read_bytes()).hexdigest(),
        }
        metadata_temp.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        )
        os.replace(cache_temp, target)
        os.replace(metadata_temp, metadata)
    finally:
        cache_temp.unlink(missing_ok=True)
        metadata_temp.unlink(missing_ok=True)
    print(f"handover {source}: published ({reason})")
    return True


def merge(
    base: Path,
    handover: Path,
    source: str,
    metadata: Path | None = None,
    now: int | None = None,
) -> bool:
    """Merge ``handover`` into ``base`` by newest check day and rewrite ``base``."""
    if not enabled(source):
        print(f"handover {source}: source is not enabled; base left unchanged")
        return False
    handover_checks, handover_cursor, handover_found = read_cache(handover)
    if not handover_found:
        print(f"handover {source}: handover cache is missing or cold; base left unchanged")
        return False

    base_checks, base_cursor, base_found = read_cache(base)
    merged = dict(base_checks) if base_found else {}
    selected_from_handover: dict[str, bool] = {name: False for name in merged}
    for name, value in handover_checks.items():
        handover_entry = unpack_full(value)
        base_entry = unpack_full(merged.get(name))
        if base_entry is None or (handover_entry is not None and handover_entry[0] > base_entry[0]):
            merged[name] = value
            selected_from_handover[name] = True

    write_cache(base, merged, base_cursor if base_found else handover_cursor)
    written, _, _ = read_cache(base)
    summary = {
        "base_entries": len(base_checks) if base_found else 0,
        "handover_entries": len(handover_checks),
        "taken_from_handover": sum(selected_from_handover.get(name, False) for name in written),
        "kept_base": sum(not selected_from_handover.get(name, False) for name in written),
        "total": len(written),
    }
    published_at = _published_at(metadata) if metadata is not None else None
    if published_at is not None:
        summary["published_age_seconds"] = max(0, (int(time.time()) if now is None else now) - published_at)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    enabled_parser = subparsers.add_parser("enabled")
    enabled_parser.add_argument("--source", required=True)
    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("--base", required=True, type=Path)
    merge_parser.add_argument("--handover", required=True, type=Path)
    merge_parser.add_argument("--metadata", type=Path)
    merge_parser.add_argument("--source", default="pypi")
    stage_parser = subparsers.add_parser("stage")
    stage_parser.add_argument("--snapshot", required=True, type=Path)
    stage_parser.add_argument("--target", required=True, type=Path)
    stage_parser.add_argument("--source", required=True)
    stage_parser.add_argument("--now", type=int, default=None)
    stage_parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.command == "enabled":
        return 0 if enabled(args.source) else 1
    try:
        if args.command == "merge":
            merge(args.base, args.handover, args.source, args.metadata)
        else:
            stage(args.snapshot, args.target, args.source,
                  int(time.time()) if args.now is None else args.now, args.force)
    except Exception as error:  # A disposable cache must never stop publication or CI.
        first_line = str(error).splitlines()[0] if str(error) else type(error).__name__
        print(f"handover {args.source}: skipped ({first_line})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
