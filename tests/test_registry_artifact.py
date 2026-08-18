from io import BytesIO
import gzip
import json
import tarfile
import zipfile

import global_executables.registry_artifact as registry_artifact
from global_executables.registry_artifact import (_go_rows, _packagist_packages, _packagist_rows,
                                                   _crawl_pypi, _failure_state, _pypi_projects,
                                                   _ruby_gem_rows, _rubygems_names, _sdist_rows,
                                                   _wheel_rows)


def test_pypi_simple_catalog_is_normalized_and_sorted():
    body = b'<a href="/simple/zeta/">zeta</a>\n<a href="/simple/Alpha/">Alpha</a>\n'
    assert _pypi_projects(body) == ["Alpha", "zeta"]


def test_wheel_console_scripts_are_artifact_evidence():
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("demo-1.0.dist-info/entry_points.txt", "[console_scripts]\ndemo = demo:main\n")
    rows = _wheel_rows(stream.getvalue(), "demo", "1.0", "https://example.test", "https://example.test/demo.whl")
    assert rows[0]["command"] == "demo"
    assert rows[0]["confidence"] == "direct"
    assert rows[0]["registry"] == "pypi"


def test_pypi_sdist_project_scripts_are_artifact_evidence():
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        body = b"[project]\nname = 'demo'\n[project.scripts]\ndemo = 'demo:main'\n"
        info = tarfile.TarInfo("demo-1.0/pyproject.toml")
        info.size = len(body)
        archive.addfile(info, BytesIO(body))
    rows = _sdist_rows(stream.getvalue(), "demo", "1.0", "https://example.test", "https://example.test/demo.tar.gz")
    assert rows[0]["command"] == "demo"
    assert rows[0]["registry"] == "pypi"


def test_go_main_packages_are_artifact_evidence():
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("example.com/demo@v1.0.0/cmd/demo/main.go", "package main\nfunc main() {}\n")
        archive.writestr("example.com/demo@v1.0.0/internal/tool/main.go", "package main\nfunc main() {}\n")
        archive.writestr("example.com/demo@v1.0.0/cmd/demo/main_test.go", "package main\n")
    rows = _go_rows(stream.getvalue(), "example.com/demo", "v1.0.0", "https://proxy.golang.org/demo.zip")
    assert [row["command"] for row in rows] == ["demo", "tool"]
    assert all(row["language"] == "go" and row["registry"] == "go" for row in rows)


def test_rubygems_catalog_and_gem_metadata_are_artifact_evidence():
    assert _rubygems_names(b"---\nalpha\nBeta Gem\n") == ["alpha"]
    metadata = b"---\nname: demo\nversion: 1.0.0\nexecutables:\n- demo\n- demo-admin\n"
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        payload = gzip.compress(metadata)
        info = tarfile.TarInfo("metadata.gz"); info.size = len(payload)
        archive.addfile(info, BytesIO(payload))
    rows = _ruby_gem_rows(stream.getvalue(), "demo", "1.0.0", "https://example.test", "https://example.test/demo.gem")
    assert [row["command"] for row in rows] == ["demo", "demo-admin"]
    assert rows[0]["language"] == "ruby" and rows[0]["registry"] == "rubygems"


def test_packagist_only_emits_composer_bin_declarations():
    body = json.dumps({
        "packages": {
            "demo/tool": [{
                "name": "demo/tool", "version": "1.0.0",
                "bin": ["bin/demo", "tools/admin"],
                "source": {"url": "https://example.test/demo/tool"},
            }],
            "demo/library": [{"name": "demo/library", "version": "1.0.0"}],
        }
    }).encode()
    assert _packagist_packages(b'{"packageNames":["demo/tool","demo/library"]}') == ["demo/library", "demo/tool"]
    rows = _packagist_rows(json.loads(body), "demo/tool", "https://repo.packagist.org/p2/demo/tool.json")
    assert [row["command"] for row in rows] == ["admin", "demo"]
    assert all(row["language"] == "php" and row["registry"] == "packagist" for row in rows)
    assert _packagist_rows(json.loads(body), "demo/library", "https://example.test") == []


def test_permanent_registry_conditions_are_not_retryable_failures():
    state = {
        "failures": {
            "no-dist": "latest release has no wheel or sdist",
            "yanked": "crate has no non-yanked version: yanked",
            "transient": "HTTP Error 503: first byte timeout",
        },
        "retry_projects": ["no-dist", "old-source"],
        "retry_crates": ["yanked", "transient"],
    }
    failures, unavailable = _failure_state(state)
    assert set(unavailable) == {"no-dist", "yanked"}
    assert set(failures) == {"transient"}
    assert state["retry_projects"] == ["old-source"]


def test_pypi_no_distribution_advances_cursor_and_is_unavailable(tmp_path, monkeypatch):
    catalog = tmp_path / "projects.txt"
    catalog.write_text("empty-one\nempty-two\n")

    def fake_fetch(url, timeout=120):
        project = url.rsplit("/", 1)[-1]
        return json.dumps({"info": {"name": project, "version": "1.0.0"}, "urls": []}).encode(), {}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    state = {"projects_file": str(catalog)}
    report = _crawl_pypi(state, tmp_path / "pypi.jsonl", 2, 1024, 120)
    assert report["cursor"] == 2
    assert report["processed"] == 2
    assert report["failures"] == 0
    assert report["unavailable"] == 2


def test_crawl_marks_source_failures_as_failed(tmp_path, monkeypatch):
    def failed_source(*args):
        return {"failures": 1, "coverage_kind": "partial"}

    monkeypatch.setattr(registry_artifact, "_crawl_pypi", failed_source)
    report = registry_artifact.crawl_registry_sources(
        ["pypi"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json"
    )

    assert report["status"] == "failed"
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"
