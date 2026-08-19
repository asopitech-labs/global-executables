from io import BytesIO
import gzip
import json
import tarfile
import urllib.error
import urllib.parse
import zipfile

import global_executables.registry_artifact as registry_artifact
from global_executables.registry_artifact import (_crates_catalog_page, _crawl_crates, _go_rows,
                                                   _packagist_packages, _packagist_rows,
                                                   _crawl_pypi, _failure_state, _pypi_projects,
                                                   _ruby_gem_rows, _rubygems_names, _sdist_rows,
                                                   _wheel_rows)


def _crate_archive(name, version, binary="demo"):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        manifest = f'[package]\nname = "{name}"\nversion = "{version}"\n\n[[bin]]\nname = "{binary}"\n'
        info = tarfile.TarInfo(f"{name}-{version}/Cargo.toml")
        info.size = len(manifest.encode())
        archive.addfile(info, BytesIO(manifest.encode()))
    return stream.getvalue()


def _fake_crates_registry(catalog, downloads=None):
    """Serve crates.io seek pagination from an in-memory alphabetical catalog."""
    requests = []

    def fake_fetch(url, timeout=120):
        requests.append(url)
        if url.startswith("https://crates.io/api/v1/crates?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = int(query.get("seek", ["0"])[0])
            size = int(query["per_page"][0])
            window = catalog[start:start + size]
            end = start + len(window)
            meta = {"total": len(catalog)}
            if end < len(catalog):
                meta["next_page"] = f"?per_page={size}&sort=alpha&seek={end}"
            return json.dumps({"crates": window, "meta": meta}).encode(), {"downloaded_bytes": 0}
        name = url.rsplit("/", 3)[1]
        if downloads is not None and name in downloads:
            raise downloads[name]
        return _crate_archive(name, "1.0.0"), {"downloaded_bytes": 1}

    return fake_fetch, requests


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


def test_crates_catalog_reads_the_seek_cursor_not_a_page_number(monkeypatch):
    catalog = [{"name": f"crate-{index:03d}", "max_stable_version": "1.0.0"} for index in range(250)]
    fake_fetch, requests = _fake_crates_registry(catalog)
    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)

    crates, next_seek, total = _crates_catalog_page(None, 120)
    assert [crate["name"] for crate in crates][:1] == ["crate-000"]
    assert next_seek == "100" and total == 250
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0]).query)
    assert "page" not in query and query["sort"] == ["alpha"]

    crates, next_seek, _ = _crates_catalog_page(next_seek, 120)
    assert crates[0]["name"] == "crate-100" and next_seek == "200"
    assert _crates_catalog_page("200", 120)[1] is None


def test_crates_seek_cursor_resumes_mid_page_without_reprocessing(tmp_path, monkeypatch):
    catalog = [{"name": f"crate-{index:03d}", "max_stable_version": "1.0.0"} for index in range(250)]
    fake_fetch, _ = _fake_crates_registry(catalog)
    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    output = tmp_path / "crates.jsonl"
    state = {}

    first = _crawl_crates(state, output, 30, 1_000_000, 120)
    assert first["cursor"] == 30 and first["records"] == 30
    assert state["seek"] is None and state["page_offset"] == 30

    second = _crawl_crates(state, output, 30, 1_000_000, 120)
    assert second["cursor"] == 60 and state["page_offset"] == 60 and state["seek"] is None
    names = [json.loads(line)["package"] for line in output.read_text().splitlines()]
    assert names == [f"crate-{index:03d}" for index in range(60)]
    assert len(names) == len(set(names))


def test_crates_reaches_records_beyond_the_offset_pagination_cap(tmp_path, monkeypatch):
    catalog = [{"name": f"crate-{index:05d}", "max_stable_version": "1.0.0"} for index in range(20_150)]
    fake_fetch, _ = _fake_crates_registry(catalog)
    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    state = {"page": 201}  # a legacy cursor crates.io answers with HTTP 400

    report = _crawl_crates(state, tmp_path / "crates.jsonl", 50, 1_000_000, 120)

    assert "page" not in state and report["skip_remaining"] == 0
    assert report["cursor"] == 20_050 and report["catalog_size"] == 20_150
    packages = [json.loads(line)["package"] for line in (tmp_path / "crates.jsonl").read_text().splitlines()]
    assert packages[0] == "crate-20000"  # the first crate offset pagination could never reach


def test_crates_catalog_outage_keeps_progress_and_fails_the_run(tmp_path, monkeypatch):
    catalog = [{"name": f"crate-{index:03d}", "max_stable_version": "1.0.0"} for index in range(250)]
    fake_fetch, _ = _fake_crates_registry(catalog)
    calls = {"count": 0}

    def flaky_fetch(url, timeout=120):
        if url.startswith("https://crates.io/api/v1/crates?"):
            calls["count"] += 1
            if calls["count"] > 1:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return fake_fetch(url, timeout)

    monkeypatch.setattr(registry_artifact, "fetch", flaky_fetch)
    output = tmp_path / "crates.jsonl"
    state = {}

    report = _crawl_crates(state, output, 200, 1_000_000, 120)

    assert report["error"].startswith("HTTP Error 503")
    assert report["complete"] is False and report["coverage_kind"] == "partial"
    assert report["cursor"] == 100 and state["cursor"] == 100 and state["seek"] == "100"
    assert len(output.read_text().splitlines()) == 100


def test_crates_yanked_crates_are_unavailable_and_never_downloaded(tmp_path, monkeypatch):
    catalog = [{"name": "live", "max_stable_version": "1.0.0", "yanked": False},
               {"name": "gone", "max_stable_version": None, "newest_version": "0.0.0", "yanked": True}]
    fake_fetch, requests = _fake_crates_registry(catalog)
    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    state = {}

    report = _crawl_crates(state, tmp_path / "crates.jsonl", 2, 1_000_000, 120)

    assert report["processed"] == 2 and report["cursor"] == 2
    assert report["failures"] == 0 and report["unavailable"] == 1
    assert state["unavailable"]["gone"] == "all versions are yanked"
    assert not any("/gone/" in url for url in requests)  # 0.0.0 has no artifact to fetch


def test_source_package_budget_overrides_the_shared_budget(tmp_path, monkeypatch):
    seen = {}

    def spy(name):
        def runner(state, output, budget, byte_budget, timeout):
            seen[name] = budget
            return {"coverage_kind": "partial", "complete": False}
        return runner

    monkeypatch.setattr(registry_artifact, "_crawl_crates", spy("crates"))
    monkeypatch.setattr(registry_artifact, "_crawl_npm", spy("npm"))
    report = registry_artifact.crawl_registry_sources(
        ["npm", "crates"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json",
        package_budget=1000, source_budgets={"crates": 10_000},
    )

    assert seen == {"npm": 1000, "crates": 10_000}
    assert report["sources"]["crates"]["package_budget"] == 10_000


def test_crawl_marks_source_failures_as_failed(tmp_path, monkeypatch):
    def failed_source(*args):
        return {"failures": 1, "coverage_kind": "partial"}

    monkeypatch.setattr(registry_artifact, "_crawl_pypi", failed_source)
    report = registry_artifact.crawl_registry_sources(
        ["pypi"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json"
    )

    assert report["status"] == "failed"
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"
