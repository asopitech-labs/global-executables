from io import BytesIO
import gzip
import json
import re
import tarfile
import pathlib
import urllib.error
import urllib.parse
import zipfile

import pytest

import global_executables.registry_artifact as registry_artifact
from global_executables.collectors import npm_metadata
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
        headers = {"Last-Modified": "Wed, 19 Aug 2026 02:00:00 GMT", "Content-Length": str(len(payload))}
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
    assert requested == [registry_artifact.CRATES_DB_DUMP, registry_artifact.CRATES_DB_DUMP]
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


def test_go_catalog_cursor_survives_an_interrupted_sweep(tmp_path, monkeypatch):
    """Run-level state is only written when a pass returns; the catalog phase outlives that."""
    pages = [
        b'{"Path":"example.com/a","Version":"v1","Timestamp":"2019-01-02T00:00:00Z"}\n',
        b'{"Path":"example.com/b","Version":"v1","Timestamp":"2019-01-03T00:00:00Z"}\n',
    ]

    def fake_fetch(url, timeout=120, attempts=4):
        if url.startswith(registry_artifact.GO_INDEX):
            if not pages:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
            return pages.pop(0), {"downloaded_bytes": 1}
        return b'{"Version":"v1"}', {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "GO_INDEX_PAGE", 1)
    monkeypatch.setattr(registry_artifact, "_go_module_rows", lambda *a: ([], 0))
    catalog = tmp_path / "go-modules.txt"

    # The third index page raises, so the pass never reaches its own state write.
    with pytest.raises(urllib.error.HTTPError):
        registry_artifact._crawl_go({"modules_file": str(catalog)}, tmp_path / "go.jsonl", 0, 1_000, 120)

    cursor = json.loads(catalog.with_suffix(".cursor.json").read_text())
    assert cursor["since"] == "2019-01-03T00:00:00Z" and cursor["complete"] is False
    assert catalog.read_text().split() == ["example.com/a", "example.com/b"]

    # A fresh state with no catalog_since still resumes from the checkpoint.
    seen = []
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda url, timeout=120, attempts=4: (seen.append(url), (b"", {"downloaded_bytes": 0}))[1])
    registry_artifact._crawl_go({"modules_file": str(catalog)}, tmp_path / "go.jsonl", 0, 1_000, 120)
    assert "2019-01-03" in seen[0]


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
    failures, unavailable, _attempts = _failure_state(state)
    assert set(unavailable) == {"no-dist", "yanked"}
    assert set(failures) == {"transient"}
    assert state["retry_projects"] == ["old-source"]


def _http_error(code: str | int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid/x", int(code), "gone", {}, None)


def test_withdrawn_and_route_collision_answers_are_permanent():
    """410 and 451 mean withdrawn; 405 is npm's answer for the package named "-"."""
    failures: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    attempts: dict[str, int] = {}
    for name, code in (("gone", 410), ("legal", 451), ("-", 405), ("missing", 404)):
        registry_artifact._record_failure(failures, unavailable, name, _http_error(code), attempts)
    assert set(unavailable) == {"gone", "legal", "-", "missing"}
    assert failures == {} and attempts == {}


def test_an_unreadable_artifact_stops_being_retried_but_stays_on_the_record():
    failures: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    attempts: dict[str, int] = {}
    error = zipfile.BadZipFile("File is not a zip file")
    for _ in range(registry_artifact.FAILURE_ATTEMPT_LIMIT - 1):
        registry_artifact._record_failure(failures, unavailable, "broken", error, attempts)
        assert "broken" in failures and "broken" not in unavailable
    registry_artifact._record_failure(failures, unavailable, "broken", error, attempts)
    assert failures == {} and attempts == {}
    assert unavailable["broken"].startswith("gave up after 3 attempts:")


def test_network_blips_never_exhaust_the_attempt_budget():
    """A DNS outage says nothing about the package, so it must not bury one."""
    failures: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    attempts: dict[str, int] = {}
    blip = urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
    for _ in range(registry_artifact.FAILURE_ATTEMPT_LIMIT * 3):
        registry_artifact._record_failure(failures, unavailable, "fine", blip, attempts)
    assert set(failures) == {"fine"} and unavailable == {} and attempts == {}


def test_a_tarball_declared_as_a_wheel_is_read_as_a_sdist(tmp_path, monkeypatch):
    """PyPI records the type the uploader claimed, so the filename is the honest signal."""
    catalog = tmp_path / "projects.txt"
    catalog.write_text("mislabelled\n")
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        body = b"[project]\nname = 'mislabelled'\n[project.scripts]\nallocate = 'app:main'\n"
        info = tarfile.TarInfo("mislabelled-1.0/pyproject.toml"); info.size = len(body)
        archive.addfile(info, BytesIO(body))
    tarball = stream.getvalue()

    def fake_fetch(url, timeout=120):
        if url.endswith(".tar.gz"):
            return tarball, {"downloaded_bytes": len(tarball)}
        return json.dumps({
            "info": {"name": "mislabelled", "version": "1.0"},
            "urls": [{"packagetype": "bdist_wheel", "filename": "mislabelled.tar.gz",
                      "url": "https://example.invalid/mislabelled.tar.gz"}],
        }).encode(), {}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_wheel_commands",
                        lambda url, timeout: pytest.fail("a .tar.gz must not be read as a wheel"))
    output = tmp_path / "out.jsonl"
    report = registry_artifact._crawl_pypi({"projects_file": str(catalog)}, output, 10, 10**9, 5)
    assert report["failures"] == 0
    assert [json.loads(line)["command"] for line in output.read_text().splitlines()] == ["allocate"]


def test_every_crawler_that_blocks_on_failures_can_revisit_one():
    """A source that refuses `exhaustive` while a failure stands must be able to retry it.

    Otherwise the cursor walks past a package that lost a DNS lookup, nothing ever
    brings it back, and the source is `partial` for good.  Found separately in
    RubyGems, npm and PyPI, so it is asserted rather than remembered.
    """
    import inspect

    source = inspect.getsource(registry_artifact)
    for name, function in vars(registry_artifact).items():
        if not name.startswith("_crawl_") or not callable(function):
            continue
        body = inspect.getsource(function)
        if "not failures" not in body or "_record_failure(" not in body:
            continue  # cannot strand a failure it never records, or never gates on one
        # Either shape works: queue it the moment it fails, or seed the queue from the
        # outstanding failures when the next pass starts.
        requeued = re.search(r"in failures and \w+ not in retry_\w+", body)
        seeded = re.search(r"for \w+ in failures:\s*\n\s*if \w+ not in retry_\w+:", body)
        assert requeued or seeded, (
            f"{name} blocks exhaustive on failures but never queues one for retry")
    assert source.count("not in retry_") >= 5


def test_npm_revisits_a_package_that_failed_on_a_blip(tmp_path, monkeypatch):
    catalog = tmp_path / "npm-packages.txt"
    catalog.write_text("good\nflaky\n")
    refused = {"flaky"}

    def fake_fetch(url, timeout=120):
        name = url.rstrip("/").rsplit("/", 2)[-2]
        if name in refused:
            raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        return json.dumps({"name": name, "version": "1.0.0", "bin": {name: "cli.js"}}).encode(), \
            {"downloaded_bytes": 10}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    state = {"packages_file": str(catalog), "parser_generation": registry_artifact.NPM_PARSER_GENERATION}
    first = registry_artifact._crawl_npm(state, tmp_path / "out.jsonl", 10, 10**9, 5)
    assert state["retry_npm"] == ["flaky"] and first["coverage_kind"] == "partial"

    refused.clear()
    second = registry_artifact._crawl_npm(state, tmp_path / "out.jsonl", 10, 10**9, 5)
    assert state["retry_npm"] == [] and second["failures"] == 0
    assert second["coverage_kind"] == "exhaustive"


def test_pypi_revisits_a_project_that_failed_on_a_blip(tmp_path, monkeypatch):
    catalog = tmp_path / "projects.txt"
    catalog.write_text("good\nflaky\n")
    refused = {"flaky"}

    def fake_fetch(url, timeout=120):
        project = url.rsplit("/", 2)[-2]
        if project in refused:
            raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        return json.dumps({"info": {"name": project, "version": "1.0.0"}, "urls": []}).encode(), {}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    state = {"projects_file": str(catalog)}
    first = registry_artifact._crawl_pypi(state, tmp_path / "out.jsonl", 10, 10**9, 5)
    assert state["retry_projects"] == ["flaky"] and first["coverage_kind"] == "partial"

    refused.clear()
    second = registry_artifact._crawl_pypi(state, tmp_path / "out.jsonl", 10, 10**9, 5)
    assert state["retry_projects"] == [] and second["failures"] == 0
    assert second["coverage_kind"] == "exhaustive"


def test_rubygems_revisits_a_gem_that_failed_on_a_blip(tmp_path, monkeypatch):
    catalog = tmp_path / "names.txt"
    catalog.write_text("good\nflaky\n")
    refused = {"flaky"}

    def fake_fetch(url, timeout=120):
        name = url.rsplit("/", 1)[-1].removesuffix(".json")
        if name in refused:
            raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        return json.dumps({"name": name, "version": "1.0.0", "gem_uri": "https://example.invalid/g.gem"}).encode(), {}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_gem_metadata", lambda url, timeout: (b"", 0))
    state = {"names_file": str(catalog)}
    first = registry_artifact._crawl_rubygems(state, tmp_path / "out.jsonl", 10, 10**9, 5)
    assert first["cursor"] == 2 and state["retry_gems"] == ["flaky"]
    assert first["coverage_kind"] == "partial"  # the cursor is at the end but one gem is owed

    refused.clear()  # the outage passes
    second = registry_artifact._crawl_rubygems(state, tmp_path / "out.jsonl", 10, 10**9, 5)
    assert state["retry_gems"] == [] and second["failures"] == 0
    assert second["coverage_kind"] == "exhaustive"


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
        def runner(state, output, budget, byte_budget, timeout, checkpoint=None):
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


def test_a_source_cannot_claim_exhaustive_with_nothing_on_file(tmp_path, monkeypatch):
    """npm claimed completeness for its whole history while recording no commands."""
    def empty_but_confident(state, output, budget, byte_budget, timeout, checkpoint=None):
        return {"coverage_kind": "exhaustive", "complete": True, "records": 0}

    def productive(state, output, budget, byte_budget, timeout, checkpoint=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"command": "demo"}\n')
        return {"coverage_kind": "exhaustive", "complete": True, "records": 1}

    monkeypatch.setattr(registry_artifact, "_crawl_npm", empty_but_confident)
    monkeypatch.setattr(registry_artifact, "_crawl_crates", productive)
    report = registry_artifact.crawl_registry_sources(
        ["npm", "crates"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json")

    npm = report["sources"]["npm"]
    assert npm["coverage_kind"] == "partial" and npm["complete"] is False
    assert npm["observations"] == 0 and "no observations" in npm["error"]
    assert report["status"] == "failed"
    # A source that actually collected something keeps its claim.
    assert report["sources"]["crates"]["coverage_kind"] == "exhaustive"
    assert report["sources"]["crates"]["observations"] == 1


def test_crawl_marks_source_failures_as_failed(tmp_path, monkeypatch):
    def failed_source(*args, **kwargs):
        return {"failures": 1, "coverage_kind": "partial"}

    monkeypatch.setattr(registry_artifact, "_crawl_pypi", failed_source)
    report = registry_artifact.crawl_registry_sources(
        ["pypi"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json"
    )

    assert report["status"] == "failed"
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"


def test_windows_command_names_drop_the_extension_users_never_type():
    """`curl.exe` has to collide with `curl` or the cross-ecosystem index is useless."""
    from global_executables.collectors import windows_command, scoop_manifests, winget_commands
    assert windows_command("curl.exe") == "curl"
    assert windows_command("BASH.EXE") == "BASH"
    assert windows_command("run.cmd") == "run"
    assert windows_command(".exe") == ".exe"  # not an extension on its own
    assert windows_command("tar") == "tar"

    manifests = [
        ("ripgrep", {"version": "1.0", "bin": "rg.exe"}),
        ("aliased", {"version": "2.0", "bin": [["bin\\tool.exe", "mytool.exe"]]}),
        ("library", {"version": "3.0"}),
    ]
    rows = scoop_manifests(manifests, "https://example.test/bucket")
    assert [(r["command"], r["package"]) for r in rows] == [("rg", "ripgrep"), ("mytool", "aliased")]
    assert rows[0]["distribution_family"] == "windows"

    # winget's index carries silent-install switches beside real commands
    pairs = [("rg", "BurntSushi.ripgrep"), ("/VERYSILENT", "Some.Installer"), ("gh.exe", "GitHub.cli")]
    assert [r["command"] for r in winget_commands(pairs, "x")] == ["gh", "rg"]


def test_msys2_reuses_the_pacman_reader_with_a_windows_identity():
    from global_executables.collectors import package_files
    text = "%NAME%\nmsys2-runtime\n%FILES%\nusr/bin/bash.exe\nusr/bin/tar.exe\n"
    rows = package_files(text, "msys2", "https://repo.msys2.org/msys.files",
                         family="windows", distribution="msys2")
    assert [r["command"] for r in rows] == ["bash", "tar"]
    assert rows[0]["distribution_family"] == "windows" and rows[0]["distribution"] == "msys2"
    # the same reader keeps Arch's identity and its extensions
    arch = package_files("%NAME%\ncoreutils\n%FILES%\nusr/bin/ls\n", "arch", "x")
    assert arch[0]["distribution"] == "archlinux"


def test_nuget_never_claims_exhaustive_from_a_truncated_catalog(tmp_path, monkeypatch):
    catalog = tmp_path / "tools.txt"
    catalog.write_text("demo.tool\n")
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda url, timeout=120, attempts=4: (json.dumps({"versions": ["1.0.0"]}).encode(), {"downloaded_bytes": 1}))
    monkeypatch.setattr(registry_artifact, "_nuget_tool_commands", lambda url, timeout: (["demo"], 5))
    state = {"tools_file": str(catalog), "catalog_truncated": True, "catalog_advertised": 8619}

    report = registry_artifact._crawl_nuget(state, tmp_path / "nuget.jsonl", 10, 1_000_000, 120)

    assert report["cursor"] == 1 and report["records"] == 1
    assert report["complete"] is False and report["coverage_kind"] == "partial"
    assert report["catalog_truncated"] is True and "sample" in report["note"]


def test_windows_image_commands_fold_case_and_cite_the_digest(monkeypatch):
    """The image ships ARP.EXE beside attrib.exe; both fold to what a user types."""
    import gzip as gziplib, io as iolib, tarfile as tarlib
    from global_executables import production

    stream = iolib.BytesIO()
    with tarlib.open(fileobj=stream, mode="w") as archive:
        for path in ("Files/Windows/System32/ARP.EXE", "Files/Windows/System32/attrib.exe",
                     "Files/Windows/System32/drivers/etc/hosts", "Files/Windows/System32/en-US/nested.exe",
                     "Files/Windows/notepad.exe", "Files/Windows/System32/readme.txt"):
            info = tarlib.TarInfo(path); info.size = 0
            archive.addfile(info, iolib.BytesIO(b""))
    layer = gziplib.compress(stream.getvalue())

    class _Response:
        def __init__(self, payload, headers=None):
            self._stream = iolib.BytesIO(payload); self.headers = headers or {}
        def read(self, size=-1): return self._stream.read(size)
        def __enter__(self): return self
        def __exit__(self, *args): return False

    manifest = json.dumps({"layers": [{"digest": "sha256:layer", "size": len(layer)}]}).encode()
    def fake_urlopen(request, timeout=None):
        if "manifests" in request.full_url:
            return _Response(manifest, {"Docker-Content-Digest": "sha256:pinned"})
        return _Response(layer)

    monkeypatch.setattr(production.urllib.request, "urlopen", fake_urlopen)
    rows, coverage = production._windows_image_rows("windows/nanoserver", "ltsc2022-amd64", 60)

    assert [row["command"] for row in rows] == ["arp", "attrib", "notepad"]
    assert coverage["image_digest"] == "sha256:pinned"
    # the tag moves, the digest does not, so the evidence cites the digest
    assert rows[0]["source"] == "windows/nanoserver@sha256:pinned"
    assert rows[0]["shipped_as"] == "ARP.EXE" and rows[0]["version"] == "ltsc2022-amd64"


def test_base_command_observations_accumulate_across_builds(tmp_path, monkeypatch):
    """No observation of a base command set is privileged; samples widen coverage."""
    from global_executables import production

    output = tmp_path / "macos.jsonl"
    output.write_text(json.dumps({"command": "only-on-intel", "package": "macos-base",
                                  "source": "macos@14.7.1-23H222-x86_64"}) + "\n")

    def observe(root):
        rows = [{"command": name, "package": "macos-base", "source": "macos@26.5.2-25F84-arm64"}
                for name in ("ls", "launchctl")]
        return rows, {"status": "success", "coverage_kind": "exhaustive", "records": len(rows),
                      "source": "macos@26.5.2-25F84-arm64", "downloaded_bytes": 0}

    monkeypatch.setattr(production, "_macos_rows", observe)
    coverage = production.crawl_source("macos", output, 60)

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert {row["command"] for row in rows} == {"only-on-intel", "ls", "launchctl"}
    assert sorted({row["source"] for row in rows}) == ["macos@14.7.1-23H222-x86_64",
                                                       "macos@26.5.2-25F84-arm64"]
    # the report cites the build this run observed, not the filesystem root it read
    assert coverage["source"] == "macos@26.5.2-25F84-arm64"
    assert coverage["observed"] == ["macos@26.5.2-25F84-arm64"]


def test_shell_builtins_are_pinned_to_the_release_that_defines_them(monkeypatch):
    """The set moves between releases: macOS bash 3.2 has 58, Debian bash 5.2 has 61."""
    from global_executables import production

    class _Result:
        def __init__(self, stdout): self.stdout = stdout

    def fake_run(argv, **kwargs):
        return _Result("5.2.37(1)-release" if "BASH_VERSION" in argv[-1] else "cd\nexport\n\n[\n")

    monkeypatch.setattr(production.__dict__["__builtins__"]["__import__"]("shutil"), "which",
                        lambda name: f"/bin/{name}")
    monkeypatch.setattr(production.__dict__["__builtins__"]["__import__"]("subprocess"), "run", fake_run)
    rows, coverage = production._shell_builtin_rows("bash")

    assert [row["command"] for row in rows] == ["[", "cd", "export"]
    assert coverage["source"] == "bash@5.2.37(1)-release"
    assert rows[0]["source_type"] == "shell_builtin" and rows[0]["shell_version"] == "5.2.37(1)-release"


def test_shells_that_cannot_be_asked_come_from_their_specification():
    from global_executables import production
    rows, coverage = production._shell_builtin_rows("cmd")
    commands = {row["command"] for row in rows}
    # exactly the names no filesystem scan reaches
    assert {"path", "dir", "set", "copy", "del", "echo"} <= commands
    assert coverage["source"].startswith("https://learn.microsoft.com/")
    posix, _ = production._shell_builtin_rows("sh")
    assert {"cd", "export", "trap", ":"} <= {row["command"] for row in posix}


def test_a_shell_absent_from_this_machine_is_not_observed_here(tmp_path, monkeypatch):
    from global_executables import production

    def only_bash(shell, executable=None):
        if shell != "bash":
            raise production.ProductionSourceError(f"shell is not installed here: {shell}")
        return ([{"command": "cd", "ecosystem": "shell", "package": "bash", "source": "bash@5"}],
                {"status": "success", "coverage_kind": "exhaustive", "records": 1,
                 "source": "bash@5", "downloaded_bytes": 0})

    monkeypatch.setattr(production, "_shell_builtin_rows", only_bash)
    monkeypatch.setitem(production.SOURCE_INDEXES, "shell", ["bash", "fish"])
    coverage = production.crawl_source("shell", tmp_path / "shell.jsonl", 60)

    assert coverage["records"] == 1
    assert coverage["coverage_kind"] == "partial"  # an unobserved shell is not a covered one
    assert [index["status"] for index in coverage["indexes"]] == ["success", "skipped"]


def test_progress_survives_an_interruption_mid_pass(tmp_path, monkeypatch):
    """State was written once, after every source finished, so a kill lost the lot."""
    catalog = tmp_path / "projects.txt"
    catalog.write_text("\n".join(f"pkg-{i}" for i in range(1000)) + "\n")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"version": 1, "sources": {
        "pypi": {"projects_file": str(catalog)}}}) + "\n")

    seen = {"count": 0}

    def fake_fetch(url, timeout=120, attempts=4):
        seen["count"] += 1
        if seen["count"] > 250:          # the process is killed part-way through the pass
            registry_artifact._interrupted = True
        project = url.rsplit("/", 2)[-2]
        return json.dumps({"info": {"name": project, "version": "1.0.0"},
                           "urls": [{"packagetype": "bdist_wheel", "url": f"https://x/{project}.whl",
                                     "filename": f"{project}-none-any.whl"}]}).encode(), {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_wheel_commands", lambda url, timeout: ([url.rsplit("/", 1)[-1][:-4]], 1))
    monkeypatch.setattr(registry_artifact, "_interrupted", False)
    try:
        report = registry_artifact.crawl_registry_sources(
            ["pypi"], state_path, tmp_path / "intermediate", tmp_path / "report.json", package_budget=1000)
    finally:
        registry_artifact._interrupted = False

    saved = json.loads(state_path.read_text())["sources"]["pypi"]
    observations = (tmp_path / "intermediate" / "pypi.jsonl").read_text().splitlines()
    # the cursor and the rows both survive, and they agree with each other
    assert saved["cursor"] >= registry_artifact.CHECKPOINT_INTERVAL
    assert saved["cursor"] == len(observations)
    assert report["sources"]["pypi"]["cursor"] == saved["cursor"]


def test_a_finished_source_keeps_its_cursor_when_a_later_one_dies(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"

    def finished(state, output, budget, byte_budget, timeout, checkpoint=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"command": "demo"}\n')
        state["cursor"] = 4242
        return {"coverage_kind": "partial", "complete": False, "cursor": 4242, "records": 1}

    def explodes(state, output, budget, byte_budget, timeout, checkpoint=None):
        raise RuntimeError("killed mid-pass")

    monkeypatch.setattr(registry_artifact, "_crawl_npm", finished)
    monkeypatch.setattr(registry_artifact, "_crawl_go", explodes)
    registry_artifact.crawl_registry_sources(
        ["npm", "go"], state_path, tmp_path / "intermediate", tmp_path / "report.json")

    assert json.loads(state_path.read_text())["sources"]["npm"]["cursor"] == 4242


def test_an_unchanged_crates_dump_is_not_downloaded_again(tmp_path, monkeypatch):
    """The dump is republished daily; re-reading it costs 1.7GB to rewrite one file."""
    output = tmp_path / "crates.jsonl"
    output.write_text('{"command": "rg", "package": "ripgrep"}\n')

    class _Head:
        headers = {"Last-Modified": "Wed, 20 Aug 2026 02:00:00 GMT", "Content-Length": "1750000000"}
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen", lambda request, timeout=None: _Head())
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda *a, **k: pytest.fail("an unchanged dump must not be fetched"))
    state = {"dump_last_modified": "Wed, 20 Aug 2026 02:00:00 GMT", "catalog_size": 319466,
             "dump_timestamp": "2026-08-20T02:00:21Z"}

    report = registry_artifact._crawl_crates(state, output, 10, 5_000_000_000, 120)

    assert report["unchanged"] is True and report["downloaded_bytes"] == 0
    assert report["coverage_kind"] == "exhaustive" and report["records"] == 1
    assert report["cursor"] == 319466


def test_every_crawler_can_actually_call_its_checkpoint(tmp_path, monkeypatch):
    """A local name shadowed the checkpoint parameter, so Go failed the moment it fired."""
    import inspect
    calls = []

    def spy(buffer=None, **updates):
        calls.append(updates)

    # Go reaches its checkpoint after CHECKPOINT_INTERVAL modules, so make that one module.
    monkeypatch.setattr(registry_artifact, "CHECKPOINT_INTERVAL", 1)
    catalog = tmp_path / "go-modules.txt"
    catalog.write_text("example.com/a\nexample.com/b\n")
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda url, timeout=120, attempts=4: (b'{"Version":"v1"}', {"downloaded_bytes": 1}))
    monkeypatch.setattr(registry_artifact, "_go_module_rows", lambda *a: ([], 0))
    state = {"modules_file": str(catalog), "catalog_complete": True}

    registry_artifact._crawl_go(state, tmp_path / "go.jsonl", 2, 1_000_000, 120, spy)

    assert [update["cursor"] for update in calls] == [1, 2]
    # and no crawler may shadow the parameter it was handed
    for name in ("_crawl_pypi", "_crawl_npm", "_crawl_crates", "_crawl_go",
                 "_crawl_rubygems", "_crawl_packagist", "_crawl_nuget"):
        source = inspect.getsource(getattr(registry_artifact, name))
        assert "\n    checkpoint = " not in source, f"{name} rebinds its checkpoint parameter"



def test_npm_enumerates_packages_once_instead_of_every_revision(tmp_path, monkeypatch):
    """The changes feed carries 126M revisions against 4.3M packages."""
    release = {"name": "demo", "version": "1.0.0", "bin": {"demo": "cli.js"}}
    asked = []

    def fake_fetch(url, timeout=120, attempts=4):
        asked.append(url)
        if url.startswith(registry_artifact.NPM_ALL_DOCS):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            start = json.loads(query["startkey"][0]) if "startkey" in query else ""
            names = ["alpha", "beta", "gamma"]
            rows = [{"id": n} for n in names if n >= start]
            return json.dumps({"rows": rows, "total_rows": 3}).encode(), {"downloaded_bytes": 5}
        return json.dumps(release).encode(), {"downloaded_bytes": 2}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    catalog = tmp_path / "npm-packages.txt"
    state = {"packages_file": str(catalog), "since": 2548252, "complete": True}

    report = registry_artifact._crawl_npm(state, tmp_path / "npm.jsonl", 10, 1_000_000, 120)

    assert registry_artifact.read_catalog(catalog) == ["alpha", "beta", "gamma"]
    assert catalog.with_suffix(".txt.gz").is_file()  # 85MB of names does not belong uncompressed
    assert report["catalog_size"] == 3 and report["cursor"] == 3 and report["records"] == 3
    assert report["coverage_kind"] == "exhaustive"
    assert "since" not in state  # the revision cursor described coverage never collected
    assert not any("_changes" in url for url in asked)
    assert asked[-1].endswith("/gamma/latest")


def test_the_crates_dump_clears_verdicts_the_retired_path_left(tmp_path, monkeypatch):
    """One stale IncompleteRead held a complete crates.io at partial forever."""
    class _Head:
        headers = {"Last-Modified": "Thu, 21 Aug 2026 02:00:00 GMT", "Content-Length": "10"}
        def __enter__(self): return self
        def __exit__(self, *args): return False

    output = tmp_path / "crates.jsonl"
    output.write_text('{"command": "rg"}\n')
    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen", lambda request, timeout=None: _Head())
    state = {"dump_last_modified": "Thu, 21 Aug 2026 02:00:00 GMT", "catalog_size": 319466,
             "failures": {"civ_map_generator": "IncompleteRead(0 bytes read)"},
             "unavailable": {"yanked-crate": "all versions are yanked"}}

    report = registry_artifact._crawl_crates(state, output, 10, 5_000_000_000, 120)

    assert report["failures"] == 0 and report["coverage_kind"] == "exhaustive"
    assert "failures" not in state or not state["failures"]


def test_a_catalog_written_uncompressed_is_still_readable(tmp_path):
    """Catalogs already published as plain text must keep working after the change."""
    plain = tmp_path / "names.txt"
    plain.write_text("alpha\nbeta\n\ngamma\n")
    assert registry_artifact.read_catalog(plain) == ["alpha", "beta", "gamma"]

    registry_artifact.write_catalog(plain, ["delta", "epsilon"])
    assert not plain.exists()  # the plain copy is replaced, not left to drift
    assert registry_artifact.read_catalog(plain) == ["delta", "epsilon"]
    assert registry_artifact.read_catalog(tmp_path / "absent.txt") == []


def test_nuget_derives_truncation_for_a_catalog_built_before_the_check(tmp_path, monkeypatch):
    """The catalog carried no verdict, so the source was about to claim a sample as complete."""
    catalog = tmp_path / "tools.txt"
    registry_artifact.write_catalog(catalog, [f"tool-{i}" for i in range(4000)])
    asked = []

    def fake_fetch(url, timeout=120, attempts=4):
        asked.append(url)
        if url.startswith(registry_artifact.NUGET_SEARCH):
            return json.dumps({"totalHits": 8619, "data": []}).encode(), {"downloaded_bytes": 1}
        return json.dumps({"versions": ["1.0.0"]}).encode(), {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_nuget_tool_commands", lambda url, timeout: ([], 1))
    state = {"tools_file": str(catalog), "cursor": 4000}  # cursor already at the end

    report = registry_artifact._crawl_nuget(state, tmp_path / "nuget.jsonl", 10, 1_000_000, 120)

    assert state["catalog_advertised"] == 8619 and state["catalog_truncated"] is True
    assert report["complete"] is False and report["coverage_kind"] == "partial"
    assert any(url.startswith(registry_artifact.NUGET_SEARCH) for url in asked)


def test_nuget_partitions_the_term_space_to_pass_the_paging_cap(tmp_path, monkeypatch):
    """One query stops at 4,000 of 9,190 tools; the cap is per query, not per catalogue."""
    universe = {f"{letter}-tool-{index}": letter
                for letter in "abc" for index in range(3000)}
    asked = []

    def fake_fetch(url, timeout=120, attempts=4):
        asked.append(url)
        # parse_qs drops blank values by default, and the empty term is a real query
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
        term, skip = query["q"][0], int(query["skip"][0])
        matching = sorted(name for name, letter in universe.items() if not term or letter == term)
        page = matching[skip:skip + 1000][:4000 - skip] if skip < 4000 else []
        return json.dumps({"totalHits": len(universe), "data": [{"id": n} for n in page]}).encode(), \
               {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_nuget_tool_commands", lambda url, timeout: ([], 0))
    catalog = tmp_path / "tools.txt"
    state = {"tools_file": str(catalog)}

    registry_artifact._crawl_nuget(state, tmp_path / "nuget.jsonl", 0, 1_000_000, 120)

    # the empty query alone would have stopped at 4,000 of 9,000
    assert len(registry_artifact.read_catalog(catalog)) == len(universe)
    assert state["catalog_truncated"] is False
    assert sum(1 for url in asked if "q=a&" in url or "q=a%26" in url) > 0


def test_a_built_catalogue_survives_a_later_failure_in_the_same_pass(tmp_path, monkeypatch):
    """The catalogue is the expensive part of a pass; a hiccup after it must not undo it."""
    saved = []

    def spy(buffer=None, **updates):
        saved.append(dict(updates))

    calls = {"n": 0}

    def flaky(url, timeout=120, attempts=4):
        calls["n"] += 1
        if url.startswith(registry_artifact.NUGET_SEARCH):
            term = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)["q"][0]
            page = [{"id": f"{term or '(empty)'}-tool"}] if int(
                urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["skip"][0]) == 0 else []
            return json.dumps({"totalHits": 37, "data": page}).encode(), {"downloaded_bytes": 1}
        raise OSError("IncompleteRead(846266 bytes read)")   # every inspection fails

    monkeypatch.setattr(registry_artifact, "fetch", flaky)
    catalog = tmp_path / "tools.txt"
    state = {"tools_file": str(catalog)}

    registry_artifact._crawl_nuget(state, tmp_path / "nuget.jsonl", 5, 1_000_000, 120, spy)

    # the verdict was checkpointed before any tool was inspected
    assert saved and saved[0] == {}
    assert state["catalog_advertised"] == 37
    assert len(registry_artifact.read_catalog(catalog)) == 37


def test_no_crawler_builds_a_catalogue_without_persisting_it(tmp_path):
    """A catalogue costs hundreds of requests; losing it to a later hiccup is the bug
    that hit Go, then NuGet, then npm, and was still open for three more sources."""
    import inspect
    unguarded = []
    for name in ("_crawl_pypi", "_crawl_rubygems", "_crawl_packagist",
                 "_crawl_nuget", "_crawl_npm", "_crawl_go"):
        body = inspect.getsource(getattr(registry_artifact, name))
        built = max(body.find("write_catalog("), body.find("_save_catalog_cursor("))
        if built < 0:
            continue
        window = body[built:built + 400]
        if "checkpoint()" not in window and "_save_catalog_cursor(" not in window:
            unguarded.append(name)
    assert unguarded == [], f"catalogue discarded on failure: {unguarded}"


def test_a_declared_entry_becomes_the_command_an_install_creates():
    """Live data carried '../bin/code-labs', '/arake', '' and '@scope/tool' as commands."""
    from global_executables.collectors import declared_command
    assert declared_command("../bin/code-labs") == "code-labs"
    assert declared_command("/arake") == "arake"
    assert declared_command("@scope/ivue-cli") == "ivue-cli"
    assert declared_command("bin\\tool.exe") == "tool.exe"
    assert declared_command("rg") == "rg"
    for nothing in ("", "   ", ".", "..", "/", None, 7):
        assert declared_command(nothing) is None

    # npm bin keys and gemspec executables are declarations, not filesystem entries
    rows = npm_metadata({"name": "@a/b", "version": "1.0",
                         "bin": {"": "x", "@a/ivue-cli": "y", "ok": "z"}}, "s")
    assert sorted(row["command"] for row in rows) == ["ivue-cli", "ok"]
    gem = _gem_rows("executables:\n- ../bin/code-labs\n- /arake\n- rake\n", "g", "1.0", None, "s")
    assert [row["command"] for row in gem] == ["arake", "code-labs", "rake"]


def test_a_budget_for_an_unselected_source_is_rejected(tmp_path):
    """CI kept --source-package-budget go=10000 after Go moved to local containers, and
    every run then died at argument parsing rather than crawling crates."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "tools/registry_artifact_crawl.py", "--source", "crates",
         "--source-package-budget", "go=10000", "--state", str(tmp_path / "s.json"),
         "--output-dir", str(tmp_path), "--report", str(tmp_path / "r.json")],
        capture_output=True, text=True, cwd=pathlib.Path(__file__).resolve().parents[1],
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"})
    assert result.returncode == 2
    assert "for a selected source" in result.stderr


def test_a_pass_reports_every_row_it_flushed_not_the_leftover(tmp_path, monkeypatch):
    """Checkpointing empties the buffer, so `records` counted only the tail since the
    last one — NuGet reported 0 records for passes that wrote thousands of rows."""
    catalog = tmp_path / "projects.txt"
    catalog.write_text("\n".join(f"pkg-{i}" for i in range(500)) + "\n")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(
        {"version": 1, "sources": {"pypi": {"projects_file": str(catalog)}}}) + "\n")
    monkeypatch.setattr(registry_artifact, "CHECKPOINT_INTERVAL", 100)

    def fake_fetch(url, timeout=120, attempts=4):
        project = url.rsplit("/", 2)[-2]
        return json.dumps({"info": {"name": project, "version": "1.0.0"},
                           "urls": [{"packagetype": "bdist_wheel", "url": f"https://x/{project}.whl",
                                     "filename": f"{project}-none-any.whl"}]}).encode(), {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_wheel_commands", lambda url, timeout: (["cmd"], 1))
    report = registry_artifact.crawl_registry_sources(
        ["pypi"], state_path, tmp_path / "intermediate", tmp_path / "report.json",
        package_budget=350)

    written = len((tmp_path / "intermediate" / "pypi.jsonl").read_text().splitlines())
    assert written == 350
    assert report["sources"]["pypi"]["records"] == written  # not 50, the residual buffer


def test_a_finished_catalogue_still_retries_what_failed(tmp_path, monkeypatch):
    """Six DNS blips held a fully-walked NuGet at partial with nothing left to walk."""
    catalog = tmp_path / "tools.txt"
    registry_artifact.write_catalog(catalog, ["alpha", "beta"])
    attempted = []

    def fake_fetch(url, timeout=120, attempts=4):
        attempted.append(url)
        return json.dumps({"versions": ["1.0.0"]}).encode(), {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_nuget_tool_commands", lambda url, timeout: (["cmd"], 1))
    # the catalogue is already walked and two tools failed on a transient error
    state = {"tools_file": str(catalog), "cursor": 2, "catalog_advertised": 2,
             "catalog_truncated": False,
             "failures": {"alpha": "<urlopen error [Errno -3] Temporary failure in name resolution>"}}

    report = registry_artifact._crawl_nuget(state, tmp_path / "nuget.jsonl", 10, 1_000_000, 120)

    assert any("alpha" in url for url in attempted)   # it was reachable again
    assert report["failures"] == 0 and report["retry_pending"] == 0
    assert report["complete"] is True and report["coverage_kind"] == "exhaustive"
    assert report["cursor"] == 2  # a retry does not advance the catalogue cursor


def test_a_yanked_gem_is_not_fetched_or_retried(tmp_path, monkeypatch):
    """RubyGems keeps listing a yanked gem while its CDN answers AccessDenied, so 38 of
    them sat in `failures` being retried forever."""
    catalog = tmp_path / "names.txt"
    registry_artifact.write_catalog(catalog, ["live", "spam"])
    fetched = []

    def fake_fetch(url, timeout=120, attempts=4):
        fetched.append(url)
        name = url.rsplit("/", 1)[-1].removesuffix(".json")
        return json.dumps({"name": name, "version": "1.0.0", "yanked": name == "spam",
                           "gem_uri": f"https://rubygems.org/gems/{name}-1.0.0.gem"}).encode(), \
               {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    monkeypatch.setattr(registry_artifact, "_gem_metadata",
                        lambda url, timeout: (gzip.compress(b"executables:\n- live\n"), 1))
    state = {"names_file": str(catalog)}

    report = registry_artifact._crawl_rubygems(state, tmp_path / "rubygems.jsonl", 10, 1_000_000, 120)

    assert not any("spam-1.0.0.gem" in url for url in fetched)   # never asked for
    assert report["failures"] == 0 and report["unavailable"] == 1
    assert state["unavailable"]["spam"] == "gem is yanked"
    assert report["cursor"] == 2 and report["coverage_kind"] == "exhaustive"
