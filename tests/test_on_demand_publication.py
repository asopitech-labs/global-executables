"""Verified live observations persist, refresh, withdraw, and reconcile by source."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from global_executables.model import write_jsonl
from global_executables.on_demand import (
    PROJECTION,
    StaleObservationError,
    claim_id,
    persist_observation,
    reconcile_indexed,
)
from global_executables.pipeline import RebuildPolicy, load_canonical, rebuild

ROOT = Path(__file__).parents[1]


def observation(*, checked_at="2026-10-21T00:00:00Z", commands=None,
                outcome="verified_entry_point", version="1.0"):
    commands = [{"command": "sample", "entry_point": "bin/sample.js"}] if commands is None else commands
    return {
        "contract_version": "package-discovery-v1",
        "outcome": outcome,
        "ecosystem": "npm",
        "package": "sample-package",
        "canonical_package": "sample-package",
        "requested_version": None,
        "requested_command": None,
        "target": {},
        "commands": commands,
        "unresolved": [] if commands else ["release declares no installed commands"],
        "checked_at": checked_at,
        "publication_status": "not_indexed",
        "verification_complete": True,
        "version": version,
        "metadata_url": f"https://registry.npmjs.org/sample-package/{version}",
        "artifact_url": f"https://registry.npmjs.org/sample-package/-/sample-package-{version}.tgz",
        "artifact_digest": "sha256:" + "a" * 64,
        "verification_method": "npm-package-json-bin-and-tar-member",
        "cache": {"status": "miss", "age_seconds": 0},
    }


def policy():
    return RebuildPolicy(replace_sources=frozenset({"on-demand"}))


def test_persist_writes_append_only_event_current_claim_and_projection(tmp_path):
    result = persist_observation(tmp_path, observation())
    identifier = claim_id("npm", "sample-package")
    claim_path = tmp_path / "data/production/on-demand/claims" / f"{identifier[:32]}.json"
    claim = json.loads(claim_path.read_text())
    rows = [json.loads(line) for line in (tmp_path / PROJECTION).read_text().splitlines()]

    assert result["changed"] is True
    assert claim["publication"] == {"status": "queued_for_refresh", "snapshot": None}
    assert claim["status"] == "active"
    assert rows[0]["source"] == "on-demand"
    assert rows[0]["claim_id"] == identifier
    assert rows[0]["provenance"][0]["event_digest"] == claim["event_digest"]
    assert (tmp_path / "data/production/on-demand/events"
            / f'{claim["event_digest"][:32]}.json').is_file()


def test_duplicate_is_idempotent_and_older_result_cannot_rewind(tmp_path):
    current = observation(checked_at="2026-10-21T02:00:00Z")
    first = persist_observation(tmp_path, current)
    second = persist_observation(tmp_path, current)
    assert first["changed"] is True and second["changed"] is False

    with pytest.raises(StaleObservationError):
        persist_observation(tmp_path, observation(checked_at="2026-10-21T01:00:00Z", version="0.9"))


def test_exact_version_observation_cannot_replace_current_release(tmp_path):
    exact = observation()
    exact["requested_version"] = "1.0"

    with pytest.raises(ValueError, match="registry-selected current release"):
        persist_observation(tmp_path, exact)


def test_verified_observation_flows_to_dictionary_and_readback_marks_indexed(tmp_path):
    artifact = tmp_path / "artifact"
    dictionary = tmp_path / "dictionary"
    persisted = persist_observation(artifact, observation())
    rebuild(dictionary, [artifact / PROJECTION], "2026-10-21", {"on-demand": "partial"}, policy=policy())

    record = load_canonical(dictionary)["sample"]
    provider = record["providers"][0]
    assert provider["source"] == "on-demand"
    assert provider["claim_id"] == persisted["claim"]["claim_id"]
    assert reconcile_indexed(artifact, dictionary) == {"indexed": 1, "queued": 0, "unchanged": 0}
    claim = json.loads(next((artifact / "data/production/on-demand/claims").glob("*.json")).read_text())
    assert claim["publication"] == {"status": "indexed", "snapshot": "2026-10-21"}


def test_failed_readback_is_queued(tmp_path):
    artifact = tmp_path / "artifact"
    dictionary = tmp_path / "dictionary"
    persist_observation(artifact, observation())
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    dictionary.mkdir()
    rebuild(dictionary, [empty], "2026-10-21", "partial")

    assert reconcile_indexed(artifact, dictionary) == {"indexed": 0, "queued": 0, "unchanged": 1}
    claim = json.loads(next((artifact / "data/production/on-demand/claims").glob("*.json")).read_text())
    assert claim["publication"]["status"] == "queued_for_refresh"


def test_authoritative_withdrawal_removes_only_on_demand_source(tmp_path):
    artifact = tmp_path / "artifact"
    dictionary = tmp_path / "dictionary"
    persist_observation(artifact, observation())
    crawler = tmp_path / "npm.jsonl"
    write_jsonl([{
        "command": "sample", "ecosystem": "npm", "package": "other-package", "version": "3",
        "repository": None, "source": "npm-registry", "confidence": "direct",
    }], crawler)
    rebuild(dictionary, [crawler, artifact / PROJECTION], "2026-10-21",
            {"npm": "partial", "on-demand": "partial"}, policy=policy())

    withdrawn = observation(checked_at="2026-10-22T00:00:00Z", commands=[],
                            outcome="unverified", version="2.0")
    persist_observation(artifact, withdrawn)
    assert (artifact / PROJECTION).read_text() == ""
    rebuild(dictionary, [crawler, artifact / PROJECTION], "2026-10-22",
            {"npm": "partial", "on-demand": "partial"}, policy=policy())

    providers = load_canonical(dictionary)["sample"]["providers"]
    assert [provider["source"] for provider in providers] == ["npm-registry"]
    assert reconcile_indexed(artifact, dictionary)["indexed"] == 1
    events = list((artifact / "data/production/on-demand/events").glob("**/*.json"))
    assert len(events) == 2


def test_withdrawal_of_sole_source_is_an_authorized_source_owned_shrink(tmp_path):
    artifact = tmp_path / "artifact"
    dictionary = tmp_path / "dictionary"
    persist_observation(artifact, observation())
    rebuild(dictionary, [artifact / PROJECTION], "2026-10-21", "partial", policy=policy())
    persist_observation(artifact, observation(checked_at="2026-10-22T00:00:00Z", commands=[],
                                              outcome="not_published", version=None))

    rebuild(dictionary, [artifact / PROJECTION], "2026-10-22", "partial", policy=policy())

    assert load_canonical(dictionary) == {}


def test_withdrawn_claim_can_reappear_with_new_verified_evidence(tmp_path):
    artifact = tmp_path / "artifact"
    persist_observation(artifact, observation())
    persist_observation(artifact, observation(
        checked_at="2026-10-22T00:00:00Z", commands=[], outcome="unverified", version="2.0"))
    result = persist_observation(artifact, observation(
        checked_at="2026-10-23T00:00:00Z", version="2.1"))

    rows = [json.loads(line) for line in (artifact / PROJECTION).read_text().splitlines()]
    assert result["claim"]["status"] == "active"
    assert rows[0]["version"] == "2.1"
    assert len(list((artifact / "data/production/on-demand/events").glob("*.json"))) == 3


def test_workflows_keep_artifact_and_dictionary_publication_separate():
    acquisition = (ROOT / ".github/workflows/on-demand-discovery.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh.yml").read_text(encoding="utf-8")

    assert "tools/git_intake_runner_smoke.py" in acquisition
    assert "GLOBAL_EXECUTABLES_INTAKE_REPOSITORY" in acquisition
    assert "HEAD:artifact-data" not in acquisition
    assert "gh workflow run git-intake-collector.yml" in acquisition
    assert "--replace-source on-demand" in refresh
    assert "reconcile-discovery" in refresh
    assert "--dictionary-root /tmp/dictionary-publish" in refresh
