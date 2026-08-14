from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .model import dumps, filename, provider_key, shard, valid_command


def merge(records: Iterable[dict[str, Any]], previous: dict[str, dict[str, Any]] | None, seen: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[tuple[str, ...], dict[str, Any]]] = defaultdict(dict)
    for raw in records:
        command = raw["command"]
        if not valid_command(command):
            raise ValueError(f"invalid executable name: {command!r}")
        provider = {k: raw.get(k) for k in ("ecosystem", "package", "version", "repository", "source", "confidence")}
        if raw.get("alias_of") is not None:
            provider["alias_of"] = raw["alias_of"]
        grouped[command][provider_key(provider)] = provider
    output = []
    previous = previous or {}
    for command in sorted(grouped, key=lambda x: (x.casefold(), x)):
        old = previous.get(command, {})
        output.append({"command": command, "providers": sorted(grouped[command].values(), key=provider_key),
                       "first_seen": old.get("first_seen", seen), "last_seen": seen})
    return output


def load_canonical(root: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for path in sorted((root / "data/executables").glob("**/*.json")):
        record = json.loads(path.read_text())
        result[record["command"]] = record
    return result


def publish(root: Path, records: list[dict[str, Any]], coverage: dict[str, Any], snapshot: str) -> None:
    data = root / "data"
    shutil.rmtree(data / "executables", ignore_errors=True)
    shutil.rmtree(data / "indexes", ignore_errors=True)
    prefixes: dict[str, list[str]] = defaultdict(list); lengths: dict[int, list[str]] = defaultdict(list)
    ecosystems: dict[str, list[str]] = defaultdict(list); trigrams: dict[str, list[str]] = defaultdict(list)
    for record in records:
        command = record["command"]
        out = data / "executables" / shard(command) / filename(command)
        out.parent.mkdir(parents=True, exist_ok=True); out.write_text(dumps(record))
        prefixes[shard(command)].append(command); lengths[len(command)].append(command)
        for eco in sorted({p["ecosystem"] for p in record["providers"]}): ecosystems[eco].append(command)
        padded = f"  {command.casefold()}  "
        for tri in set(padded[i:i+3] for i in range(len(padded)-2)): trigrams[tri].append(command)
    for group, values in (("prefix", prefixes), ("length", lengths), ("ecosystem", ecosystems), ("trigram", trigrams)):
        for key, names in sorted(values.items(), key=lambda x: str(x[0])):
            safe = str(key) if group != "trigram" else key.encode().hex()
            path = data / "indexes" / group / f"{safe}.json"
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(dumps(sorted(names, key=lambda x:(x.casefold(),x))))
    successful = [k for k,v in coverage.items() if v.get("status") == "success"]
    # A snapshot identifier determines serialized output. Wall-clock collection
    # timings belong in collector coverage, never in reproducible canonical output.
    metadata = {"snapshot": snapshot, "generated_at": f"{snapshot}T00:00:00+00:00",
                "unique_executables": len(records), "canonical": "data/executables", "indexes_derived": True,
                "checked_sources": sorted(successful), "coverage": coverage}
    data.mkdir(exist_ok=True); (data / "metadata.json").write_text(dumps(metadata))


def rebuild(root: Path, inputs: list[Path], snapshot: str | None = None) -> None:
    snapshot = snapshot or date.today().isoformat()
    previous = load_canonical(root)
    rows = []; coverage = {}
    for path in inputs:
        current = [json.loads(line) for line in path.read_text().splitlines() if line]
        rows.extend(current); eco = path.stem
        coverage[eco] = {"status": "success", "records": len(current), "source": str(path)}
    publish(root, merge(rows, previous, snapshot), coverage, snapshot)
