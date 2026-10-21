"""Exercise the scheduled Git collector against a local bare Git remote."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from global_executables.git_intake import reserve_observation
from global_executables.git_intake_collector import run
from global_executables.on_demand import persist_observation


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=artifact-data")
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=artifact-data")
    git(seed, "config", "user.name", "Fixture")
    git(seed, "config", "user.email", "fixture@example.test")
    (seed / "README").write_text("artifact fixture\n")
    (seed / ".gitignore").write_text("data/production/intermediate/\n")
    git(seed, "add", "README", ".gitignore")
    git(seed, "commit", "-m", "Seed artifact data")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "HEAD:artifact-data")
    runner = tmp_path / "runner"
    subprocess.run(["git", "clone", str(remote), str(runner)],
                   capture_output=True, text=True, check=True)
    return remote, runner


def observation(*, version="1.0", checked_at="2026-10-21T00:00:00Z") -> dict:
    return {
        "contract_version": "package-discovery-v1", "outcome": "verified_entry_point",
        "verification_complete": True, "ecosystem": "npm",
        "package": "sample-package", "canonical_package": "sample-package",
        "requested_version": None, "requested_command": None, "target": {},
        "version": version, "commands": [{"command": "sample", "entry_point": "bin/sample.js"}],
        "checked_at": checked_at, "metadata_url": f"https://registry.npmjs.org/sample-package/{version}",
        "artifact_url": f"https://registry.npmjs.org/sample-package/-/sample-package-{version}.tgz",
        "artifact_digest": "sha256:" + "a" * 64,
        "verification_method": "npm-package-json-bin-and-tar-member",
        "cache": {"status": "miss", "age_seconds": 0}, "unresolved": [],
    }


class Discovery:
    def __init__(self, result: dict):
        self.result = result
        self.calls = []

    def discover(self, ecosystem, package, version=None, command=None, target=None, *, revalidate=False):
        self.calls.append((ecosystem, package, version, command, target, revalidate))
        return dict(self.result)


def test_verified_intake_is_merged_and_marks_refresh_pending(tmp_path):
    remote, runner = repository(tmp_path)
    selected = observation()
    reserved = reserve_observation(selected, runner)
    assert reserved["status"] == "reserved"

    engine = Discovery(selected)
    result = run(runner, discovery_engine=engine)
    assert result == {"accepted": 1, "rejected": 0, "retryable": 0,
                      "needs_refresh": True}
    assert engine.calls == [("npm", "sample-package", None, None, {}, True)]
    accepted = json.loads(git(runner, "show", "origin/artifact-data:data/production/on-demand/intake-ledger/"
                              + reserved["reservation_id"] + ".json"))
    assert accepted["status"] == "accepted"
    assert accepted["outcome"] == "current"
    assert accepted["refresh_pending"] is True
    assert git(runner, "ls-remote", "--heads", "origin", reserved["branch"]) == ""
    parent_line = git(runner, "rev-list", "--parents", "-n", "1", "origin/artifact-data")
    assert len(parent_line.split()) == 3, "accepted proposal must be a merge parent"


def test_changed_public_artifact_rejects_without_projecting_provider(tmp_path):
    _remote, runner = repository(tmp_path)
    reserved = reserve_observation(observation(), runner)
    changed = observation()
    changed["artifact_digest"] = "sha256:" + "b" * 64
    result = run(runner, discovery_engine=Discovery(changed))
    assert result["rejected"] == 1, result
    assert result["needs_refresh"] is False
    ledger = json.loads(git(runner, "show", "origin/artifact-data:data/production/on-demand/intake-ledger/"
                            + reserved["reservation_id"] + ".json"))
    assert ledger["status"] == "rejected"
    assert "artifact_digest" in ledger["reason"]
    assert not (runner / "data/production/intermediate/on-demand.jsonl").exists()


def test_reused_id_with_different_evidence_is_audited(tmp_path):
    _remote, runner = repository(tmp_path)
    reserved = reserve_observation(observation(), runner)
    assert run(runner, discovery_engine=Discovery(observation()))["accepted"] == 1
    manifest = "data/production/on-demand/intake/" + reserved["reservation_id"] + ".json"
    proposal = json.loads(git(runner, "show", "origin/artifact-data:" + manifest))
    proposal["metadata_url"] += "?different=1"
    path = runner / manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal) + "\n")
    git(runner, "add", manifest)
    git(runner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-m", "Conflicting evidence for the same ID")
    candidate = git(runner, "rev-parse", "HEAD")
    git(runner, "push", "origin", "HEAD:refs/heads/" + reserved["branch"])

    result = run(runner, discovery_engine=Discovery(observation()))
    assert result["rejected"] == 1
    conflict = "data/production/on-demand/intake-conflicts/" + reserved["reservation_id"] + "/" + candidate + ".json"
    audit = json.loads(git(runner, "show", "origin/artifact-data:" + conflict))
    assert audit["candidate_sha"] == candidate
    assert "different proposal evidence" in audit["reason"]
    assert git(runner, "ls-remote", "--heads", "origin", reserved["branch"]) == ""


def test_crash_after_merge_before_ref_cleanup_replays_without_duplicate(tmp_path, monkeypatch):
    _remote, runner = repository(tmp_path)
    reserved = reserve_observation(observation(), runner)
    from global_executables import git_intake_collector as collector

    original = collector._git

    def lost_cleanup(root, *args, **kwargs):
        if args[:2] == ("push", "origin") and str(args[2]).startswith(":refs/heads/intake/"):
            return subprocess.CompletedProcess(args, 1, "", "simulated cleanup outage")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(collector, "_git", lost_cleanup)
    first = collector.run(runner, discovery_engine=Discovery(observation()))
    assert first["accepted"] == 1
    assert git(runner, "ls-remote", "--heads", "origin", reserved["branch"])
    accepted_head = git(runner, "ls-remote", "--heads", "origin", "artifact-data").split()[0]

    monkeypatch.setattr(collector, "_git", original)
    second = collector.run(runner, discovery_engine=Discovery(observation()))
    assert second["accepted"] == 0
    assert git(runner, "ls-remote", "--heads", "origin", reserved["branch"]) == ""
    assert git(runner, "ls-remote", "--heads", "origin", "artifact-data").split()[0] == accepted_head


def test_moving_latest_retains_verified_previous_release_as_history(tmp_path):
    _remote, runner = repository(tmp_path)
    reserved = reserve_observation(observation(), runner)

    class AdvancedRegistry(Discovery):
        def discover(self, ecosystem, package, version=None, command=None, target=None, *, revalidate=False):
            self.calls.append((ecosystem, package, version, command, target, revalidate))
            result = observation(version="2.0") if version is None else observation(version=version)
            result["requested_version"] = version
            return result

    engine = AdvancedRegistry(observation())
    result = run(runner, discovery_engine=engine)
    assert result == {"accepted": 1, "rejected": 0, "retryable": 0, "needs_refresh": False}
    assert [call[2] for call in engine.calls] == [None, "1.0"]
    ledger = json.loads(git(runner, "show", "origin/artifact-data:data/production/on-demand/intake-ledger/"
                            + reserved["reservation_id"] + ".json"))
    assert ledger["outcome"] == "historical"
    assert ledger["refresh_pending"] is False


def test_transient_registry_outage_writes_retry_record_and_keeps_branch(tmp_path):
    _remote, runner = repository(tmp_path)
    reserved = reserve_observation(observation(), runner)
    unavailable = observation()
    unavailable["outcome"] = "temporarily_unavailable"
    engine = Discovery(unavailable)

    first = run(runner, discovery_engine=engine)
    assert first["retryable"] == 1 and first["accepted"] == 0
    assert git(runner, "ls-remote", "--heads", "origin", reserved["branch"])
    ledger = json.loads(git(runner, "show", "origin/artifact-data:data/production/on-demand/intake-ledger/"
                            + reserved["reservation_id"] + ".json"))
    assert ledger["status"] == "retryable"
    assert ledger["attempts"] == 1 and ledger["next_attempt"]
    assert "temporarily_unavailable" in ledger["reason"]

    second = run(runner, discovery_engine=engine)
    assert second["retryable"] == 0
    assert len(engine.calls) == 1, "backoff must avoid another immediate registry request"


def test_aged_active_claim_is_rechecked_and_authoritative_withdrawal_is_published(tmp_path):
    _remote, runner = repository(tmp_path)
    old = observation(checked_at="2026-10-01T00:00:00Z")
    persist_observation(runner, old)
    git(runner, "add", "data/production/on-demand")
    git(runner, "add", "-f", "data/production/intermediate/on-demand.jsonl")
    git(runner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-m", "Seed old current claim")
    git(runner, "push", "origin", "HEAD:artifact-data")
    removed = observation(checked_at=datetime.now(timezone.utc).isoformat())
    removed.update(outcome="not_published", commands=[], version=None)
    engine = Discovery(removed)

    result = run(runner, sweep_limit=5, discovery_engine=engine)
    assert result["needs_refresh"] is True
    claim_files = git(runner, "ls-tree", "-r", "--name-only", "origin/artifact-data",
                      "data/production/on-demand/claims").splitlines()
    claim = json.loads(git(runner, "show", "origin/artifact-data:" + claim_files[0]))
    assert claim["status"] == "withdrawn"
    assert claim["publication"]["status"] == "queued_for_refresh"
    assert engine.calls == [("npm", "sample-package", None, None, {}, True)]


def test_backoff_does_not_starve_other_intake_refs(tmp_path):
    _remote, runner = repository(tmp_path)
    first = observation()
    second = observation()
    second.update(package="another-package", canonical_package="another-package",
                  metadata_url="https://registry.npmjs.org/another-package/1.0",
                  artifact_url="https://registry.npmjs.org/another-package/-/another-package-1.0.tgz")
    refs = [reserve_observation(item, runner) for item in (first, second)]
    assert all(item["status"] == "reserved" for item in refs)
    ordered = sorted((item["reservation_id"], obs) for item, obs in zip(refs, (first, second)))
    deferred_package = ordered[0][1]["canonical_package"]

    class SometimesUnavailable(Discovery):
        def discover(self, ecosystem, package, version=None, command=None, target=None, *, revalidate=False):
            self.calls.append((ecosystem, package, version, command, target, revalidate))
            if package == deferred_package:
                return {"outcome": "temporarily_unavailable"}
            return dict(second if second["canonical_package"] == package else first)

    engine = SometimesUnavailable(first)
    first_run = run(runner, limit=1, sweep_limit=0, discovery_engine=engine)
    assert first_run["retryable"] == 1
    second_run = run(runner, limit=1, sweep_limit=0, discovery_engine=engine)
    assert second_run["accepted"] == 1


def test_refresh_pending_clears_only_after_matching_claim_readback(tmp_path):
    _remote, runner = repository(tmp_path)
    selected = observation()
    reserve_observation(selected, runner)
    first = run(runner, sweep_limit=0, discovery_engine=Discovery(selected))
    assert first["needs_refresh"] is True

    git(runner, "fetch", "origin", "artifact-data")
    git(runner, "reset", "--hard", "origin/artifact-data")
    path = next((runner / "data/production/on-demand/claims").glob("*.json"))
    claim = json.loads(path.read_text())
    claim["publication"] = {"status": "indexed", "snapshot": "2026-10-21"}
    path.write_text(json.dumps(claim))
    git(runner, "add", str(path.relative_to(runner)))
    git(runner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-m", "Record dictionary readback")
    git(runner, "push", "origin", "HEAD:artifact-data")

    second = run(runner, sweep_limit=0, discovery_engine=Discovery(selected))
    assert second["needs_refresh"] is False


def test_malformed_intake_ref_is_audited_with_candidate_sha(tmp_path):
    _remote, runner = repository(tmp_path)
    candidate = git(runner, "rev-parse", "HEAD")
    git(runner, "push", "origin", "HEAD:refs/heads/intake/not-a-valid-id")

    result = run(runner, sweep_limit=0, discovery_engine=Discovery(observation()))
    assert result["rejected"] == 1
    files = git(runner, "ls-tree", "-r", "--name-only", "origin/artifact-data",
                "data/production/on-demand/intake-ledger").splitlines()
    assert len(files) == 1
    ledger = json.loads(git(runner, "show", "origin/artifact-data:" + files[0]))
    assert ledger["status"] == "rejected"
    assert ledger["candidate_sha"] == candidate
    assert ledger["reason"] == "invalid intake branch name"


def test_sweep_cursor_advances_past_repeated_transient_failures(tmp_path):
    _remote, runner = repository(tmp_path)
    for index in range(6):
        item = observation(checked_at="2026-10-01T00:00:00Z")
        package = f"sample-package-{index}"
        item.update(package=package, canonical_package=package,
                    metadata_url=f"https://registry.npmjs.org/{package}/1.0",
                    artifact_url=f"https://registry.npmjs.org/{package}/-/archive.tgz")
        persist_observation(runner, item)
    git(runner, "add", "data/production/on-demand")
    git(runner, "add", "-f", "data/production/intermediate/on-demand.jsonl")
    git(runner, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-m", "Seed aged claims")
    git(runner, "push", "origin", "HEAD:artifact-data")
    failed = observation()
    failed["outcome"] = "temporarily_unavailable"
    engine = Discovery(failed)

    run(runner, limit=1, sweep_limit=5, discovery_engine=engine)
    first_packages = {call[1] for call in engine.calls}
    assert len(first_packages) == 5
    run(runner, limit=1, sweep_limit=1, discovery_engine=engine)
    assert len({call[1] for call in engine.calls}) == 6
