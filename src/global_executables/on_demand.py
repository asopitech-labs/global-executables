"""Durable, source-owned publication lifecycle for verified live observations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .discovery import CONTRACT_VERSION
LAYOUT_VERSION = 1
SOURCE = "on-demand"
ROOT = Path("data/production/on-demand")
PROJECTION = Path("data/production/intermediate/on-demand.jsonl")


class ObservationError(ValueError):
    """The live result is not safe to persist as current source evidence."""


class StaleObservationError(ObservationError):
    """A delayed observation attempted to replace newer evidence."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _normalized_package(ecosystem: str, package: str) -> str:
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", package).lower()
    return package.lower()


def claim_id(ecosystem: str, package: str) -> str:
    identity = f"on-demand-v1\0{ecosystem}\0{_normalized_package(ecosystem, package)}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _checked_at(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ObservationError("observation checked_at is not an ISO timestamp") from error


def _claim_paths(root: Path) -> list[Path]:
    return sorted((root / ROOT / "claims").glob("*.json"))


def _event_path(root: Path, digest: str) -> Path:
    # The complete digest remains inside the event. A 128-bit filename keeps
    # atomic writes below the legacy Windows path limit even in pytest roots.
    return root / ROOT / "events" / f"{digest[:32]}.json"


def _claim_path(root: Path, identifier: str) -> Path:
    return root / ROOT / "claims" / f"{identifier[:32]}.json"


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _row(claim: dict[str, Any], command: dict[str, str]) -> dict[str, Any]:
    ecosystem = claim["ecosystem"]
    language = "javascript" if ecosystem == "npm" else "python"
    manager = "npm" if ecosystem == "npm" else "pip"
    observation = claim["observation"]
    evidence = {
        key: observation[key]
        for key in ("metadata_url", "artifact_url", "artifact_digest", "verification_method")
        if observation.get(key) is not None
    }
    evidence.update({
        "event_digest": claim["event_digest"],
        "checked_at": claim["checked_at"],
        "entry_point": command["entry_point"],
        "target": observation.get("target", {}),
    })
    return {
        "command": command["command"],
        "ecosystem": ecosystem,
        "package": claim["package"],
        "version": claim.get("release_version"),
        "repository": None,
        "source": SOURCE,
        "confidence": "direct",
        "source_type": "language_package",
        "package_system": manager,
        "language": language,
        "registry": ecosystem,
        "last_observed_at": claim["checked_at"],
        "claim_id": claim["claim_id"],
        "provenance": [evidence],
    }


def rebuild_projection(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in _claim_paths(root):
        claim = _read(path)
        if claim and claim.get("status") == "active":
            rows.extend(_row(claim, command) for command in claim.get("commands", []))
    rows.sort(key=lambda row: (row["command"].casefold(), row["command"], row["package"]))
    _atomic_jsonl(root / PROJECTION, rows)
    return rows


def persist_observation(root: str | Path, observation: dict[str, Any]) -> dict[str, Any]:
    """Append one verified event and replace that package's current source projection."""
    root = Path(root)
    if observation.get("contract_version") != CONTRACT_VERSION:
        raise ObservationError(f"expected {CONTRACT_VERSION}")
    if observation.get("target"):
        raise ObservationError("durable v1 publication accepts only target-neutral observations")
    if observation.get("requested_command"):
        raise ObservationError("durable publication requires the package's complete command set")
    if observation.get("requested_version") is not None:
        raise ObservationError(
            "durable v1 publication accepts only the registry-selected current release"
        )
    if not observation.get("verification_complete"):
        raise ObservationError("observation did not complete authoritative verification")
    outcome = observation.get("outcome")
    commands = observation.get("commands")
    if outcome not in {"verified_entry_point", "unverified", "not_published"} or not isinstance(commands, list):
        raise ObservationError(f"outcome cannot be persisted: {outcome}")
    if outcome == "verified_entry_point" and not commands:
        raise ObservationError("verified observation has no commands")
    if outcome == "unverified" and commands:
        raise ObservationError("ambiguous unverified observation cannot retire selected commands")
    ecosystem = observation.get("ecosystem")
    package = observation.get("canonical_package") or observation.get("package")
    if ecosystem not in {"npm", "pypi"} or not isinstance(package, str) or not package:
        raise ObservationError("observation has no supported canonical package identity")
    identifier = claim_id(ecosystem, package)
    digest = hashlib.sha256(_canonical(observation)).hexdigest()
    checked = observation.get("checked_at")
    checked_time = _checked_at(checked)
    path = _claim_path(root, identifier)
    previous = _read(path)
    if previous:
        if previous.get("claim_id") != identifier:
            raise ObservationError("truncated claim filename collision")
        previous_time = _checked_at(previous["checked_at"])
        if checked_time < previous_time:
            raise StaleObservationError("older observation cannot replace current evidence")
        if checked_time == previous_time and previous["event_digest"] != digest:
            raise StaleObservationError("conflicting observation has the same acquisition time")
        if previous["event_digest"] == digest:
            return {"changed": False, "claim": previous, "projection_records": len(rebuild_projection(root))}
    status = "active" if commands else "withdrawn"
    claim = {
        "layout_version": LAYOUT_VERSION,
        "claim_id": identifier,
        "ecosystem": ecosystem,
        "package": package,
        "normalized_package": _normalized_package(ecosystem, package),
        "status": status,
        "release_version": observation.get("version"),
        "commands": commands,
        "checked_at": checked,
        "event_digest": digest,
        "previous_event_digest": previous.get("event_digest") if previous else None,
        "publication": {"status": "queued_for_refresh", "snapshot": None},
        "observation": observation,
    }
    event = {"event_type": "observation", "event_digest": digest, "claim": claim}
    event_path = _event_path(root, digest)
    stored_event = _read(event_path)
    if stored_event and stored_event.get("event_digest") != digest:
        raise ObservationError("truncated event filename collision")
    if not stored_event:
        _atomic_json(event_path, event)
    _atomic_json(path, claim)
    rows = rebuild_projection(root)
    return {"changed": True, "claim": claim, "projection_records": len(rows)}


def _providers(dictionary_root: Path) -> tuple[str, list[dict[str, Any]]]:
    metadata = json.loads((dictionary_root / "data/metadata.json").read_text(encoding="utf-8"))
    providers = []
    for path in sorted((dictionary_root / "data/executables").glob("**/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        providers.extend({"command": record["command"], **provider} for provider in record.get("providers", []))
    return metadata["snapshot"], providers


def reconcile_indexed(root: str | Path, dictionary_root: str | Path) -> dict[str, int]:
    """Mark queued claims indexed only after matching the published dictionary."""
    root, dictionary_root = Path(root), Path(dictionary_root)
    snapshot, providers = _providers(dictionary_root)
    counts = {"indexed": 0, "queued": 0, "unchanged": 0}
    for path in _claim_paths(root):
        claim = _read(path)
        if not claim:
            continue
        matching = [provider for provider in providers
                    if provider.get("source") == SOURCE and provider.get("claim_id") == claim["claim_id"]]
        if claim["status"] == "active":
            expected = {item["command"] for item in claim["commands"]}
            current = [item for item in matching
                       if any(entry.get("event_digest") == claim["event_digest"]
                              for entry in item.get("provenance", []))]
            observed = {item["command"] for item in current}
            indexed = expected == observed and len(current) == len(matching) == len(expected)
        else:
            indexed = not matching
        desired = {"status": "indexed", "snapshot": snapshot} if indexed else {
            "status": "queued_for_refresh", "snapshot": None}
        if claim.get("publication") == desired:
            counts["unchanged"] += 1
            continue
        claim["publication"] = desired
        _atomic_json(path, claim)
        counts["indexed" if indexed else "queued"] += 1
    return counts
