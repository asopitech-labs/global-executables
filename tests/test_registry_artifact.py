from io import BytesIO
import gzip
import json
import tarfile
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


def test_npm_reads_the_release_document_not_the_packument(tmp_path, monkeypatch):
    """`bin` lives in versions[...]; reading the packument top level found nothing."""
    packument = {"name": "demo", "dist-tags": {"latest": "1.0.0"},
                 "versions": {"1.0.0": {"name": "demo", "version": "1.0.0", "bin": {"demo": "cli.js"}}}}
    release = packument["versions"]["1.0.0"]
    assert npm_metadata(packument, "https://registry.npmjs.org/demo") == []
    assert [row["command"] for row in npm_metadata(release, "x")] == ["demo"]

    asked = []

    def fake_fetch(url, timeout=120, attempts=4):
        asked.append(url)
        if "_changes" in url:
            return json.dumps({"results": [{"seq": 7, "id": "demo"}]}).encode(), {"downloaded_bytes": 1}
        return json.dumps(release).encode(), {"downloaded_bytes": 2}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    output = tmp_path / "npm.jsonl"
    report = registry_artifact._crawl_npm({}, output, 1, 1_000_000, 120)

    assert report["records"] == 1
    assert asked[-1].endswith("/demo/latest")
    assert json.loads(output.read_text().strip())["command"] == "demo"


def test_npm_is_only_complete_when_the_feed_runs_short_of_its_own_limit(tmp_path, monkeypatch):
    """A page shortened by the remaining budget used to be read as the end of the feed."""
    def fake_fetch(url, timeout=120, attempts=4):
        if "_changes" in url:
            limit = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["limit"][0])
            results = [{"seq": index, "id": f"pkg-{index}"} for index in range(1, limit + 1)]
            return json.dumps({"results": results}).encode(), {"downloaded_bytes": 1}
        return json.dumps({"name": "pkg", "version": "1.0.0"}).encode(), {"downloaded_bytes": 1}

    monkeypatch.setattr(registry_artifact, "fetch", fake_fetch)
    state = {"parser_generation": registry_artifact.NPM_PARSER_GENERATION}
    report = registry_artifact._crawl_npm(state, tmp_path / "npm.jsonl", 150, 1_000_000, 120)

    assert report["processed"] == 150
    assert report["complete"] is False and report["coverage_kind"] == "partial"


def test_npm_cursor_from_the_broken_parser_is_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setattr(registry_artifact, "fetch",
                        lambda *a, **k: (json.dumps({"results": []}).encode(), {"downloaded_bytes": 0}))
    state = {"since": 2548252, "complete": True}
    registry_artifact._crawl_npm(state, tmp_path / "npm.jsonl", 1, 1_000_000, 120)
    assert state["parser_generation"] == registry_artifact.NPM_PARSER_GENERATION
    assert state["since"] == 0  # the walk is redone now that it can record anything


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
