import json
import tarfile
from io import BytesIO

import pytest

from global_executables.production import (ACCUMULATING_SOURCES, COLLECTED_SOURCES,
                                           ProductionSourceError, _arch_text,
                                           _crawl_homebrew, _crawl_os,
                                           _merge_observations, crawl_sources)


def test_arch_archive_is_normalized_to_exhaustive_records():
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        payload = b"usr/bin/demo\nusr/share/doc/demo/readme\n"
        info = tarfile.TarInfo("demo-1/files")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))
    rows, coverage = _crawl_os("arch", stream.getvalue(), "test://arch")
    assert [row["command"] for row in rows] == ["demo"]
    assert coverage["coverage_kind"] == "exhaustive"


def test_production_source_report_keeps_artifact_sources_failed(tmp_path):
    report = crawl_sources(["npm"], tmp_path / "intermediate", tmp_path / "report.json")
    assert report["status"] == "failed"
    assert report["sources"]["npm"]["status"] == "failed"
    assert "artifact" in report["sources"]["npm"]["error"]


def test_failed_collection_uses_a_stored_observation_without_erasing_it(tmp_path, monkeypatch):
    from global_executables import production

    output = tmp_path / "intermediate" / "debian.jsonl"
    output.parent.mkdir()
    stored = {"command": "kept", "ecosystem": "debian", "package": "kept",
              "source": "stored", "confidence": "filesystem"}
    output.write_text(json.dumps(stored) + "\n")

    def fail(*args, **kwargs):
        raise ProductionSourceError("upstream unavailable")

    monkeypatch.setattr(production, "crawl_source", fail)
    report = crawl_sources(["debian"], output.parent, tmp_path / "report.json")

    assert report["status"] == "degraded"
    assert report["failed"] == [] and report["fallbacks"] == ["debian"]
    assert report["sources"]["debian"]["status"] == "fallback"
    assert report["sources"]["debian"]["records"] == 1
    assert json.loads(output.read_text()) == stored


def test_every_locally_collected_source_accumulates_durable_observations(tmp_path):
    assert ACCUMULATING_SOURCES == COLLECTED_SOURCES
    output = tmp_path / "debian.jsonl"
    old = {"command": "old-name", "ecosystem": "debian", "package": "old-package",
           "source": "debian-index", "confidence": "filesystem"}
    shared_old = {"command": "shared", "ecosystem": "debian", "package": "old-package",
                  "version": "1", "source": "debian-index", "confidence": "filesystem"}
    output.write_text(json.dumps(old) + "\n" + json.dumps(shared_old) + "\n")
    shared_new = {**shared_old, "version": "2"}

    merged = _merge_observations([shared_new], output)

    assert old in merged
    assert shared_new in merged
    assert shared_old not in merged


def test_malformed_stored_observation_is_not_silently_discarded(tmp_path):
    output = tmp_path / "debian.jsonl"
    output.write_text('{not json}\n')

    with pytest.raises(json.JSONDecodeError):
        _merge_observations([], output)


def test_homebrew_formula_catalog_uses_declared_executable_inventory():
    rows, coverage = _crawl_homebrew(json.dumps([{"name": "demo", "versions": {"stable": "1.0"}, "homepage": "https://example.test", "executables": ["demo"]}]).encode(), "test://homebrew")
    assert rows[0]["command"] == "demo"
    assert coverage["coverage_kind"] == "exhaustive"
