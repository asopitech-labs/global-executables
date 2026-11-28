#!/usr/bin/env python3
"""Inspect a bounded slice of npm's full catalog independently of the critical sweep."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from global_executables.collectors import npm_metadata  # noqa: E402
from global_executables.booster import owner_bucket  # noqa: E402
from global_executables.registry_state import load_state, save_state  # noqa: E402

SOURCE = "npm-registry"
CATALOG_SOURCE = "https://replicate.npmjs.com/_all_docs"


def inspect(name: str) -> list[dict]:
    url = "https://registry.npmjs.org/" + quote(name, safe="@") + "/latest"
    user_agent = os.environ.get(
        "GE_USER_AGENT",
        "global-executables/1.0 (+https://github.com/asopitech-labs/global-executables)",
    )
    request = Request(url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=45) as response:
        document = json.load(response)
    if not isinstance(document, dict) or document.get("name") != name:
        raise ValueError(f"npm latest metadata has the wrong package name: {name}")
    return npm_metadata(document, source=url)


def write_rows(path: Path, updates: dict[str, list[dict]]) -> int:
    """Merge one bounded batch into sorted observations with constant old-row memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    pending = iter(sorted(updates.items()))
    next_update = next(pending, None)
    count = 0

    def emit(stream, package_rows):
        nonlocal count
        for row in package_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1

    with temporary.open("w") as stream:
        if path.exists():
            replaced_package = None
            with path.open() as previous:
                for line in previous:
                    row = json.loads(line)
                    package = row["package"]
                    if package == replaced_package:
                        continue
                    while next_update is not None and next_update[0] < package:
                        emit(stream, next_update[1])
                        next_update = next(pending, None)
                    if next_update is not None and next_update[0] == package:
                        emit(stream, next_update[1])
                        replaced_package = package
                        next_update = next(pending, None)
                        continue
                    stream.write(line)
                    count += 1
        while next_update is not None:
            emit(stream, next_update[1])
            next_update = next(pending, None)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    return count


def crawl(catalog: Path, manifest: Path, state_path: Path, observations: Path,
          report_path: Path, budget: int, interval: float = .2,
          bucket: int | None = None, position: int = 0) -> dict:
    if budget < 1:
        raise ValueError("budget must be positive")
    if bucket is not None and not 0 <= bucket < 256:
        raise ValueError("bucket must be within 0-255")
    if position < 0:
        raise ValueError("position must be non-negative")
    details = json.loads(manifest.read_text())
    if not details.get("catalog_complete") or details.get("catalog_source") != CATALOG_SOURCE:
        raise ValueError("npm registry catalog has no complete npm _all_docs manifest")
    digest = hashlib.sha256()
    catalog_count = 0
    state = load_state(state_path)
    current = state.setdefault("sources", {}).setdefault(SOURCE, {})
    old_size = current.get("catalog_size")
    old_digest = current.get("catalog_digest")
    prefix_digest = None
    bucket_size = 0
    with catalog.open("rb") as stream:
        for line in stream:
            digest.update(line)
            catalog_count += 1
            if bucket is not None:
                name = line.rstrip(b"\r\n").decode("utf-8")
                if owner_bucket(name) == bucket:
                    bucket_size += 1
            if catalog_count == old_size:
                prefix_digest = "sha256:" + digest.hexdigest()
    if details.get("catalog_digest") != "sha256:" + digest.hexdigest() or details.get("catalog_size") != catalog_count:
        raise ValueError("npm registry catalog does not match its manifest")
    digest = details["catalog_digest"]
    if current.get("catalog_digest") not in (None, digest):
        if not (type(old_size) is int and old_size > 0 and catalog_count >= old_size
                and prefix_digest == old_digest):
            raise ValueError("npm registry catalog changed without preserving the old prefix")
    current.update({key: details[key] for key in (
        "catalog_complete", "catalog_digest", "catalog_size", "catalog_snapshot",
        "catalog_source", "catalog_scope",
    )})
    current["packages_file"] = "data/production/npm-packages.txt"
    cursor = int(current.get("cursor", 0)) if bucket is None else position
    if bucket is not None and cursor > bucket_size:
        raise ValueError("bucket position exceeds its catalog size")
    rows: dict[str, list[dict]] = {}
    failures = current.setdefault("failures", {})
    unavailable = current.setdefault("unavailable", {})
    attempted = 0
    owned_failures = [name for name in failures
                      if bucket is None or owner_bucket(name) == bucket]
    retry_budget = min(len(owned_failures), max(1, budget // 4))
    for name in owned_failures:
        if attempted >= retry_budget:
            break
        try:
            rows[name] = inspect(name)
            failures.pop(name, None)
            unavailable.pop(name, None)
        except HTTPError as error:
            if error.code in (404, 410, 451):
                failures.pop(name, None)
                unavailable[name] = f"HTTP {error.code}"
                rows[name] = []
            else:
                failures[name] = f"HTTP {error.code}"
        except (URLError, TimeoutError, ValueError) as error:
            failures[name] = str(error)
        attempted += 1
        if interval:
            time.sleep(interval)
    owned_index = 0
    with catalog.open(encoding="utf-8") as stream:
        for index, raw in enumerate(stream):
            name = raw.rstrip("\n")
            if not name:
                raise ValueError(f"empty npm package name at catalog index {index}")
            if bucket is not None and owner_bucket(name) != bucket:
                continue
            current_position = owned_index if bucket is not None else index
            if bucket is not None:
                owned_index += 1
            if current_position < cursor:
                continue
            if attempted >= budget:
                break
            try:
                rows[name] = inspect(name)
                failures.pop(name, None)
            except HTTPError as error:
                if error.code in (404, 410, 451):
                    unavailable[name] = f"HTTP {error.code}"
                    rows[name] = []
                else:
                    failures[name] = f"HTTP {error.code}"
            except (URLError, TimeoutError, ValueError) as error:
                failures[name] = str(error)
            cursor = current_position + 1
            attempted += 1
            if interval:
                time.sleep(interval)
    records = write_rows(observations, rows)
    if bucket is None:
        current["cursor"] = cursor
    save_state(state_path, state)
    remaining_failures = sum(
        1 for name in failures if bucket is None or owner_bucket(name) == bucket)
    task = None if bucket is None else {
        "unit": f"{bucket:02x}", "position": cursor, "size": bucket_size,
        "failures": remaining_failures,
        "complete": cursor == bucket_size and remaining_failures == 0,
    }
    result = {"status": "success", "coverage_kind": "partial", "sources": {SOURCE: {
        "status": "success", "coverage_kind": "partial", "catalog_scope": current["catalog_scope"],
        "catalog_size": current["catalog_size"], "cursor": cursor,
        "catalog_complete": True, "failures": len(failures),
        "unavailable": len(unavailable), "records": records,
        **({"task": task} if task is not None else {}),
    }}}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, default=Path("data/production/npm-packages.txt"))
    parser.add_argument("--state", type=Path, default=Path("data/production/registry-state"))
    parser.add_argument("--output", type=Path, default=Path("data/production/intermediate/npm-registry.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("reports/npm-registry-crawl.json"))
    parser.add_argument("--budget", type=int, default=1000)
    parser.add_argument("--bucket", type=lambda value: int(value, 0))
    parser.add_argument("--position", type=int, default=0)
    args = parser.parse_args()
    crawl(args.catalog, args.catalog.with_suffix(args.catalog.suffix + ".manifest.json"),
          args.state, args.output, args.report, args.budget,
          bucket=args.bucket, position=args.position)


if __name__ == "__main__":
    main()
