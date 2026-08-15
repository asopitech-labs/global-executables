import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import global_executables.freshness as freshness
from global_executables.freshness import run_scan
from global_executables.search import Dataset


def _record(command: str, package: str = "pkg", version: str = "1") -> dict:
    return {
        "command": command,
        "ecosystem": "fixture",
        "package": package,
        "version": version,
        "repository": None,
        "source": "fixture",
        "confidence": "direct",
    }


def _manifest(root: Path, paths: dict[str, Path]) -> Path:
    target = root / "manifest.json"
    target.write_text(json.dumps({
        "version": 1,
        "partitions": [
            {"id": key, "source": key.split("/", 1)[0], "input": str(path.relative_to(root))}
            for key, path in paths.items()
        ],
    }))
    return target


def test_scheduler_is_deterministic_and_fair(tmp_path):
    paths = {}
    for name, command in (("a/one", "one"), ("b/two", "two"), ("c/three", "three")):
        path = tmp_path / f"{name[0]}.jsonl"
        path.write_text(json.dumps(_record(command)) + "\n")
        paths[name] = path
    manifest = _manifest(tmp_path, paths)
    state = tmp_path / "state.json"
    report = tmp_path / "report.json"
    times = [datetime(2026, 8, 15, hour, tzinfo=timezone.utc) for hour in range(4)]
    selected = []
    for observed_at in times:
        result = run_scan(tmp_path, manifest, state, report, partition_budget=1,
                          record_budget=10, observed_at=observed_at)
        selected.append(result["scheduler"]["selected"])
    assert selected == [["a/one"], ["b/two"], ["c/three"], ["a/one"]]
    assert json.loads(state.read_text())["next_partition"] == 1


def test_partial_scan_detects_changes_and_removals_only_after_full_cycle(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(_record("one")) + "\n" + json.dumps(_record("two")) + "\n")
    manifest = _manifest(tmp_path, {"fixture/all": source})
    state = tmp_path / "state.json"
    report = tmp_path / "report.json"
    first = run_scan(tmp_path, manifest, state, report, record_budget=1,
                     observed_at=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert first["summary"]["new_records"] == 1 and not first["partitions"][0]["cycle_completed"]
    second = run_scan(tmp_path, manifest, state, report, record_budget=1,
                      observed_at=datetime(2026, 8, 15, 1, tzinfo=timezone.utc))
    assert second["partitions"][0]["cycle_completed"]

    source.write_text(json.dumps(_record("one", version="2")) + "\n")
    changed = run_scan(tmp_path, manifest, state, report, record_budget=10,
                       observed_at=datetime(2026, 8, 15, 2, tzinfo=timezone.utc))
    partition = changed["partitions"][0]
    assert partition["changed_records"] == 1
    assert partition["removed_records"] == 1
    assert changed["coverage_kind"] == "partial"
    assert json.loads(report.read_text())["status"] == "success"


def test_failure_preserves_last_success_and_is_visible(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(_record("one")) + "\n")
    manifest = _manifest(tmp_path, {"fixture/all": source})
    state = tmp_path / "state.json"
    report = tmp_path / "report.json"
    run_scan(tmp_path, manifest, state, report, record_budget=10,
             observed_at=datetime(2026, 8, 15, tzinfo=timezone.utc))
    saved = json.loads(state.read_text())["partitions"]["fixture/all"]["last_success_at"]
    source.unlink()
    result = run_scan(tmp_path, manifest, state, report, record_budget=10,
                      observed_at=datetime(2026, 8, 16, tzinfo=timezone.utc))
    assert result["status"] == "failed"
    assert result["failed_partitions"] == ["fixture/all"]
    assert result["partitions"][0]["unavailable"] is True
    assert json.loads(state.read_text())["partitions"]["fixture/all"]["last_success_at"] == saved


def test_budget_skips_partitions_and_manifest_rejects_duplicates(tmp_path):
    paths = {}
    for name in ("a/one", "b/two"):
        path = tmp_path / f"{name[0]}.jsonl"
        path.write_text(json.dumps(_record(name)) + "\n")
        paths[name] = path
    manifest = _manifest(tmp_path, paths)
    result = run_scan(tmp_path, manifest, tmp_path / "state.json", tmp_path / "report.json",
                      partition_budget=2, record_budget=1,
                      observed_at=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert result["summary"]["skipped_partitions"] == 1
    assert result["skipped_partitions"] == ["b/two"]

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps({"version": 1, "partitions": [
        {"id": "same", "source": "a", "input": "a.jsonl"},
        {"id": "same", "source": "b", "input": "b.jsonl"},
    ]}))
    with pytest.raises(ValueError, match="duplicate"):
        run_scan(tmp_path, duplicate, tmp_path / "bad-state.json", tmp_path / "bad-report.json")


def test_transient_partition_failure_is_retried(tmp_path, monkeypatch):
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(_record("one")) + "\n")
    manifest = _manifest(tmp_path, {"fixture/all": source})
    original = freshness._scan_partition
    calls = 0

    def flaky(partition, previous, observed_at, record_budget, byte_budget):
        nonlocal calls
        calls += 1
        if calls == 1:
            return previous, {"id": partition["id"], "source": partition["source"], "scope": {},
                              "status": "failed", "checked_at": "2026-08-15T00:00:00Z",
                              "error": "transient source timeout", "unavailable": True}
        return original(partition, previous, observed_at, record_budget, byte_budget)

    monkeypatch.setattr(freshness, "_scan_partition", flaky)
    result = run_scan(tmp_path, manifest, tmp_path / "state.json", tmp_path / "report.json",
                      retries=1, observed_at=datetime(2026, 8, 15, tzinfo=timezone.utc))
    assert calls == 2
    assert result["status"] == "success"
    assert result["partitions"][0]["attempts"] == 2
    assert result["partitions"][0]["retries"] == 1
