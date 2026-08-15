import json
import tarfile
from io import BytesIO

from global_executables.production import _arch_text, _crawl_homebrew, _crawl_os


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
    from global_executables.production import crawl_sources

    report = crawl_sources(["npm"], tmp_path / "intermediate", tmp_path / "report.json")
    assert report["status"] == "failed"
    assert report["sources"]["npm"]["status"] == "failed"
    assert "artifact" in report["sources"]["npm"]["error"]


def test_homebrew_formula_catalog_uses_declared_executable_inventory():
    rows, coverage = _crawl_homebrew(json.dumps([{"name": "demo", "versions": {"stable": "1.0"}, "homepage": "https://example.test", "executables": ["demo"]}]).encode(), "test://homebrew")
    assert rows[0]["command"] == "demo"
    assert coverage["coverage_kind"] == "exhaustive"
