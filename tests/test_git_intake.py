"""End-to-end Git intake reservations against disposable bare remotes."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from global_executables import git_intake


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *args],
                            text=True, capture_output=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "-q")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    git(seed, "config", "user.name", "Tester")
    git(seed, "config", "user.email", "test@example.invalid")
    (seed / "README.md").write_text("seed\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-qm", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "HEAD:refs/heads/artifact-data")
    return seed, remote


def observation(*, package="typescript", checked="2026-10-21T12:00:00Z", command="tsc"):
    return {
        "contract_version": "package-discovery-v1", "outcome": "verified_entry_point",
        "verification_complete": True, "ecosystem": "npm", "canonical_package": package,
        "version": "7.0.2", "requested_version": None, "target": {},
        "artifact_digest": "sha256:" + "a" * 64,
        "metadata_url": f"https://registry.npmjs.org/{package}/latest",
        "artifact_url": f"https://registry.npmjs.org/{package}/-/{package}-7.0.2.tgz",
        "checked_at": checked, "commands": [{"command": command, "entry_point": "./bin/tsc"}],
    }


def test_durable_ack_and_exact_single_file_delta(repo):
    checkout, remote = repo
    result = git_intake.reserve_observation(observation(), checkout)
    assert result["status"] == "reserved"
    identifier = result["reservation_id"]
    branch = "refs/heads/intake/" + identifier
    head = git(remote, "rev-parse", branch)
    base = git(remote, "rev-parse", "refs/heads/artifact-data")
    assert git(remote, "rev-parse", head + "^") == base
    assert git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", head) == (
        f"{git_intake.PROPOSAL_DIR}/{identifier}.json")
    payload = json.loads(git(remote, "show", f"{head}:{git_intake.PROPOSAL_DIR}/{identifier}.json"))
    assert git_intake.validate_proposal(payload)["id"] == identifier
    assert result["commit"] == head


def test_same_artifact_duplicate_coalesces_across_worktrees(repo, tmp_path):
    checkout, remote = repo
    second = tmp_path / "second"
    git(tmp_path, "clone", "-q", str(remote), str(second))
    first = git_intake.reserve_observation(observation(), checkout)
    later = git_intake.reserve_observation(
        observation(checked="2026-10-21T12:01:00Z"), second)
    assert later["status"] == "reserved"
    assert later["reservation_id"] == first["reservation_id"]
    assert later["commit"] == first["commit"]
    assert len(git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/intake").splitlines()) == 1


def test_two_distinct_artifacts_use_independent_refs(repo, tmp_path):
    checkout, remote = repo
    second = tmp_path / "second"
    git(tmp_path, "clone", "-q", str(remote), str(second))
    first = git_intake.reserve_observation(observation(), checkout)
    other = git_intake.reserve_observation(observation(package="other-tool", command="other-tool"), second)
    assert first["status"] == other["status"] == "reserved"
    assert first["reservation_id"] != other["reservation_id"]
    assert len(git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/intake").splitlines()) == 2


def test_push_reply_lost_is_recovered_by_ref_readback(repo, monkeypatch):
    checkout, _remote = repo
    original = git_intake._git

    def lost_reply(directory, *args, timeout):
        result = original(directory, *args, timeout=timeout)
        if args and args[0] == "push" and result.returncode == 0:
            return subprocess.CompletedProcess(result.args, 1, "", "connection lost")
        return result

    monkeypatch.setattr(git_intake, "_git", lost_reply)
    assert git_intake.reserve_observation(observation(), checkout)["status"] == "reserved"


def test_conflicting_existing_branch_is_not_overwritten(repo):
    checkout, remote = repo
    original = observation()
    first = git_intake.reserve_observation(original, checkout)
    identifier = first["reservation_id"]
    # An alternate declaration with the same artifact identity is a conflict.
    changed = observation(command="tsserver")
    result = git_intake.reserve_observation(changed, checkout)
    assert result == {"status": "unavailable", "reason": "intake branch evidence conflicts"}
    assert git(remote, "rev-parse", "refs/heads/intake/" + identifier) == first["commit"]


def test_no_remote_or_unverified_result_cannot_ack(repo):
    checkout, _remote = repo
    result = git_intake.reserve_observation({**observation(), "verification_complete": False}, checkout)
    assert result["status"] == "not_eligible"
    git(checkout, "remote", "remove", "origin")
    assert git_intake.reserve_observation(observation(), checkout)["status"] == "unavailable"


def test_processed_ledger_prevents_recreating_deleted_branch(repo):
    checkout, remote = repo
    first = git_intake.reserve_observation(observation(), checkout)
    identifier = first["reservation_id"]
    git(checkout, "checkout", "-q", "-b", "artifact-data")
    path = checkout / git_intake.LEDGER_DIR / f"{identifier}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"id": identifier, "outcome": "accepted", "snapshot": "2026-10-21"}))
    git(checkout, "add", str(path.relative_to(checkout)))
    git(checkout, "commit", "-qm", "accept reservation")
    git(checkout, "push", "-q", "origin", "HEAD:refs/heads/artifact-data")
    git(remote, "update-ref", "-d", "refs/heads/intake/" + identifier)
    result = git_intake.reserve_observation(observation(), checkout)
    assert result == {"status": "already_processed", "reservation_id": identifier,
                      "outcome": "accepted", "snapshot": "2026-10-21"}
    assert not git(remote, "for-each-ref", "--format=%(refname)", "refs/heads/intake")
