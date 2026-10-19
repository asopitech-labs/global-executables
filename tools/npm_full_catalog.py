#!/usr/bin/env python3
"""Fetch the npm replication database's package-name catalog with a durable cursor.

The CouchDB ``_all_docs`` endpoint is ordered by id. A checkpoint contains the
last written id so interruption never turns a partial catalog into a complete one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_URL = "https://replicate.npmjs.com/_all_docs"


def fetch_page(start: str | None, limit: int) -> list[str]:
    query = f"?limit={limit}"
    if start is not None:
        query += f"&startkey={quote(json.dumps(start), safe='')}"
    request = Request(BASE_URL + query, headers={
        "User-Agent": "global-executables/1.0 (+https://github.com/asopitech-labs/global-executables)",
    })
    for attempt in range(5):
        try:
            with urlopen(request, timeout=120) as response:
                payload = json.load(response)
            break
        except (OSError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("npm _all_docs returned no rows array")
    names = [row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]
    if len(names) != len(rows):
        raise RuntimeError("npm _all_docs returned a row without an id")
    if start is not None and names and names[0] == start:
        names.pop(0)
    if names != sorted(set(names)):
        raise RuntimeError("npm _all_docs returned unordered or duplicate ids")
    return names


def build_catalog(output: Path, checkpoint: Path, page_size: int = 10000) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest = output.with_suffix(output.suffix + ".manifest.json")
    if manifest.exists():
        details = json.loads(manifest.read_text())
        if not output.exists() or digest_file(output) != details["catalog_digest"]:
            raise RuntimeError("completed npm catalog digest does not match")
        return 0
    progress = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
    start = progress["last_id"] if progress else None
    if progress:
        if not output.exists() or output.stat().st_size < progress["bytes"]:
            raise RuntimeError("npm catalog checkpoint exceeds the catalog")
        # The last page may have reached disk before its checkpoint. Discard it.
        with output.open("rb+") as stream:
            stream.truncate(progress["bytes"])
    elif output.exists():
        raise RuntimeError("npm catalog exists without a checkpoint")
    else:
        output.touch()
        checkpoint.write_text(json.dumps({"last_id": None, "bytes": 0}) + "\n")
    count = 0
    while True:
        names = fetch_page(start, page_size)
        if not names:
            write_manifest(output, manifest)
            checkpoint.unlink(missing_ok=True)
            return count
        with output.open("a") as stream:
            for name in names:
                if not name.startswith("_"):
                    stream.write(name + "\n")
                    count += 1
            stream.flush()
            os.fsync(stream.fileno())
        start = names[-1]
        temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
        temporary.write_text(json.dumps({"last_id": start, "bytes": output.stat().st_size}) + "\n")
        temporary.replace(checkpoint)
        if len(names) < page_size - 1:
            write_manifest(output, manifest)
            checkpoint.unlink(missing_ok=True)
            return count


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_manifest(output: Path, manifest: Path) -> None:
    with output.open() as stream:
        count = sum(1 for _ in stream)
    temporary = manifest.with_suffix(manifest.suffix + ".tmp")
    temporary.write_text(json.dumps({
        "catalog_source": BASE_URL,
        "catalog_snapshot": date.today().isoformat(),
        "catalog_size": count,
        "catalog_digest": digest_file(output),
        "catalog_complete": True,
        "catalog_scope": "npm registry package-name index",
    }, sort_keys=True, indent=2) + "\n")
    temporary.replace(manifest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/production/npm-packages.txt"))
    parser.add_argument("--checkpoint", type=Path, default=Path("data/production/npm-packages.cursor"))
    args = parser.parse_args()
    print(f"new_packages={build_catalog(args.output, args.checkpoint)} output={args.output}")


if __name__ == "__main__":
    main()
