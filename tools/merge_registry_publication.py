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
from pathlib import Path
from typing import Any


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


def would_regress(published: Any, local: Any) -> bool:
    before, after = cursor(published), cursor(local)
    return before is not None and (after is None or after < before)


def merge_state(source: str, published_path: Path, local_path: Path) -> tuple[bool, int | None, int | None]:
    published = read_json(published_path, {"version": 1, "sources": {}})
    local = read_json(local_path, {"version": 1, "sources": {}})
    published_sources = published.setdefault("sources", {})
    local_entry = local.get("sources", {}).get(source)
    if not isinstance(published_sources, dict) or not isinstance(local_entry, dict):
        raise ValueError(f"local state has no object for source {source!r}")
    previous = published_sources.get(source)
    before, after = cursor(previous), cursor(local_entry)
    if would_regress(previous, local_entry):
        return False, before, after
    published_sources[source] = local_entry
    write_json_atomic(published_path, published)
    return True, before, after


def aggregate_report(report: dict[str, Any]) -> None:
    sources = report.get("sources", {})
    entries = list(sources.values()) if isinstance(sources, dict) else []
    statuses = {entry.get("status") for entry in entries if isinstance(entry, dict)}
    if entries and statuses == {"success"}:
        report["status"] = "success"
    elif statuses & {"failure", "failed", "error"}:
        report["status"] = "failure"
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


def merge_report(source: str, published_path: Path, local_path: Path) -> bool:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--published-state", required=True, type=Path)
    parser.add_argument("--local-state", required=True, type=Path)
    parser.add_argument("--published-report", type=Path)
    parser.add_argument("--local-report", type=Path)
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
        report_merged = merge_report(args.source, args.published_report, args.local_report)
    movement = f"{before} -> {after}" if before != after else "cursor unchanged"
    print(f"{args.source} {movement}; report {'merged' if report_merged else 'unchanged'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
