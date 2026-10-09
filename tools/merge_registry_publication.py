#!/usr/bin/env python3
"""Merge one registry writer into the shared artifact-data snapshot.

Each publisher owns only its named source.  Updating the whole state or report from
one writer can roll another writer back, so this command performs a source-scoped,
monotonic merge immediately before the Git commit is created.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from global_executables.registry_state import load_state, save_state  # noqa: E402


def read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return fallback
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cursor(entry: Any) -> int | None:
    value = entry.get("cursor") if isinstance(entry, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def catalog(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    for field in ("modules_file", "packages_file", "projects_file", "names_file"):
        value = entry.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def catalog_size(entry: Any) -> int | None:
    value = entry.get("catalog_size") if isinstance(entry, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def catalog_identity(entry: Any) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    for field in ("catalog_digest", "catalog_snapshot"):
        value = entry.get(field)
        if isinstance(value, str) and value:
            return field, value
    value = catalog(entry)
    return ("catalog", value) if value else None


def catalog_changed_forward(published: Any, local: Any) -> bool:
    before = published.get("catalog_snapshot") if isinstance(published, dict) else None
    after = local.get("catalog_snapshot") if isinstance(local, dict) else None
    if isinstance(before, str) and before:
        if not isinstance(after, str) or after <= before:
            return False
        previous_digest = published.get("catalog_digest")
        next_digest = local.get("catalog_digest") if isinstance(local, dict) else None
        valid_next_digest = (
            isinstance(next_digest, str)
            and len(next_digest) == 71
            and next_digest.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in next_digest[7:])
        )
        return valid_next_digest and next_digest != previous_digest
    previous_catalog, next_catalog = catalog_identity(published), catalog_identity(local)
    return next_catalog is not None and (
        (previous_catalog is not None and previous_catalog != next_catalog)
        or (
            previous_catalog is None
            and catalog_size(published) is not None
            and catalog_size(published) != catalog_size(local)
        )
    )


def would_regress(published: Any, local: Any) -> bool:
    before, after = cursor(published), cursor(local)
    return before is not None and (after is None or after < before) and not catalog_changed_forward(published, local)


def merge_state(source: str, published_path: Path, local_path: Path) -> tuple[bool, int | None, int | None]:
    # State paths may name the sharded directory or the legacy file; saving writes the
    # directory and removes the legacy file, which migrates the published branch.
    published = load_state(published_path, {"version": 1, "sources": {}})
    local = load_state(local_path, {"version": 1, "sources": {}})
    published_sources = published.setdefault("sources", {})
    local_entry = local.get("sources", {}).get(source)
    if not isinstance(published_sources, dict) or not isinstance(local_entry, dict):
        raise ValueError(f"local state has no object for source {source!r}")
    previous = published_sources.get(source)
    before, after = cursor(previous), cursor(local_entry)
    if would_regress(previous, local_entry):
        return False, before, after
    published_sources[source] = local_entry
    save_state(published_path, published)
    return True, before, after


def aggregate_report(report: dict[str, Any]) -> None:
    sources = report.get("sources", {})
    entries = list(sources.values()) if isinstance(sources, dict) else []
    statuses = {entry.get("status") for entry in entries if isinstance(entry, dict)}
    if statuses & {"failure", "failed", "error"}:
        report["status"] = "failure"
    elif entries and statuses <= {None, "success"}:
        report["status"] = "success"
    else:
        report["status"] = "partial"
    report["coverage_kind"] = (
        "exhaustive"
        if entries
        and all(
            isinstance(entry, dict) and entry.get("coverage_kind") == "exhaustive"
            for entry in entries
        )
        else "partial"
    )
    report["aggregate"] = True
    for key in ("byte_budget", "package_budget", "state", "started_at", "interrupted", "snapshot_generation"):
        report.pop(key, None)


# Per-run effort and timing: how many requests a pass spent says nothing about what the
# published data is, and committing it would make every run a commit (history/cache
# split, docs/OPERATIONS.md). A report entry that differs only in these keys is not
# published; one that differs in anything else (status, cursors, failures, coverage,
# catalogue identity, generation, errors) is.
EFFORT_KEYS = frozenset({
    "started_at", "finished_at", "interrupted", "duration_seconds", "packages_per_minute", "modules_per_minute",
    "processed", "refreshed", "records", "downloaded_bytes", "requests", "rate_limited", "timeouts", "workers",
    "host_concurrency", "package_budget", "byte_budget", "unchanged", "skipped_not_due", "feed_works",
    "feed_events", "feed_enqueued", "feed_requests", "feed_bytes", "feed_resync", "feed_error", "feed",
    "feed_queue", "checked", "ttl", "catalog_discovered", "catalog_requests", "budget_exhausted", "refresh_cursor",
    "state",
})


def outcome(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if key not in EFFORT_KEYS}


def merge_report(source: str, published_path: Path, local_path: Path, heartbeat_hours: float = 0.0) -> bool:
    if not local_path.is_file():
        return False
    published = read_json(published_path, {"sources": {}})
    local = read_json(local_path, {"sources": {}})
    published_sources = published.setdefault("sources", {})
    local_entry = local.get("sources", {}).get(source)
    if not isinstance(published_sources, dict) or not isinstance(local_entry, dict):
        return False
    previous = published_sources.get(source)
    if would_regress(previous, local_entry):
        return False
    if previous is not None and outcome(previous) == outcome(local_entry) and not heartbeat_due(published, heartbeat_hours):
        return False  # nothing but effort changed: no commit for a run that found nothing
    published_sources[source] = local_entry
    finished = [
        value
        for value in (published.get("finished_at"), local.get("finished_at"))
        if isinstance(value, str) and value
    ]
    if finished:
        published["finished_at"] = max(finished)
    aggregate_report(published)
    write_json_atomic(published_path, published)
    return True


def heartbeat_due(published: dict[str, Any], hours: float) -> bool:
    """With a heartbeat the report is refreshed at least that often even when idle (off by default)."""
    if hours <= 0:
        return False
    finished = published.get("finished_at")
    try:
        last = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(hours=hours)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--published-state", required=True, type=Path)
    parser.add_argument("--local-state", required=True, type=Path)
    parser.add_argument("--published-report", type=Path)
    parser.add_argument("--local-report", type=Path)
    parser.add_argument("--report-heartbeat-hours", type=float, default=float(os.environ.get("REPORT_HEARTBEAT_HOURS", "0")),
                        help="republish an unchanged report at least this often (0: never)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (args.published_report is None) != (args.local_report is None):
        raise SystemExit("--published-report and --local-report must be supplied together")
    merged, before, after = merge_state(args.source, args.published_state, args.local_state)
    if not merged:
        print(f"refusing {args.source}: local cursor {after} is behind published cursor {before}")
        # A distinct status lets callers skip this source's catalogue and JSONL too.
        # Returning success here would protect state while still copying stale artifacts.
        return 3
    report_merged = False
    if args.published_report is not None:
        report_merged = merge_report(args.source, args.published_report, args.local_report,
                                     args.report_heartbeat_hours)
    movement = f"{before} -> {after}" if before != after else "cursor unchanged"
    print(f"{args.source} {movement}; report {'merged' if report_merged else 'unchanged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
