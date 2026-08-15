"""Incremental, fail-closed freshness scans over declared source partitions.

Freshness scans deliberately operate outside the canonical dataset.  They keep
their cursor and observations in ``data/freshness`` and publish a report under
``reports``.  A partial scan can therefore detect changes without pretending
that an unvisited partition was exhaustively checked.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STATE_VERSION = 1
MANIFEST_VERSION = 1


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    value = value or _now()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _staleness(last_success_at: str | None, observed_at: datetime) -> int | None:
    previous = _parse_timestamp(last_success_at)
    if previous is None:
        return None
    return max(0, int((observed_at - previous).total_seconds()))


def _file_checksum(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _record_key(record: dict[str, Any]) -> str:
    """Return a stable identity for one normalized executable observation."""
    identity = {
        key: record.get(key)
        for key in ("command", "ecosystem", "package", "source", "scope")
    }
    return _stable_json(identity)


def _record_fingerprint(record: dict[str, Any]) -> str:
    return hashlib.sha256(_stable_json(record).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def load_manifest(path: Path, root: Path) -> list[dict[str, Any]]:
    """Load and validate a deterministic partition manifest."""
    value = json.loads(path.read_text())
    if value.get("version") != MANIFEST_VERSION or not isinstance(value.get("partitions"), list):
        raise ValueError("freshness manifest must contain version=1 and partitions")
    partitions = []
    seen: set[str] = set()
    for raw in value["partitions"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ValueError("each freshness partition requires a string id")
        partition = dict(raw)
        partition_id = partition["id"]
        if partition_id in seen:
            raise ValueError(f"duplicate freshness partition: {partition_id}")
        seen.add(partition_id)
        if not isinstance(partition.get("input"), str) or not partition["input"]:
            raise ValueError(f"partition {partition_id} requires an input path")
        partition["path"] = (root / partition["input"]).resolve()
        partition["source"] = str(partition.get("source", partition_id))
        partition["enabled"] = bool(partition.get("enabled", True))
        if partition.get("scope") is not None and not isinstance(partition["scope"], dict):
            raise ValueError(f"partition {partition_id} scope must be an object")
        partitions.append(partition)
    return sorted((p for p in partitions if p["enabled"]), key=lambda p: p["id"])


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": STATE_VERSION, "next_partition": 0, "partitions": {}}
    value = json.loads(path.read_text())
    if value.get("version") != STATE_VERSION or not isinstance(value.get("partitions"), dict):
        raise ValueError("invalid freshness state version or partitions")
    value["next_partition"] = int(value.get("next_partition", 0))
    return value


def _empty_partition_state() -> dict[str, Any]:
    return {
        "cursor": 0,
        "source_checksum": None,
        "observations": {},
        "cycle_observations": {},
        "last_checked_at": None,
        "last_success_at": None,
        "last_status": "never_checked",
        "last_error": None,
        "cycles_completed": 0,
    }


def _scan_partition(
    partition: dict[str, Any],
    previous: dict[str, Any],
    observed_at: datetime,
    record_budget: int,
    byte_budget: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Scan one partition and return (candidate state, report)."""
    path = partition["path"]
    report: dict[str, Any] = {
        "id": partition["id"],
        "source": partition["source"],
        "scope": partition.get("scope", {}),
        "status": "failed",
        "checked_at": _timestamp(observed_at),
        "source_snapshot": partition.get("source_snapshot"),
        "scanned_records": 0,
        "scanned_bytes": 0,
        "new_records": 0,
        "changed_records": 0,
        "removed_records": 0,
        "cycle_completed": False,
        "rate_limit": partition.get("rate_limit"),
    }
    if not path.is_file():
        report["unavailable"] = True
        report["error"] = f"input is unavailable: {partition['input']}"
        candidate = copy.deepcopy(previous)
        candidate.update({"last_checked_at": report["checked_at"], "last_status": "failed",
                          "last_error": report["error"]})
        return candidate, report

    try:
        checksum, source_bytes = _file_checksum(path)
    except OSError as error:
        candidate = copy.deepcopy(previous)
        candidate.update({"last_checked_at": report["checked_at"], "last_status": "failed",
                          "last_error": str(error)})
        report["unavailable"] = True
        report["error"] = str(error)
        return candidate, report
    report["source_checksum"] = checksum
    report["source_bytes"] = source_bytes
    candidate = copy.deepcopy(previous)
    candidate.setdefault("observations", {})
    candidate.setdefault("cycle_observations", {})
    candidate.setdefault("cursor", 0)
    if candidate.get("source_checksum") not in (None, checksum):
        # Start a new cycle when the source snapshot changes, while retaining
        # the previous completed cycle for change/removal comparison.
        candidate["cursor"] = 0
        candidate["cycle_observations"] = {}
    candidate["source_checksum"] = checksum
    cursor_before = int(candidate.get("cursor", 0))
    report["cursor_before"] = cursor_before
    line_count = 0
    reached_eof = True
    scan_budget = max(0, int(record_budget))
    remaining_bytes = None if byte_budget is None else max(0, int(byte_budget))
    try:
        with path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle):
                line_count = line_number + 1
                if line_number < cursor_before:
                    continue
                if report["scanned_records"] >= scan_budget:
                    reached_eof = False
                    break
                if remaining_bytes is not None and report["scanned_bytes"] + len(raw_line) > remaining_bytes:
                    reached_eof = False
                    break
                if not raw_line.strip():
                    candidate["cursor"] = line_number + 1
                    continue
                record = json.loads(raw_line)
                if not isinstance(record, dict) or not isinstance(record.get("command"), str):
                    raise ValueError(f"invalid normalized record at line {line_number + 1}")
                key = _record_key(record)
                fingerprint = _record_fingerprint(record)
                old_fingerprint = candidate["observations"].get(key)
                if old_fingerprint is None:
                    report["new_records"] += 1
                elif old_fingerprint != fingerprint:
                    report["changed_records"] += 1
                candidate["cycle_observations"][key] = fingerprint
                candidate["cursor"] = line_number + 1
                report["scanned_records"] += 1
                report["scanned_bytes"] += len(raw_line)
                if remaining_bytes is not None:
                    remaining_bytes -= len(raw_line)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        candidate = copy.deepcopy(previous)
        candidate.update({"last_checked_at": report["checked_at"], "last_status": "failed",
                          "last_error": str(error)})
        report["unavailable"] = False
        report["error"] = str(error)
        report["cursor_after"] = previous.get("cursor", 0)
        return candidate, report

    # A cursor at or beyond EOF completes the current source cycle.  Empty
    # files are valid and complete immediately.
    if reached_eof and int(candidate.get("cursor", 0)) >= line_count:
        old_observations = candidate.get("observations", {})
        current_observations = candidate.get("cycle_observations", {})
        report["removed_records"] = len(set(old_observations) - set(current_observations))
        candidate["observations"] = current_observations
        candidate["cycle_observations"] = {}
        candidate["cursor"] = 0
        candidate["cycles_completed"] = int(candidate.get("cycles_completed", 0)) + 1
        report["cycle_completed"] = True
    candidate.update({"last_checked_at": report["checked_at"], "last_success_at": report["checked_at"],
                      "last_status": "success", "last_error": None})
    report["cursor_after"] = candidate["cursor"]
    report["status"] = "success"
    report["last_success_at"] = candidate["last_success_at"]
    report["staleness_seconds"] = 0
    return candidate, report


def run_scan(
    root: Path,
    manifest_path: Path,
    state_path: Path,
    report_path: Path,
    partition_budget: int = 1,
    record_budget: int = 1000,
    byte_budget: int | None = None,
    observed_at: datetime | None = None,
    retries: int = 0,
    backoff_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run one bounded partial scan and persist state/report atomically."""
    if partition_budget < 1 or record_budget < 1:
        raise ValueError("partition_budget and record_budget must be positive")
    if retries < 0 or backoff_seconds < 0:
        raise ValueError("retries and backoff_seconds must be non-negative")
    observed_at = observed_at or _now()
    started = time.monotonic()
    manifest = load_manifest(manifest_path, root)
    if not manifest:
        raise ValueError("freshness manifest has no enabled partitions")
    state = load_state(state_path)
    states = state.setdefault("partitions", {})
    start = int(state.get("next_partition", 0)) % len(manifest)
    selected = [manifest[(start + offset) % len(manifest)] for offset in range(min(partition_budget, len(manifest)))]
    selected_ids = {p["id"] for p in selected}
    skipped = [p for p in manifest if p["id"] not in selected_ids]
    remaining_records = record_budget
    remaining_bytes = byte_budget
    results = []
    for partition in selected:
        if remaining_records <= 0 or (remaining_bytes is not None and remaining_bytes <= 0):
            previous = copy.deepcopy(states.get(partition["id"], _empty_partition_state()))
            result = {"id": partition["id"], "source": partition["source"], "scope": partition.get("scope", {}),
                      "status": "skipped", "reason": "run_budget_exhausted", "checked_at": _timestamp(observed_at),
                      "last_success_at": previous.get("last_success_at"),
                      "staleness_seconds": _staleness(previous.get("last_success_at"), observed_at)}
            results.append(result)
            continue
        previous = copy.deepcopy(states.get(partition["id"], _empty_partition_state()))
        attempts = 0
        for attempt in range(retries + 1):
            attempts = attempt + 1
            candidate, result = _scan_partition(partition, previous, observed_at, remaining_records, remaining_bytes)
            if result["status"] == "success" or attempt == retries:
                break
            if backoff_seconds:
                time.sleep(backoff_seconds * (2 ** attempt))
        result["attempts"] = attempts
        result["retries"] = attempts - 1
        if result["status"] == "success":
            states[partition["id"]] = candidate
            remaining_records -= result["scanned_records"]
            if remaining_bytes is not None:
                remaining_bytes -= result["scanned_bytes"]
        else:
            failure_state = states.setdefault(partition["id"], previous)
            failure_state.update({"last_checked_at": result["checked_at"], "last_status": "failed",
                                  "last_error": result.get("error")})
            result["last_success_at"] = failure_state.get("last_success_at")
            result["staleness_seconds"] = _staleness(failure_state.get("last_success_at"), observed_at)
        results.append(result)

    next_partition = (start + len(selected)) % len(manifest)
    state["version"] = STATE_VERSION
    state["next_partition"] = next_partition
    state["updated_at"] = _timestamp(observed_at)
    failed = [result for result in results if result["status"] == "failed"]
    succeeded = [result for result in results if result["status"] == "success"]
    report = {
        "version": 1,
        "run_id": _timestamp(observed_at),
        "checked_at": _timestamp(observed_at),
        "duration_seconds": round(time.monotonic() - started, 6),
        "coverage_kind": "partial",
        "status": "success" if not failed else "failed",
        "scheduler": {"next_partition_before": start, "next_partition_after": next_partition,
                       "selected": [p["id"] for p in selected], "skipped": [p["id"] for p in skipped]},
        "budget": {"partitions": partition_budget, "records": record_budget, "bytes": byte_budget},
        "attempted_partitions": [p["id"] for p in selected if p["id"] not in {r.get("id") for r in results if r.get("status") == "skipped"}],
        "succeeded_partitions": [r["id"] for r in succeeded],
        "failed_partitions": [r["id"] for r in failed],
        "skipped_partitions": [r["id"] for r in skipped] + [r["id"] for r in results if r.get("status") == "skipped"],
        "partition_staleness": {
            p["id"]: _staleness(states.get(p["id"], {}).get("last_success_at"), observed_at)
            for p in manifest
        },
        "partitions": results,
        "summary": {
            "scanned_records": sum(r.get("scanned_records", 0) for r in results),
            "scanned_bytes": sum(r.get("scanned_bytes", 0) for r in results),
            "new_records": sum(r.get("new_records", 0) for r in results),
            "changed_records": sum(r.get("changed_records", 0) for r in results),
            "removed_records": sum(r.get("removed_records", 0) for r in results),
            "failed_partitions": len(failed),
            "unavailable_partitions": sum(bool(r.get("unavailable")) for r in results),
            "skipped_partitions": len(skipped) + sum(r.get("status") == "skipped" for r in results),
        },
        "quality_gate": {"canonical_publish": False, "absence_claim": "unknown",
                         "failure_blocks_exhaustive": True},
    }
    _write_json(report_path, report)
    _write_json(state_path, state)
    return report
