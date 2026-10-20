from __future__ import annotations

import json
import hashlib
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .model import dumps, filename, provider_key, shard, valid_command


@dataclass(frozen=True)
class RebuildPolicy:
    """Explicit exceptions to the default safe publication policy."""

    shrink_reason: str | None = None


@dataclass(frozen=True)
class MergeResult:
    records: list[dict[str, Any]]
    rejected: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class RebuildResult:
    input_records: int
    unique_executables: int
    previous_unique_executables: int
    rejected_records: int
    rejected: tuple[dict[str, Any], ...]
    shrink_reason: str | None


class DatasetShrinkError(ValueError):
    def __init__(self, previous: int, current: int):
        self.previous = previous
        self.current = current
        super().__init__(
            f"refusing to shrink the dictionary from {previous} to {current} unique executables; "
            "pass an explicit shrink reason when the drop is intentional"
        )


def merge(records: Iterable[dict[str, Any]], previous: dict[str, dict[str, Any]] | None, seen: str,
          history: dict[str, str] | None = None) -> MergeResult:
    grouped: dict[str, dict[tuple[str, ...], dict[str, Any]]] = defaultdict(dict)
    rejected: list[dict[str, Any]] = []
    for raw in records:
        command = raw["command"]
        if not isinstance(command, str) or not valid_command(command):
            rejected.append({
                "command": command,
                "ecosystem": raw.get("ecosystem"),
                "package": raw.get("package"),
                "reason": "invalid executable name",
            })
            continue
        provider = {k: raw.get(k) for k in ("ecosystem", "package", "version", "repository", "source", "confidence")}
        for key in (
            "alias_of", "source_type", "package_system", "distribution_family", "distribution",
            "distribution_release", "language", "registry", "latest_release_at", "latest_version",
            "last_observed_at", "release_history", "usage_metrics",
        ):
            if raw.get(key) is not None:
                provider[key] = raw[key]
        grouped[command][provider_key(provider)] = provider
    output = []
    previous = previous or {}
    history = history or {}
    for command in sorted(grouped, key=lambda x: (x.casefold(), x)):
        old = previous.get(command, {})
        output.append({"command": command, "providers": sorted(grouped[command].values(), key=provider_key),
                       "first_seen": old.get("first_seen", history.get(command, seen)), "last_seen": seen})
    return MergeResult(output, tuple(rejected))


def load_canonical(root: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for path in sorted((root / "data/executables").glob("**/*.json")):
        record = json.loads(path.read_text())
        result[record["command"]] = record
    return result


def load_history(root: Path) -> dict[str, str]:
    path = root / "data/history.json"
    if not path.is_file():
        return {}
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def publish(root: Path, records: list[dict[str, Any]], coverage: dict[str, Any], snapshot: str,
            history: dict[str, str] | None = None) -> None:
    data = root / "data"
    shutil.rmtree(data / "executables", ignore_errors=True)
    shutil.rmtree(data / "indexes", ignore_errors=True)
    prefixes: dict[str, list[str]] = defaultdict(list); lengths: dict[int, list[str]] = defaultdict(list)
    ecosystems: dict[str, list[str]] = defaultdict(list); trigrams: dict[str, list[str]] = defaultdict(list)
    scopes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in records:
        command = record["command"]
        out = data / "executables" / shard(command) / filename(command)
        out.parent.mkdir(parents=True, exist_ok=True); out.write_text(dumps(record))
        prefixes[shard(command)].append(command); lengths[len(command)].append(command)
        for eco in sorted({p["ecosystem"] for p in record["providers"]}): ecosystems[eco].append(command)
        for provider in record["providers"]:
            for dimension in ("source_type", "package_system", "distribution_family", "distribution",
                              "distribution_release", "language", "registry", "ecosystem"):
                if provider.get(dimension) is not None:
                    scopes[(dimension, str(provider[dimension]))].append(command)
        padded = f"  {command.casefold()}  "
        for tri in set(padded[i:i+3] for i in range(len(padded)-2)): trigrams[tri].append(command)
    for group, values in (("prefix", prefixes), ("length", lengths), ("ecosystem", ecosystems), ("trigram", trigrams)):
        for key, names in sorted(values.items(), key=lambda x: str(x[0])):
            safe = str(key) if group != "trigram" else key.encode().hex()
            path = data / "indexes" / group / f"{safe}.json"
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(dumps(sorted(names, key=lambda x:(x.casefold(),x))))
    for (dimension, value), names in sorted(scopes.items()):
        path = data / "indexes" / "scope" / dimension / f"{value}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(sorted(set(names), key=lambda x:(x.casefold(),x))))
    manifest = {}
    for path in sorted((data / "indexes").glob("**/*.json")):
        relative = path.relative_to(data).as_posix()
        manifest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    successful = [k for k,v in coverage.items() if v.get("status") == "success"]
    # A snapshot identifier determines serialized output. Wall-clock collection
    # timings belong in collector coverage, never in reproducible canonical output.
    metadata = {"snapshot": snapshot, "generated_at": f"{snapshot}T00:00:00+00:00",
                "unique_executables": len(records), "canonical": "data/executables", "indexes_derived": True,
                "checked_sources": sorted(successful), "negative_lookup": "exhaustive" if coverage and all(v.get("coverage_kind") == "exhaustive" and v.get("status") == "success" for v in coverage.values()) else "unknown",
                "coverage": coverage, "index_manifest": manifest}
    data.mkdir(exist_ok=True); (data / "metadata.json").write_text(dumps(metadata))
    durable_history = dict(history or {})
    durable_history.update({record["command"]: record["first_seen"] for record in records})
    (data / "history.json").write_text(dumps(dict(sorted(durable_history.items()))))


def rebuild(root: Path, inputs: list[Path], snapshot: str | None = None,
            coverage_kind: str | dict[str, str] = "fixture", *,
            policy: RebuildPolicy = RebuildPolicy()) -> RebuildResult:
    snapshot = snapshot or date.today().isoformat()
    previous = load_canonical(root)
    history = load_history(root)
    rows = []; coverage = {}
    for path in inputs:
        current = [json.loads(line) for line in path.read_text().splitlines() if line]
        rows.extend(current); eco = path.stem
        kind = coverage_kind.get(eco, "partial") if isinstance(coverage_kind, dict) else coverage_kind
        coverage[eco] = {"status": "success", "coverage_kind": kind, "records": len(current), "source": str(path)}
    merged = merge(rows, previous, snapshot, history)
    if len(merged.records) < len(previous) and not policy.shrink_reason:
        raise DatasetShrinkError(len(previous), len(merged.records))
    publish(root, merged.records, coverage, snapshot, history)
    return RebuildResult(
        input_records=len(rows),
        unique_executables=len(merged.records),
        previous_unique_executables=len(previous),
        rejected_records=len(merged.rejected),
        rejected=merged.rejected,
        shrink_reason=policy.shrink_reason,
    )
