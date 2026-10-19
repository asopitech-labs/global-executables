import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "tools/npm_full_catalog.py"
spec = importlib.util.spec_from_file_location("npm_full_catalog", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_catalog_resumes_without_repeating_the_checkpoint(tmp_path, monkeypatch):
    output = tmp_path / "npm-packages.txt"
    checkpoint = tmp_path / "npm-packages.cursor"
    calls = []

    def page(start, limit):
        calls.append(start)
        return {None: ["a", "b"], "b": ["c"], "c": []}[start]

    monkeypatch.setattr(module, "fetch_page", page)
    assert module.build_catalog(output, checkpoint, page_size=2) == 3
    assert output.read_text() == "a\nb\nc\n"
    assert not checkpoint.exists()
    assert calls == [None, "b", "c"]
    assert module.build_catalog(output, checkpoint, page_size=2) == 0
    assert calls == [None, "b", "c"]


def test_catalog_refuses_mismatched_resume_checkpoint(tmp_path):
    output = tmp_path / "npm-packages.txt"
    checkpoint = tmp_path / "npm-packages.cursor"
    output.write_text("a\n")
    checkpoint.write_text('{"last_id":"b","bytes":10}')
    with pytest.raises(RuntimeError, match="exceeds"):
        module.build_catalog(output, checkpoint)


def test_catalog_truncates_uncommitted_page_and_excludes_internal_ids(tmp_path, monkeypatch):
    output = tmp_path / "npm-packages.txt"
    checkpoint = tmp_path / "npm-packages.cursor"
    output.write_text("a\nnever-committed\n")
    checkpoint.write_text('{"last_id":"a","bytes":2}')
    monkeypatch.setattr(module, "fetch_page", lambda start, limit: ["b", "_design/index"])
    assert module.build_catalog(output, checkpoint, page_size=10) == 1
    assert output.read_text() == "a\nb\n"
    assert __import__("json").loads(output.with_suffix(".txt.manifest.json").read_text())["catalog_size"] == 2
