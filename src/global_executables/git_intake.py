"""Git-only, immutable reservation writer for a designated MCP/CLI runner.

The runner may push ``intake/*`` but never writes artifact-data or dictionary.
The scheduled collector independently verifies every proposal before ingestion.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .discovery import CONTRACT_VERSION

PROPOSAL_VERSION = 1
INTAKE_PREFIX = "intake/"
PROPOSAL_DIR = "data/production/on-demand/intake"
LEDGER_DIR = "data/production/on-demand/intake-ledger"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[0-9a-f]{64}$")
_COMMAND = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
_NPM = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]{0,213}/)?[a-z0-9][a-z0-9._~-]{0,213}$")
_PYPI = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,198}[a-z0-9])?$")
_IDENTITY = ("ecosystem", "package", "version", "selector", "target", "verifier", "artifact_digest")


class IntakeError(ValueError):
    """A proposal is malformed or conflicts with a durable intake ref."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _package(ecosystem: str, value: str) -> str:
    if not isinstance(value, str) or len(value) > 255:
        raise IntakeError("invalid package identifier")
    package = value.lower()
    if ecosystem == "npm" and _NPM.fullmatch(package):
        return package
    if ecosystem == "pypi" and _PYPI.fullmatch(package):
        return re.sub(r"[-_.]+", "-", package)
    raise IntakeError("unsupported ecosystem or invalid package identifier")


def _url(value: Any, hosts: set[str]) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and parsed.hostname in hosts and port in (None, 443)
            and not parsed.username and not parsed.password and not parsed.fragment)


def proposal_id(proposal: dict[str, Any]) -> str:
    """Stable across rechecks of the same exact artifact and target."""
    return hashlib.sha256(_canonical({key: proposal[key] for key in _IDENTITY})).hexdigest()


def validate_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    """Validate the untrusted branch payload before a collector considers it."""
    required = {"schema_version", "id", *_IDENTITY, "metadata_url", "artifact_url",
                "checked_at", "commands"}
    if not isinstance(proposal, dict) or set(proposal) != required:
        raise IntakeError("proposal fields differ from schema v1")
    if proposal["schema_version"] != PROPOSAL_VERSION or proposal["verifier"] != CONTRACT_VERSION:
        raise IntakeError("unsupported proposal or verifier version")
    eco = proposal["ecosystem"]
    if eco not in {"npm", "pypi"} or _package(eco, proposal["package"]) != proposal["package"]:
        raise IntakeError("proposal package is not canonical")
    if not isinstance(proposal["version"], str) or not 0 < len(proposal["version"]) <= 128:
        raise IntakeError("invalid release version")
    if proposal["selector"] not in {"latest", "exact"}:
        raise IntakeError("invalid release selector")
    target = proposal["target"]
    if (not isinstance(target, dict) or len(target) > 12 or
            any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 64 or len(v) > 128
                for k, v in target.items()) or len(_canonical(target)) > 1024):
        raise IntakeError("invalid target variant")
    if not _DIGEST.fullmatch(str(proposal["artifact_digest"])):
        raise IntakeError("invalid artifact digest")
    metadata_hosts = {"registry.npmjs.org"} if eco == "npm" else {"pypi.org"}
    artifact_hosts = {"registry.npmjs.org"} if eco == "npm" else {"files.pythonhosted.org"}
    if not _url(proposal["metadata_url"], metadata_hosts) or not _url(proposal["artifact_url"], artifact_hosts):
        raise IntakeError("proposal source URL is not authoritative")
    try:
        checked = datetime.fromisoformat(proposal["checked_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise IntakeError("invalid checked_at") from error
    if checked.tzinfo is None:
        raise IntakeError("checked_at requires timezone")
    commands = proposal["commands"]
    if not isinstance(commands, list) or not 1 <= len(commands) <= 128:
        raise IntakeError("verified proposal needs commands")
    seen = set()
    for command in commands:
        if not isinstance(command, dict) or set(command) != {"command", "entry_point"}:
            raise IntakeError("invalid command declaration")
        name, entry = command["command"], command["entry_point"]
        if not isinstance(name, str) or not _COMMAND.fullmatch(name) or name in seen:
            raise IntakeError("invalid or duplicate command")
        if not isinstance(entry, str) or not entry or len(entry) > 1024 or any(ord(c) < 32 for c in entry):
            raise IntakeError("invalid entry point")
        seen.add(name)
    identifier = proposal["id"]
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier != proposal_id(proposal):
        raise IntakeError("proposal ID does not match its evidence identity")
    if len(_canonical(proposal)) > 32_768:
        raise IntakeError("proposal exceeds size limit")
    return proposal


def proposal_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    if (observation.get("outcome") != "verified_entry_point" or
            observation.get("verification_complete") is not True):
        raise IntakeError("observation is not verified entry-point evidence")
    ecosystem = observation.get("ecosystem")
    package = _package(ecosystem, observation.get("canonical_package"))
    proposal = {
        "schema_version": PROPOSAL_VERSION, "ecosystem": ecosystem,
        "package": package, "version": observation.get("version"),
        "selector": "exact" if observation.get("requested_version") is not None else "latest",
        "target": observation.get("target") or {}, "verifier": observation.get("contract_version"),
        "artifact_digest": observation.get("artifact_digest"),
        "metadata_url": observation.get("metadata_url"),
        "artifact_url": observation.get("artifact_url"),
        "checked_at": observation.get("checked_at"),
        "commands": observation.get("commands"),
    }
    try:
        proposal["id"] = proposal_id(proposal)
    except (KeyError, TypeError, ValueError) as error:
        raise IntakeError("verified observation omits proposal identity") from error
    return validate_proposal(proposal)


def _git(repository: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    return subprocess.run(["git", "-C", str(repository), *args], text=True,
                          capture_output=True, timeout=timeout, env=environment)


def _checked_git(repository: Path, *args: str, timeout: int) -> str:
    result = _git(repository, *args, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Git operation failed")
    return result.stdout.strip()


def _read_remote_proposal(repository: Path, remote: str, identifier: str,
                          base_sha: str, timeout: int) -> tuple[dict[str, Any], str] | None:
    branch = INTAKE_PREFIX + identifier
    refs = _checked_git(repository, "ls-remote", "--heads", remote, branch, timeout=timeout)
    if not refs:
        return None
    sha = refs.split()[0]
    _checked_git(repository, "fetch", "--no-tags", remote, sha, timeout=timeout)
    path = f"{PROPOSAL_DIR}/{identifier}.json"
    names = _checked_git(repository, "diff-tree", "--no-commit-id", "--name-status", "-r",
                         sha, timeout=timeout).splitlines()
    parents = _checked_git(repository, "rev-list", "--parents", "-n", "1", sha,
                           timeout=timeout).split()
    if names != [f"A\t{path}"] or len(parents) != 2:
        raise IntakeError("existing intake branch changes unexpected files")
    if _git(repository, "merge-base", "--is-ancestor", parents[1], base_sha,
            timeout=timeout).returncode:
        raise IntakeError("existing intake branch is outside artifact-data history")
    size = int(_checked_git(repository, "cat-file", "-s", f"{sha}:{path}", timeout=timeout))
    if size > 32_768:
        raise IntakeError("existing intake proposal exceeds size limit")
    body = _checked_git(repository, "show", f"{sha}:{path}", timeout=timeout)
    try:
        payload = validate_proposal(json.loads(body))
    except (ValueError, json.JSONDecodeError) as error:
        raise IntakeError("existing intake branch has invalid proposal") from error
    return payload, sha


def _read_ledger(repository: Path, base_sha: str, identifier: str,
                 timeout: int) -> dict[str, Any] | None:
    path = f"{LEDGER_DIR}/{identifier}.json"
    result = _git(repository, "show", f"{base_sha}:{path}", timeout=timeout)
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        raise IntakeError("existing intake ledger is invalid") from error
    if not isinstance(value, dict) or value.get("id") != identifier:
        raise IntakeError("existing intake ledger identity differs")
    return value


def reserve_observation(observation: dict[str, Any], repository: str | Path, *,
                        remote: str = "origin", timeout: int = 20) -> dict[str, Any]:
    """Push one immutable intake branch and read it back before returning ACK."""
    try:
        proposal = proposal_from_observation(observation)
    except IntakeError:
        return {"status": "not_eligible"}
    identifier = proposal["id"]
    if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", remote):
        return {"status": "unavailable", "reason": "invalid Git remote"}
    if not isinstance(timeout, int) or not 1 <= timeout <= 60:
        return {"status": "unavailable", "reason": "invalid Git timeout"}
    repository = Path(repository)
    branch = INTAKE_PREFIX + identifier
    try:
        if _git(repository, "rev-parse", "--is-inside-work-tree", timeout=timeout).returncode:
            return {"status": "unavailable", "reason": "designated runner has no Git checkout"}
        base = _checked_git(repository, "ls-remote", "--heads", remote, "artifact-data", timeout=timeout)
        if not base:
            return {"status": "unavailable", "reason": "artifact-data branch is unavailable"}
        base_sha = base.split()[0]
        _checked_git(repository, "fetch", "--no-tags", remote, base_sha, timeout=timeout)
        ledger = _read_ledger(repository, base_sha, identifier, timeout)
        if ledger:
            return {"status": "already_processed", "reservation_id": identifier,
                    "outcome": ledger.get("outcome"), "snapshot": ledger.get("snapshot")}
        existing = _read_remote_proposal(repository, remote, identifier, base_sha, timeout)
        if existing:
            if _canonical({key: existing[0][key] for key in existing[0] if key != "checked_at"}) != _canonical(
                    {key: proposal[key] for key in proposal if key != "checked_at"}):
                return {"status": "unavailable", "reason": "intake branch evidence conflicts"}
            return {"status": "reserved", "reservation_id": identifier, "branch": branch,
                    "commit": existing[1]}
        with tempfile.TemporaryDirectory(prefix="ge-intake-") as temporary:
            worktree = Path(temporary) / "tree"
            _checked_git(repository, "worktree", "add", "--detach", str(worktree), base_sha,
                         timeout=timeout)
            try:
                path = worktree / PROPOSAL_DIR / f"{identifier}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(_canonical(proposal) + b"\n")
                _checked_git(worktree, "add", "--", str(path.relative_to(worktree)), timeout=timeout)
                _checked_git(worktree, "-c", "core.hooksPath=/dev/null", "-c", "user.name=global-executables-intake",
                             "-c", "user.email=intake@users.noreply.github.com", "commit", "-m",
                             f"Reserve package discovery {identifier}", timeout=timeout)
                # The empty expected value makes the ref creation atomic: even
                # a concurrent ref at our base commit cannot be fast-forwarded.
                _git(worktree, "push", f"--force-with-lease=refs/heads/{branch}:",
                     remote, f"HEAD:refs/heads/{branch}", timeout=timeout)
            finally:
                _git(repository, "worktree", "remove", "--force", str(worktree), timeout=timeout)
        # Read back even after an apparent push success: a lost reply is not proof
        # of failure, and an unconfirmed local commit is not a durable ACK.
        for _ in range(3):
            existing = _read_remote_proposal(repository, remote, identifier, base_sha, timeout)
            if existing:
                if _canonical({key: existing[0][key] for key in existing[0] if key != "checked_at"}) != _canonical(
                        {key: proposal[key] for key in proposal if key != "checked_at"}):
                    return {"status": "unavailable", "reason": "intake branch evidence conflicts"}
                return {"status": "reserved", "reservation_id": identifier,
                        "branch": branch, "commit": existing[1]}
            # The collector may have accepted and deleted the ref already.
            latest = _checked_git(repository, "ls-remote", "--heads", remote, "artifact-data", timeout=timeout)
            if latest:
                latest_sha = latest.split()[0]
                _checked_git(repository, "fetch", "--no-tags", remote, latest_sha, timeout=timeout)
                ledger = _read_ledger(repository, latest_sha, identifier, timeout)
                if ledger:
                    return {"status": "already_processed", "reservation_id": identifier,
                            "outcome": ledger.get("outcome"), "snapshot": ledger.get("snapshot")}
        return {"status": "unavailable", "reason": "intake ref could not be read back"}
    except (OSError, subprocess.TimeoutExpired, RuntimeError, IntakeError):
        return {"status": "unavailable", "reason": "Git intake could not be confirmed"}
