from io import BytesIO
import gzip
import json
import tarfile
import urllib.error
import urllib.parse
import zipfile

import pytest

import global_executables.registry_artifact as registry_artifact
from global_executables.registry_artifact import (_console_scripts, _go_rows, _gem_rows,
                                                   _packagist_packages, _packagist_rows,
                                                   _crawl_pypi, _failure_state, _postgres_array,
                                                   _pypi_projects, _ruby_gem_rows, _rubygems_names,
                                                   _sdist_rows, _wheel_commands_from, _wheel_rows)


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


def test_crates_binaries_come_from_the_dump_not_from_downloads(tmp_path, monkeypatch):
    """crates.io states bin_names per version, so no .crate is ever fetched."""
    dump = BytesIO()
    files = {
        "2026-08-19/metadata.json": b'{"timestamp": "2026-08-19T02:00:27Z"}',
        "2026-08-19/data/crates.csv": b"id,name,repository\n1,ripgrep,https://example.test/rg\n2,serde,\n",
        "2026-08-19/data/default_versions.csv": b"crate_id,version_id\n1,11\n2,22\n",
        "2026-08-19/data/versions.csv": (b"id,crate_id,num,yanked,bin_names\n"
                                         b'11,1,15.2.0,f,"{rg}"\n'
                                         b'22,2,1.0.229,f,{}\n'
                                         b'33,1,0.1.0,t,"{old-rg}"\n'),
    }
    with tarfile.open(fileobj=dump, mode="w:gz") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name); info.size = len(body)
            archive.addfile(info, BytesIO(body))
    payload = dump.getvalue()

    class _Response:
        def __init__(self): self._stream = BytesIO(payload)
        def read(self, size=-1): return self._stream.read(size)
        def __enter__(self): return self
        def __exit__(self, *args): return False

    requested = []
    monkeypatch.setattr(registry_artifact, "content_length", lambda url, timeout=120: len(payload))
    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen",
                        lambda request, timeout=None: requested.append(request.full_url) or _Response())
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda *a, **k: pytest.fail("the dump must not trigger artifact downloads"))
    output = tmp_path / "crates.jsonl"
    state = {"page": 194, "seek": "abc", "cursor": 19300}

    report = registry_artifact._crawl_crates(state, output, 10, 10_000_000, 120)

    assert report["coverage_kind"] == "exhaustive" and report["complete"] is True
    assert report["catalog_size"] == 2 and report["crates_with_binaries"] == 1
    assert requested == [registry_artifact.CRATES_DB_DUMP]
    assert "page" not in state and "seek" not in state  # the retired cursor is dropped
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [(row["command"], row["package"], row["version"]) for row in rows] == [("rg", "ripgrep", "15.2.0")]


def test_postgres_array_literals_from_the_dump():
    assert _postgres_array("{rg}") == ["rg"]
    assert _postgres_array("{}") == []
    assert _postgres_array('{"cargo-add","cargo-rm"}') == ["cargo-add", "cargo-rm"]
    assert _postgres_array('{"with,comma"}') == ["with,comma"]
    assert _postgres_array("") == []


def test_wheel_commands_cover_data_scripts_as_well_as_entry_points():
    """A wheel shipping a prebuilt binary declares it as a data script."""
    members = {
        "demo-1.0.dist-info/entry_points.txt": b"[console_scripts]\ndemo = demo:main\n",
        "demo-1.0.data/scripts/demo-bin": b"#!/bin/sh\n",
        "demo/__init__.py": b"",
    }
    commands = _wheel_commands_from(list(members), members.__getitem__)
    assert sorted(commands) == ["demo", "demo-bin"]
    assert _console_scripts("[console_scripts]\na = x\n[gui_scripts]\nb = y\n") == ["a"]


def test_gem_executables_are_read_from_the_head_of_the_archive(monkeypatch):
    metadata = b"---\nname: demo\nversion: 1.0.0\nexecutables:\n- demo\n"
    gem = BytesIO()
    with tarfile.open(fileobj=gem, mode="w") as archive:
        payload = gzip.compress(metadata)
        info = tarfile.TarInfo("metadata.gz"); info.size = len(payload)
        archive.addfile(info, BytesIO(payload))
        filler = b"x" * 500_000
        info = tarfile.TarInfo("data.tar.gz"); info.size = len(filler)
        archive.addfile(info, BytesIO(filler))
    body = gem.getvalue()

    monkeypatch.setattr(registry_artifact, "content_length", lambda url, timeout=120: len(body))
    monkeypatch.setattr(registry_artifact, "fetch_range",
                        lambda url, start, end, timeout=120: (body[start:end + 1], {"downloaded_bytes": end - start + 1}))
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda *a, **k: pytest.fail("the head read must cover the gemspec"))

    blob, spent = registry_artifact._gem_metadata("https://example.test/demo.gem", 120)
    assert spent == registry_artifact.GEM_HEAD_BYTES and spent < len(body)
    rows = _gem_rows(gzip.decompress(blob).decode(), "demo", "1.0.0", None, "https://example.test/demo.gem")
    assert [row["command"] for row in rows] == ["demo"]


def test_go_catalog_records_each_module_once(tmp_path, monkeypatch):
    """The index republishes a module per version; the catalog keeps one entry."""
    pages = [
        b'{"Path":"example.com/a","Version":"v1.0.0","Timestamp":"2019-01-02T00:00:00Z"}\n'
        b'{"Path":"example.com/a","Version":"v1.1.0","Timestamp":"2019-01-03T00:00:00Z"}\n'
        b'{"Path":"example.com/b","Version":"v1.0.0","Timestamp":"2019-01-04T00:00:00Z"}\n',
    ]

    def fake_fetch(url, timeout=120, attempts=4):
        if url.startswith(registry_artifact.GO_INDEX):
            return (pages.pop(0) if pages else b""), {"downloaded_bytes": 10}
        if url.endswith("/@latest"):
            return b'{"Version":"v1.1.0"}', {"downloaded_bytes": 20}
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_go_module_rows",
                        lambda url, module, version, timeout: ([], 5))
    catalog = tmp_path / "go-modules.txt"
    state = {"modules_file": str(catalog), "since": "2019-06-18T00:00:00Z"}

    report = registry_artifact._crawl_go(state, tmp_path / "go.jsonl", 10, 1_000_000, 120)

    assert catalog.read_text().split() == ["example.com/a", "example.com/b"]
    assert report["catalog_size"] == 2 and report["discovered"] == 2
    assert report["processed"] == 2 and report["catalog_complete"] is True
    assert "since" not in state  # the download cursor is not a catalog cursor


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


def test_crates_api_requests_are_paced_but_other_hosts_are_not(monkeypatch):
    slept = []
    monkeypatch.setattr(registry_artifact.time, "sleep", slept.append)
    monkeypatch.setattr(registry_artifact, "_last_request", {})

    registry_artifact._throttle("https://crates.io/api/v1/crates?per_page=100")
    assert slept == []  # the first request never waits
    registry_artifact._throttle("https://crates.io/api/v1/crates/demo/1.0.0/download")
    assert len(slept) == 1 and 0 < slept[0] <= 1.0

    registry_artifact._throttle("https://static.crates.io/crates/demo/demo-1.0.0.crate")
    registry_artifact._throttle("https://pypi.org/simple/")
    assert len(slept) == 1  # CDN mirrors and other registries keep their own pace


def test_fetch_backs_off_on_rate_limiting_instead_of_failing(monkeypatch):
    monkeypatch.setattr(registry_artifact, "_last_request", {})
    monkeypatch.setattr(registry_artifact.time, "sleep", lambda seconds: None)
    responses = [urllib.error.HTTPError("https://crates.io/x", 429, "Too Many Requests",
                                        {"Retry-After": "7"}, None)]

    class _Response:
        status = 200
        def read(self): return b"ok"
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_urlopen(request, timeout=None):
        if responses:
            raise responses.pop(0)
        return _Response()

    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen", fake_urlopen)
    body, transfer = registry_artifact.fetch("https://crates.io/x", 120)

    assert body == b"ok" and transfer["status_code"] == 200
    assert registry_artifact._retry_after_seconds(
        urllib.error.HTTPError("u", 429, "", {"Retry-After": "7"}, None), 1) == 7.0
    assert registry_artifact._retry_after_seconds(
        urllib.error.HTTPError("u", 429, "", {}, None), 3) == 8.0


def test_fetch_surfaces_rate_limiting_once_the_attempts_run_out(monkeypatch):
    monkeypatch.setattr(registry_artifact, "_last_request", {})
    monkeypatch.setattr(registry_artifact.time, "sleep", lambda seconds: None)
    attempts = []

    def always_limited(request, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError("https://crates.io/x", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen", always_limited)
    with pytest.raises(urllib.error.HTTPError):
        registry_artifact.fetch("https://crates.io/x", 120, attempts=3)
    assert len(attempts) == 3


def test_crawl_marks_source_failures_as_failed(tmp_path, monkeypatch):
    def failed_source(*args):
        return {"failures": 1, "coverage_kind": "partial"}

    monkeypatch.setattr(registry_artifact, "_crawl_pypi", failed_source)
    report = registry_artifact.crawl_registry_sources(
        ["pypi"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json"
    )

    assert report["status"] == "failed"
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"
