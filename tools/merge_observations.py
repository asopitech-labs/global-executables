#!/usr/bin/env python3
"""Merge a checkout's observations for one source into the published copy.

Moving package indexes are evidence over time, so rows merge by provider identity:
a newly observed row wins, and a published row this run did not observe is kept
rather than deleted.  A recipe snapshot (vcpkg, xmake) is the exception: the
collector writes ``<source>.reparsed.json`` beside the rows, naming every
``(ecosystem, package)`` whose recipe it just parsed.  For those packages the
observed rows are the whole answer, so published rows the current parser no longer
produces are dropped instead of being carried forward.  Without this the publication
step re-added exactly the rows the collector had replaced (#58).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_reparsed(path: Path | None) -> set[tuple[str, str]]:
    if path is None or not path.is_file():
        return set()
    value = json.loads(path.read_text(encoding="utf-8"))
    return {(ecosystem, package) for ecosystem, package in value.get("packages", [])}


def identity(row: dict[str, Any]) -> tuple[Any, ...]:
    return row.get("command"), row.get("ecosystem"), row.get("package"), row.get("source")


def merge(observed: list[dict[str, Any]], published: list[dict[str, Any]],
          reparsed: set[tuple[str, str]]) -> list[dict[str, Any]]:
    merged = {identity(row): row for row in observed}
    for row in published:
        if (row.get("ecosystem"), row.get("package")) in reparsed:
            continue
        merged.setdefault(identity(row), row)
    return sorted(merged.values(), key=lambda row: (row.get("command", ""), row.get("package", ""),
                                                    row.get("source", "")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--observed", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--reparsed", type=Path, help="defaults to <observed stem>.reparsed.json")
    args = parser.parse_args(argv)
    reparsed_path = args.reparsed or args.observed.with_name(f"{args.observed.stem}.reparsed.json")
    rows = merge(read_rows(args.observed), read_rows(args.target), read_reparsed(reparsed_path))
    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
                           encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
