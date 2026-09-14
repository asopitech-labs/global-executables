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


def _repository_tarball(members: dict[str, bytes]) -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, BytesIO(payload))
    return stream.getvalue()


def test_vcpkg_snapshot_records_declared_tools_and_stays_partial():
    from global_executables.production import _crawl_vcpkg

    body = _repository_tarball({
        "vcpkg-master/ports/toolkit/portfile.cmake":
            b"vcpkg_copy_tools(TOOL_NAMES toolkit-run AUTO_CLEAN)\n",
        "vcpkg-master/ports/toolkit/vcpkg.json": b'{"name": "toolkit", "version": "1.4.0"}',
        "vcpkg-master/ports/headers/portfile.cmake": b"vcpkg_cmake_install()\n",
        "vcpkg-master/ports/headers/vcpkg.json": b'{"name": "headers", "version-string": "2.0.0"}',
    })
    rows, coverage = _crawl_vcpkg(body, "test://vcpkg")
    assert [(row["command"], row["package"], row["version"]) for row in rows] == [
        ("toolkit-run", "toolkit", "1.4.0")]
    assert coverage["packages"] == 2 and coverage["declaring_packages"] == 1
    # A port that installs a command without calling vcpkg_copy_tools names nothing,
    # so reading every port still cannot license a negative answer.
    assert coverage["coverage_kind"] == "partial"


def test_xmake_snapshot_records_binary_packages_only():
    from global_executables.production import _crawl_xmake

    body = _repository_tarball({
        "xmake-repo-master/packages/d/demotool/xmake.lua":
            b'package("demotool")\n    set_kind("binary")\n    add_versions("1.3.1", "aa")\n',
        "xmake-repo-master/packages/d/demolib/xmake.lua":
            b'package("demolib")\n    set_kind("library")\n',
    })
    rows, coverage = _crawl_xmake(body, "test://xmake")
    assert [(row["command"], row["confidence"]) for row in rows] == [("demotool", "inferred")]
    assert coverage["packages"] == 2 and coverage["coverage_kind"] == "partial"


def test_cpp_sources_never_claim_an_exhaustive_report(tmp_path, monkeypatch):
    from global_executables import production

    body = _repository_tarball({
        "vcpkg-master/ports/toolkit/portfile.cmake":
            b"vcpkg_copy_tools(TOOL_NAMES toolkit-run AUTO_CLEAN)\n",
        "vcpkg-master/ports/toolkit/vcpkg.json": b'{"name": "toolkit", "version": "1.4.0"}',
    })
    monkeypatch.setattr(production, "fetch", lambda url, timeout=300: (body, {"downloaded_bytes": len(body)}))
    report = production.crawl_sources(["vcpkg"], tmp_path / "intermediate", tmp_path / "report.json")
    assert report["status"] == "success"
    assert report["coverage_kind"] == "partial"
    assert report["sources"]["vcpkg"]["coverage_kind"] == "partial"
