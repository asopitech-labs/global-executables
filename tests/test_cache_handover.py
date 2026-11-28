"""Schedule-cache handover between a local booster and Actions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import cache_handover  # noqa: E402
from global_executables import refresh_policy  # noqa: E402
from global_executables.refresh_policy import is_due, pack, read_cache, write_cache  # noqa: E402


def test_merge_keeps_newest_check_base_on_ties_and_base_cursor(tmp_path, capsys):
    base = tmp_path / "base.cache.gz"
    handover = tmp_path / "handover.cache.gz"
    write_cache(base, {
        "base-only": pack(8, 1, "1", "n"),
        "handover-newer": pack(9, 1, "1", "n"),
        "tie": pack(10, 1, "base", "c"),
    }, 17)
    write_cache(handover, {
        "handover-only": pack(11, 2, "1", "n"),
        "handover-newer": pack(12, 3, "2", "c"),
        "tie": pack(10, 9, "handover", "n"),
    }, 99)

    assert cache_handover.merge(base, handover, "pypi")

    checks, cursor, found = read_cache(base)
    assert found and cursor == 17
    assert checks == {
        "base-only": pack(8, 1, "1", "n"),
        "handover-newer": pack(12, 3, "2", "c"),
        "handover-only": pack(11, 2, "1", "n"),
        "tie": pack(10, 1, "base", "c"),
    }
    assert json.loads(capsys.readouterr().out) == {
        "base_entries": 3,
        "handover_entries": 3,
        "kept_base": 2,
        "taken_from_handover": 2,
        "total": 4,
    }


def test_merge_uses_handover_cursor_only_for_a_cold_base(tmp_path):
    base = tmp_path / "missing.cache.gz"
    handover = tmp_path / "handover.cache.gz"
    write_cache(handover, {"a": pack(12, 1, "1", "n")}, 73)

    assert cache_handover.merge(base, handover, "pypi")
    assert read_cache(base) == ({"a": pack(12, 1, "1", "n")}, 73, True)


@pytest.mark.parametrize("kind", ["missing", "unreadable", "other-revision"])
def test_cold_handover_leaves_base_byte_identical(tmp_path, capsys, kind):
    base = tmp_path / "base.cache.gz"
    handover = tmp_path / "handover.cache.gz"
    write_cache(base, {"ci": pack(10, 1, "1", "c")}, 4)
    before = base.read_bytes()
    if kind == "unreadable":
        handover.write_bytes(b"not gzip")
    elif kind == "other-revision":
        write_cache(handover, {"booster": pack(20, 1, "1", "n")}, 8,
                    revision=refresh_policy.EXTRACTION_REVISION + 1)

    assert not cache_handover.merge(base, handover, "pypi")
    assert base.read_bytes() == before
    assert "handover cache is missing or cold" in capsys.readouterr().out


def test_merge_honours_size_cap_and_is_byte_deterministic(tmp_path, monkeypatch):
    handover = tmp_path / "handover.cache.gz"
    write_cache(handover, {
        "old": pack(1, 1, "1", "n"),
        "new-a": pack(30, 1, "1", "n"),
        "new-b": pack(20, 1, "1", "n"),
    }, 9)
    monkeypatch.setattr(refresh_policy, "MAX_CACHE_ENTRIES", 2)
    first = tmp_path / "first.cache.gz"
    second = tmp_path / "second.cache.gz"

    assert cache_handover.merge(first, handover, "pypi")
    assert cache_handover.merge(second, handover, "pypi")

    checks, cursor, found = read_cache(first)
    assert found and cursor == 9 and set(checks) == {"new-a", "new-b"}
    assert first.read_bytes() == second.read_bytes()


def test_takeover_uses_handover_checks_instead_of_treating_the_superset_as_due(tmp_path):
    names = [f"package-{index}" for index in range(8)]
    base = tmp_path / "actions.cache.gz"
    handover = tmp_path / "booster.cache.gz"
    write_cache(base, {name: pack(99, 1, "1", "n") for name in names[:3]}, 3)
    write_cache(handover, {name: pack(99, 1, "1", "n") for name in names}, 8)

    before, _, _ = read_cache(base)
    assert sum(is_due(before, name, 100, 14) for name in names) == 5
    assert cache_handover.merge(base, handover, "pypi")
    after, _, _ = read_cache(base)
    assert sum(is_due(after, name, 100, 14) for name in names) == 0


def test_publish_decision_covers_throttle_force_size_revision_and_equal_bytes(tmp_path):
    snapshot = tmp_path / "snapshot.cache.gz"
    target = tmp_path / "pypi.cache.gz"
    write_cache(snapshot, {"a": pack(10, 1, "1", "n")}, 5)
    now = 2_000_000

    assert cache_handover.should_publish(snapshot, target, None, now, False) == (True, "missing")
    target.write_bytes(snapshot.read_bytes())
    assert cache_handover.should_publish(snapshot, target, now - 25 * 3600, now, False) == (False, "unchanged")
    write_cache(snapshot, {"a": pack(11, 1, "2", "c")}, 6)
    assert cache_handover.should_publish(snapshot, target, now - 23 * 3600, now, False) == (False, "fresh")
    assert cache_handover.should_publish(snapshot, target, now - 25 * 3600, now, False) == (True, "stale")
    assert cache_handover.should_publish(snapshot, target, now - 1, now, True) == (True, "forced")

    write_cache(snapshot, {"wrong": pack(12, 1, "1", "n")}, 1,
                revision=refresh_policy.EXTRACTION_REVISION + 1)
    assert cache_handover.should_publish(snapshot, target, 0, now, True) == (False, "cold")
    with snapshot.open("wb") as stream:
        stream.truncate(cache_handover.MAX_HANDOVER_BYTES + 1)
    assert cache_handover.should_publish(snapshot, target, 0, now, True) == (False, "too-large")


def test_stage_handover_copies_the_frozen_snapshot_byte_for_byte(tmp_path, capsys):
    snapshot = tmp_path / "snapshot.cache.gz"
    target = tmp_path / "handover/pypi.cache.gz"
    write_cache(snapshot, {"a": pack(10, 1, "1", "n")}, 5)

    assert cache_handover.stage(snapshot, target, "pypi", None, 100, False)
    assert target.read_bytes() == snapshot.read_bytes()
    assert "published" in capsys.readouterr().out


def test_tool_is_called_by_booster_publication_and_ci_only_reads_artifact_data():
    parallel = (ROOT / "tools/crawl_parallel.sh").read_text()
    supervisor = (ROOT / "tools/local_booster.sh").read_text()
    workflow = (ROOT / ".github/workflows/registry-refresh.yml").read_text()
    assert 'tools/cache_handover.py" stage' in parallel
    assert 'log -1 --format=%ct -- "${handover_path}"' in parallel
    assert "HANDOVER_FORCE" in parallel
    assert "HANDOVER_FORCE=1 publish_now" in supervisor
    assert supervisor.count('lease_failure_text "${lease_status}"') == 3
    assert "git cat-file -e" in workflow
    assert "git show \"origin/artifact-data:${handover_path}\"" in workflow


def test_merge_cli_never_fails_when_the_handover_is_absent(tmp_path):
    base = tmp_path / "base.cache.gz"
    write_cache(base, {"ci": pack(10, 1, "1", "c")}, 4)
    before = base.read_bytes()
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/cache_handover.py"), "merge", "--base", str(base),
         "--handover", str(tmp_path / "missing"), "--source", "pypi"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0 and base.read_bytes() == before
    assert "missing or cold" in result.stdout
