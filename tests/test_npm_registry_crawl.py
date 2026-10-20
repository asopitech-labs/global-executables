import importlib.util
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from global_executables.registry_state import load_state, save_state


SCRIPT = Path(__file__).parents[1] / "tools/npm_registry_crawl.py"
spec = importlib.util.spec_from_file_location("npm_registry_crawl", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_broad_npm_cursor_and_rows_do_not_change_critical_state(tmp_path, monkeypatch):
    catalog = tmp_path / "npm-packages.txt"
    catalog.write_text("a\nb\nc\n")
    manifest = tmp_path / "npm-packages.txt.manifest.json"
    manifest.write_text(json.dumps({
        "catalog_complete": True, "catalog_source": module.CATALOG_SOURCE,
        "catalog_digest": "sha256:" + hashlib.sha256(catalog.read_bytes()).hexdigest(), "catalog_size": 3,
        "catalog_snapshot": "2026-10-19", "catalog_scope": "npm registry package-name index",
    }))
    state = tmp_path / "registry-state"
    save_state(state, {"version": 1, "sources": {"npm": {"cursor": 2295, "catalog_size": 2295}}})
    rows = tmp_path / "npm-registry.jsonl"
    report = tmp_path / "report.json"
    inspected = []

    def fake_inspect(name):
        inspected.append(name)
        return [{"package": name, "command": name}]

    monkeypatch.setattr(module, "inspect", fake_inspect)
    module.crawl(catalog, manifest, state, rows, report, 2, interval=0)
    module.crawl(catalog, manifest, state, rows, report, 2, interval=0)
    result = load_state(state)["sources"]
    assert result["npm"] == {"cursor": 2295, "catalog_size": 2295}
    assert result["npm-registry"]["cursor"] == 3
    assert inspected == ["a", "b", "c"]
    assert [json.loads(line)["package"] for line in rows.read_text().splitlines()] == ["a", "b", "c"]


def test_observation_merge_replaces_one_package_without_loading_old_rows(tmp_path):
    path = tmp_path / "npm-registry.jsonl"
    path.write_text("".join(json.dumps({"package": package, "command": command}) + "\n"
                            for package, command in [("a", "old-a"), ("b", "old-b1"),
                                                     ("b", "old-b2"), ("d", "old-d")]))
    assert module.write_rows(path, {"b": [{"package": "b", "command": "new-b"}],
                                    "c": [{"package": "c", "command": "new-c"}]}) == 4
    assert [(row["package"], row["command"]) for row in map(json.loads, path.read_text().splitlines())] == [
        ("a", "old-a"), ("b", "new-b"), ("c", "new-c"), ("d", "old-d")]
    assert module.write_rows(path, {"a": [{"package": "a", "command": "new-a"}], "d": []}) == 3
    assert [(row["package"], row["command"]) for row in map(json.loads, path.read_text().splitlines())] == [
        ("a", "new-a"), ("b", "new-b"), ("c", "new-c")]


def test_withdrawn_retry_removes_stale_observation(tmp_path, monkeypatch):
    catalog = tmp_path / "npm-packages.txt"
    catalog.write_text("a\n")
    manifest = tmp_path / "npm-packages.txt.manifest.json"
    manifest.write_text(json.dumps({
        "catalog_complete": True, "catalog_source": module.CATALOG_SOURCE,
        "catalog_digest": "sha256:" + hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "catalog_size": 1, "catalog_snapshot": "2026-10-19",
        "catalog_scope": "npm registry package-name index",
    }))
    state = tmp_path / "registry-state"
    save_state(state, {"version": 1, "sources": {"npm-registry": {
        "cursor": 1, "failures": {"a": "HTTP 503"},
    }}})
    rows = tmp_path / "npm-registry.jsonl"
    rows.write_text(json.dumps({"package": "a", "command": "stale"}) + "\n")

    def withdrawn(name):
        raise HTTPError("https://registry.npmjs.org/a/latest", 404, "gone", {}, None)

    monkeypatch.setattr(module, "inspect", withdrawn)
    module.crawl(catalog, manifest, state, rows, tmp_path / "report.json", 10, interval=0)
    assert rows.read_text() == ""
    assert load_state(state)["sources"]["npm-registry"]["unavailable"] == {"a": "HTTP 404"}


def test_appended_catalog_keeps_old_cursor_position(tmp_path, monkeypatch):
    catalog = tmp_path / "npm-packages.txt"
    catalog.write_text("a\nb\naa\nc\n")
    old_digest = "sha256:" + hashlib.sha256(b"a\nb\n").hexdigest()
    manifest = tmp_path / "npm-packages.txt.manifest.json"
    manifest.write_text(json.dumps({
        "catalog_complete": True, "catalog_source": module.CATALOG_SOURCE,
        "catalog_digest": "sha256:" + hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "catalog_size": 4, "catalog_snapshot": "2026-10-20",
        "catalog_scope": "npm registry package-name index",
    }))
    state = tmp_path / "registry-state"
    save_state(state, {"version": 1, "sources": {"npm-registry": {
        "cursor": 1, "catalog_size": 2, "catalog_digest": old_digest,
    }}})
    checked = []
    monkeypatch.setattr(module, "inspect", lambda name: checked.append(name) or [])
    module.crawl(catalog, manifest, state, tmp_path / "rows.jsonl", tmp_path / "report.json", 2, interval=0)
    assert checked == ["b", "aa"]
    updated = load_state(state)["sources"]["npm-registry"]
    assert updated["cursor"] == 3
    assert updated["catalog_size"] == 4


def test_changed_catalog_prefix_refuses_to_reuse_cursor(tmp_path):
    catalog = tmp_path / "npm-packages.txt"
    catalog.write_text("different\nb\nc\n")
    manifest = tmp_path / "npm-packages.txt.manifest.json"
    manifest.write_text(json.dumps({
        "catalog_complete": True, "catalog_source": module.CATALOG_SOURCE,
        "catalog_digest": "sha256:" + hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "catalog_size": 3, "catalog_snapshot": "2026-10-20",
        "catalog_scope": "npm registry package-name index",
    }))
    state = tmp_path / "registry-state"
    save_state(state, {"version": 1, "sources": {"npm-registry": {
        "cursor": 1, "catalog_size": 2,
        "catalog_digest": "sha256:" + hashlib.sha256(b"a\nb\n").hexdigest(),
    }}})
    with pytest.raises(ValueError, match="preserving the old prefix"):
        module.crawl(catalog, manifest, state, tmp_path / "rows.jsonl", tmp_path / "report.json", 1, interval=0)
