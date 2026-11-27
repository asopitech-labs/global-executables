"""Collect immutable Git intake proposals into the source-owned artifact branch.

This collector runs in Actions with write access to ``artifact-data``. The
requesting agent can only create ``intake/<id>`` refs. A proposal is never
trusted as a dictionary observation: it is checked against a fresh public
release before its branch becomes a merge parent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .discovery import PackageDiscovery
from .git_intake import validate_proposal
from .on_demand import ROOT, PROJECTION, persist_observation

LEDGER = ROOT / "intake-ledger"
INTAKE = ROOT / "intake"
_ID = re.compile(r"^[0-9a-f]{64}$")


class IntakeBranchError(ValueError):
    """A branch is not a single valid immutable intake proposal."""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], text=True,
                                capture_output=True, check=False, timeout=30,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                     "GCM_INTERACTIVE": "Never"})
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"git {args[0]} timed out") from error
    if check and result.returncode:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def _json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _fingerprint(proposal: dict[str, Any]) -> str:
    stable = {key: value for key, value in proposal.items() if key != "checked_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _candidate(root: Path, ref: str, identifier: str) -> tuple[str, dict[str, Any]]:
    """Read a one-file proposal only after validating its ancestry and tree delta."""
    sha = _git(root, "rev-parse", ref).stdout.strip()
    parents = _git(root, "rev-list", "--parents", "-n", "1", sha).stdout.split()
    if len(parents) != 2:
        raise IntakeBranchError("proposal must be one commit with one parent")
    parent = parents[1]
    if _git(root, "merge-base", "--is-ancestor", parent, "origin/artifact-data",
            check=False).returncode:
        raise IntakeBranchError("proposal parent is not an artifact-data ancestor")
    path = (INTAKE / f"{identifier}.json").as_posix()
    delta = _git(root, "diff", "--name-status", parent, sha).stdout.strip().splitlines()
    if delta != [f"A\t{path}"]:
        raise IntakeBranchError("proposal changes files outside its single intake manifest")
    tree = _git(root, "ls-tree", sha, path).stdout.strip()
    if not tree.startswith("100644 blob "):
        raise IntakeBranchError("proposal must be a regular JSON blob")
    size = int(_git(root, "cat-file", "-s", f"{sha}:{path}").stdout.strip())
    if size > 32_768:
        raise IntakeBranchError("proposal exceeds size limit")
    try:
        proposal = json.loads(_git(root, "show", f"{sha}:{path}").stdout)
        proposal = validate_proposal(proposal)
    except (ValueError, KeyError, TypeError) as error:
        raise IntakeBranchError(f"invalid proposal: {error}") from error
    if proposal.get("id") != identifier:
        raise IntakeBranchError("proposal ID does not match intake branch")
    return sha, proposal


def _reverify(engine: PackageDiscovery, proposal: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Verify the proposed exact release, then identify the current neutral release."""
    requested_version = proposal["version"] if proposal["selector"] == "exact" else None
    checked = engine.discover(proposal["ecosystem"], proposal["package"], requested_version,
                              target=proposal["target"], revalidate=True)
    latest_changed = (proposal["selector"] == "latest" and checked.get("outcome") == "verified_entry_point"
                      and checked.get("version") != proposal["version"])
    if latest_changed:
        # A moving alias may have advanced since the agent reserved it. Verify
        # the proposed exact release separately and retain it as history.
        checked = engine.discover(proposal["ecosystem"], proposal["package"], proposal["version"],
                                  target=proposal["target"], revalidate=True)
    outcome = checked.get("outcome")
    if outcome in {"temporarily_unavailable", "rate_limited"}:
        raise RuntimeError(f"public revalidation: {outcome}")
    if outcome != "verified_entry_point" or not checked.get("verification_complete"):
        raise IntakeBranchError(f"public release no longer verifies: {outcome}")
    for key, expected in (("canonical_package", proposal["package"]),
                          ("version", proposal["version"]),
                          ("artifact_digest", proposal["artifact_digest"])):
        if checked.get(key) != expected:
            raise IntakeBranchError(f"public {key} differs from proposal")
    if checked.get("commands") != proposal["commands"]:
        raise IntakeBranchError("public entry points differ from proposal")
    if latest_changed:
        return checked, None
    if proposal["selector"] == "latest" and not proposal["target"]:
        return checked, checked
    current = engine.discover(proposal["ecosystem"], proposal["package"],
                              target={}, revalidate=True)
    if current.get("outcome") in {"temporarily_unavailable", "rate_limited"}:
        raise RuntimeError("current public release is temporarily unavailable")
    if current.get("outcome") != "verified_entry_point" or not current.get("verification_complete"):
        return checked, None
    return checked, current if current.get("version") == proposal["version"] else None


def _commit(root: Path, message: str) -> bool:
    _git(root, "add", "-A", "--", ROOT.as_posix())
    if (root / PROJECTION).exists() or _git(root, "ls-files", "--error-unmatch",
                                             PROJECTION.as_posix(), check=False).returncode == 0:
        # Production ignores intermediate outputs. This one projection is an
        # intentional source artifact on artifact-data, so stage it explicitly.
        _git(root, "add", "-f", "-A", "--", PROJECTION.as_posix())
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    _git(root, "-c", "user.name=github-actions[bot]",
         "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
         "commit", "-m", message)
    return True


def _push(root: Path) -> bool:
    return _git(root, "push", "origin", "HEAD:artifact-data", check=False).returncode == 0


def _delete_intake_ref(root: Path, identifier: str, expected_sha: str) -> bool:
    """Clean up only the candidate that has a durable ledger outcome."""
    ref = f"refs/heads/intake/{identifier}"
    return _git(root, "push", f"--force-with-lease={ref}:{expected_sha}",
                "origin", f":{ref}", check=False).returncode == 0


def _reset(root: Path) -> None:
    _git(root, "merge", "--abort", check=False)
    _git(root, "fetch", "origin", "artifact-data")
    _git(root, "reset", "--hard", "origin/artifact-data")
    _git(root, "clean", "-fd", "--", ROOT.as_posix(), PROJECTION.as_posix())


def _ledger(root: Path, identifier: str) -> Path:
    return root / LEDGER / f"{identifier}.json"


def _due(root: Path, ref: str) -> bool:
    identifier = ref.rsplit("/", 1)[-1]
    if not _ID.fullmatch(identifier):
        return True
    entry = _json(_ledger(root, identifier))
    if not entry or entry.get("status") != "retryable":
        return True
    try:
        return datetime.fromisoformat(entry["next_attempt"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError):
        return True


def _refresh_pending(root: Path) -> bool:
    for path in sorted((root / ROOT / "claims").glob("*.json")):
        claim = _json(path)
        if claim and claim.get("publication", {}).get("status") == "queued_for_refresh":
            return True
    for path in sorted((root / LEDGER).glob("*.json")):
        entry = _json(path)
        if entry and entry.get("refresh_pending"):
            claim = _json(root / ROOT / "claims" / f"{entry.get('claim_id', '')[:32]}.json")
            if not claim:
                return True
            if claim.get("event_digest") != entry.get("event_digest"):
                # A newer source event replaced this proposal; the current
                # claim's own publication state controls refresh dispatch.
                continue
            if claim.get("publication", {}).get("status") != "indexed":
                return True
    return False


def _sweep_current(root: Path, engine: PackageDiscovery, limit: int) -> int:
    """Recheck aged active claims even when no new intake branch exists."""
    changed = 0
    checked = 0
    paths = sorted((root / ROOT / "claims").glob("*.json"))
    cursor_path = root / ROOT / "sweep-cursor.json"
    previous_cursor = (_json(cursor_path) or {}).get("last_claim_id", "")
    start = next((index for index, path in enumerate(paths) if path.stem > previous_cursor), 0)
    ordered = paths[start:] + paths[:start]
    for path in ordered:
        if checked >= limit:
            break
        claim = _json(path)
        if not claim or claim.get("status") != "active":
            continue
        last = datetime.fromisoformat(claim["checked_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - last < timedelta(hours=24):
            continue
        checked += 1
        for _attempt in range(3):
            _reset(root)
            current = _json(path)
            if not current or current.get("event_digest") != claim.get("event_digest"):
                break
            observation = engine.discover(claim["ecosystem"], claim["package"],
                                          target={}, revalidate=True)
            outcome = observation.get("outcome")
            _save(cursor_path, {"last_claim_id": path.stem,
                                "last_attempt": datetime.now(timezone.utc).isoformat(),
                                "last_outcome": outcome})
            try:
                changed_claim = False
                if outcome in {"verified_entry_point", "not_published"} and observation.get("checked_at") != claim.get("checked_at"):
                    result = persist_observation(root, observation)
                    changed_claim = result["changed"]
                # `unverified` can mean ambiguous evidence; only an
                # authoritative absence may withdraw an active claim.
                _commit(root, f"Recheck on-demand claim {claim['claim_id'][:12]}")
                if _push(root):
                    changed += int(changed_claim)
                    break
            except Exception:
                _reset(root)
                break
    return changed


def _process(root: Path, ref: str, engine: PackageDiscovery) -> str:
    identifier = ref.rsplit("/", 1)[-1]
    if not _ID.fullmatch(identifier):
        identifier = hashlib.sha256(ref.encode()).hexdigest()
        tip = _git(root, "rev-parse", ref, check=False).stdout.strip()
        for _attempt in range(3):
            _reset(root)
            if _json(_ledger(root, identifier)):
                return "already_processed"
            _save(_ledger(root, identifier), {"id": identifier, "status": "rejected", "ref": ref,
                                             "candidate_sha": tip,
                                             "reason": "invalid intake branch name", "refresh_pending": False})
            _commit(root, f"Reject malformed intake ref {identifier[:12]}")
            if _push(root):
                return "rejected"
        return "retryable"
    for _attempt in range(3):
        _reset(root)
        existing = _json(_ledger(root, identifier))
        tip = _git(root, "rev-parse", ref).stdout.strip()
        if existing and existing.get("status") in {"accepted", "rejected"}:
            if existing.get("candidate_sha") == tip:
                _delete_intake_ref(root, identifier, tip)
                return "already_processed"
            try:
                _sha, replay = _candidate(root, ref, identifier)
                same = existing.get("proposal_fingerprint") == _fingerprint(replay)
            except IntakeBranchError:
                same = False
            if same:
                _delete_intake_ref(root, identifier, tip)
                return "already_processed"
            _save(root / ROOT / "intake-conflicts" / identifier / f"{tip}.json", {
                "id": identifier, "candidate_sha": tip,
                "existing_candidate_sha": existing.get("candidate_sha"),
                "reason": "same reservation ID has different proposal evidence",
            })
            _commit(root, f"Audit conflicting intake {identifier[:12]}")
            if _push(root):
                _delete_intake_ref(root, identifier, tip)
                return "rejected"
            continue
        if existing and existing.get("status") == "retryable":
            next_attempt = datetime.fromisoformat(existing["next_attempt"].replace("Z", "+00:00"))
            if next_attempt > datetime.now(timezone.utc):
                return "retryable"
        try:
            sha, proposal = _candidate(root, ref, identifier)
            checked, current = _reverify(engine, proposal)
            _git(root, "-c", "user.name=github-actions[bot]",
                 "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                 "merge", "--no-ff", "--no-commit", sha)
            persisted = persist_observation(root, current) if current else None
            claim = persisted["claim"] if persisted else None
            entry = {"id": identifier, "status": "accepted", "outcome": "current" if current else "historical",
                     "candidate_sha": sha, "package": proposal["package"],
                     "proposal_fingerprint": _fingerprint(proposal),
                     "version": proposal["version"], "verified_at": checked.get("checked_at"),
                     "event_digest": claim.get("event_digest") if claim else None,
                     "claim_id": claim.get("claim_id") if claim else None,
                     "refresh_pending": bool(persisted and persisted["changed"])}
            _save(_ledger(root, identifier), entry)
            _commit(root, f"Accept verified intake {identifier[:12]}")
            outcome = "accepted"
        except IntakeBranchError as error:
            _git(root, "merge", "--abort", check=False)
            _reset(root)
            _save(_ledger(root, identifier), {
                "id": identifier, "status": "rejected", "candidate_sha": tip,
                "reason": str(error), "refresh_pending": False,
            })
            _commit(root, f"Reject intake {identifier[:12]}")
            outcome = "rejected"
        except Exception as error:
            _reset(root)
            previous = _json(_ledger(root, identifier)) or {}
            now = datetime.now(timezone.utc)
            _save(_ledger(root, identifier), {
                "id": identifier, "status": "retryable", "candidate_sha": tip,
                "reason": str(error)[:256], "attempts": previous.get("attempts", 0) + 1,
                "next_attempt": (now + timedelta(minutes=30)).isoformat(),
                "refresh_pending": False,
            })
            _commit(root, f"Defer intake {identifier[:12]}")
            if _push(root):
                return "retryable"
            continue
        if _push(root):
            _delete_intake_ref(root, identifier, tip)
            return outcome
    return "retryable"


def run(repository: str | Path, limit: int = 20,
        sweep_limit: int = 5,
        discovery_engine: PackageDiscovery | None = None) -> dict[str, Any]:
    """Process at most ``limit`` refs, leaving failed work durable for a later run."""
    root = Path(repository).resolve()
    if not 1 <= limit <= 100 or not 0 <= sweep_limit <= 100:
        raise ValueError("limit must be 1–100 and sweep_limit 0–100")
    _git(root, "fetch", "--prune", "origin", "artifact-data",
         "+refs/heads/intake/*:refs/remotes/origin/intake/*")
    refs = _git(root, "for-each-ref", "--format=%(refname)",
                "refs/remotes/origin/intake").stdout.splitlines()
    engine = discovery_engine or PackageDiscovery()
    counts = {"accepted": 0, "rejected": 0, "retryable": 0}
    with tempfile.TemporaryDirectory(prefix="ge-intake-collector-") as scratch:
        worktree = Path(scratch) / "artifact"
        _git(root, "worktree", "add", "--detach", str(worktree), "origin/artifact-data")
        try:
            selected = 0
            for ref in refs:
                if not _due(worktree, ref):
                    continue
                if selected >= limit:
                    break
                selected += 1
                try:
                    outcome = _process(worktree, ref, engine)
                except Exception as error:
                    # A failure before its retry ledger is durable must fail
                    # the Actions job, rather than report a successful drain.
                    raise RuntimeError(f"intake {ref} failed before durable outcome") from error
                if outcome in counts:
                    counts[outcome] += 1
            _reset(worktree)
            _sweep_current(worktree, engine, sweep_limit)
            _reset(worktree)
            counts["needs_refresh"] = _refresh_pending(worktree)
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree), check=False)
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.repository, args.limit), sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
