from io import BytesIO
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
from global_executables.registry_artifact import _failure_state, _postgres_array


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


def test_fetch_retries_a_lost_lookup_but_gives_up_during_an_outage(monkeypatch):
    """3,000 Go modules once failed in a burst because nothing retried a name lookup."""
    monkeypatch.setattr(registry_artifact, "_network_failure_streak", 0)
    monkeypatch.setattr(registry_artifact.time, "sleep", lambda _seconds: None)
    calls = []

    def flaky(request, timeout=None):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")
        class _Response:
            status = 200
            def read(self): return b"ok"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Response()

    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen", flaky)
    body, _ = registry_artifact.fetch("https://example.invalid/a", timeout=5)
    assert body == b"ok" and len(calls) == 3  # two blips absorbed
    assert registry_artifact._network_failure_streak == 0  # a success clears the streak

    # A timeout already spent the whole budget, so retrying it only multiplies the wait.
    for reason in (TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))):
        attempted = []
        monkeypatch.setattr(registry_artifact.urllib.request, "urlopen",
                            lambda request, timeout=None, _r=reason: (attempted.append(1), (_ for _ in ()).throw(_r))[0])
        with pytest.raises((TimeoutError, urllib.error.URLError)):
            registry_artifact.fetch("https://example.invalid/slow", timeout=5)
        assert attempted == [1], f"a {type(reason).__name__} must cost one attempt, not four"

    # Once the failures stop looking isolated, a request costs one attempt, not four.
    always_down = lambda request, timeout=None: (_ for _ in ()).throw(
        urllib.error.URLError("[Errno -2] Name or service not known"))
    monkeypatch.setattr(registry_artifact.urllib.request, "urlopen", always_down)
    for _ in range(registry_artifact.NETWORK_OUTAGE_STREAK):
        with pytest.raises(urllib.error.URLError):
            registry_artifact.fetch("https://example.invalid/b", timeout=5)
    attempts_before = registry_artifact._network_failure_streak
    with pytest.raises(urllib.error.URLError):
        registry_artifact.fetch("https://example.invalid/c", timeout=5)
    assert registry_artifact._network_failure_streak == attempts_before + 1


def test_a_host_is_resolved_once_per_interval_not_once_per_request(monkeypatch):
    """A lookup per package is what the container's resolver gives way under."""
    import socket as socket_module

    lookups = []
    monkeypatch.setattr(socket_module, "getaddrinfo",
                        lambda host, port, *a: lookups.append(host) or [("fam", "type", 0, "", (host, port))])
    clock = [500.0]
    monkeypatch.setattr(registry_artifact.time, "monotonic", lambda: clock[0])

    registry_artifact.install_dns_cache()
    for _ in range(20):
        socket_module.getaddrinfo("proxy.golang.org", 443)
    assert lookups == ["proxy.golang.org"], "twenty requests must cost one lookup"

    socket_module.getaddrinfo("pypi.org", 443)  # a different host is its own entry
    clock[0] += registry_artifact.DNS_CACHE_SECONDS + 1
    socket_module.getaddrinfo("proxy.golang.org", 443)
    assert lookups == ["proxy.golang.org", "pypi.org", "proxy.golang.org"]


def test_a_slow_source_still_checkpoints_on_the_clock(monkeypatch):
    """200 Go modules can outlast the pass, and a pass killed before a checkpoint wrote nothing."""
    clock = [1000.0]
    monkeypatch.setattr(registry_artifact.time, "monotonic", lambda: clock[0])
    # The clock is module state; leaving a fake reading behind makes the next test see a
    # two-minute gap against the real clock and checkpoint when it should not.
    monkeypatch.setattr(registry_artifact, "_last_checkpoint", 0.0)
    registry_artifact._start_checkpoint_clock()

    assert registry_artifact._due_for_checkpoint(1) is False
    assert registry_artifact._due_for_checkpoint(2) is False
    clock[0] += registry_artifact.CHECKPOINT_SECONDS - 1
    assert registry_artifact._due_for_checkpoint(3) is False
    clock[0] += 2
    assert registry_artifact._due_for_checkpoint(4) is True, "the clock alone must be enough"
    # And the count still fires for a fast source that never waits.
    assert registry_artifact._due_for_checkpoint(registry_artifact.CHECKPOINT_INTERVAL) is True


def test_every_crawler_that_blocks_on_failures_can_revisit_one():
    """A source that refuses `exhaustive` while a failure stands must be able to retry it.

    Otherwise the cursor walks past a package that lost a DNS lookup, nothing ever
    brings it back, and the source is `partial` for good.  Found separately in
    Multiple registry implementations needed this invariant, so it is asserted rather
    than remembered.
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
    assert source.count("not in retry_") >= 3


def test_source_package_budget_overrides_the_shared_budget(tmp_path, monkeypatch):
    seen = {}

    def spy(name):
        def runner(state, output, budget, byte_budget, timeout, checkpoint=None):
            seen[name] = budget
            return {"coverage_kind": "partial", "complete": False}
        return runner

    monkeypatch.setattr(registry_artifact, "_crawl_crates", spy("crates"))
    monkeypatch.setattr(registry_artifact, "_crawl_nuget", spy("nuget"))
    report = registry_artifact.crawl_registry_sources(
        ["nuget", "crates"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json",
        package_budget=1000, source_budgets={"crates": 10_000},
    )

    assert seen == {"nuget": 1000, "crates": 10_000}
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
    """A cursor alone cannot prove exhaustive coverage without observations."""
    def empty_but_confident(state, output, budget, byte_budget, timeout, checkpoint=None):
        return {"coverage_kind": "exhaustive", "complete": True, "records": 0}

    def productive(state, output, budget, byte_budget, timeout, checkpoint=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"command": "demo"}\n')
        return {"coverage_kind": "exhaustive", "complete": True, "records": 1}

    monkeypatch.setattr(registry_artifact, "_crawl_nuget", empty_but_confident)
    monkeypatch.setattr(registry_artifact, "_crawl_crates", productive)
    report = registry_artifact.crawl_registry_sources(
        ["nuget", "crates"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json")

    nuget = report["sources"]["nuget"]
    assert nuget["coverage_kind"] == "partial" and nuget["complete"] is False
    assert nuget["observations"] == 0 and "no observations" in nuget["error"]
    assert report["status"] == "failed"
    # A source that actually collected something keeps its claim.
    assert report["sources"]["crates"]["coverage_kind"] == "exhaustive"
    assert report["sources"]["crates"]["observations"] == 1


def test_crawl_marks_source_failures_as_failed(tmp_path, monkeypatch):
    def failed_source(*args, **kwargs):
        return {"failures": 1, "coverage_kind": "partial"}

    monkeypatch.setattr(registry_artifact, "_crawl_nuget", failed_source)
    report = registry_artifact.crawl_registry_sources(
        ["nuget"], tmp_path / "state.json", tmp_path / "intermediate", tmp_path / "report.json"
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
        return _Result("5.2.37(1)-release" if "BASH_VERSION" in argv[-1] else ".\n..\ncd\nexport\n\n[\n")

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
    commands = {row["command"] for row in posix}
    assert {"cd", "export", "trap", ":"} <= commands
    assert "." not in commands and ".." not in commands


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


def test_no_crawler_shadows_its_checkpoint_callback():
    """Every crawler must retain the checkpoint callback handed to it."""
    import inspect

    for name in ("_crawl_crates", "_crawl_nuget"):
        source = inspect.getsource(getattr(registry_artifact, name))
        assert "\n    checkpoint = " not in source, f"{name} rebinds its checkpoint parameter"


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
    that hit multiple registries and was still open for three more sources."""
    import inspect
    unguarded = []
    for name in ("_crawl_nuget",):
        body = inspect.getsource(getattr(registry_artifact, name))
        built = body.find("write_catalog(")
        if built < 0:
            continue
        window = body[built:built + 400]
        if "checkpoint()" not in window:
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

    # npm bin keys are declarations, not filesystem entries.
    rows = npm_metadata({"name": "@a/b", "version": "1.0",
                         "bin": {"": "x", "@a/ivue-cli": "y", "ok": "z"}}, "s")
    assert sorted(row["command"] for row in rows) == ["ivue-cli", "ok"]


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


@pytest.mark.parametrize("source", ["go", "npm", "pypi", "rubygems", "packagist"])
def test_python_registry_runtime_rejects_transactional_sources(tmp_path, source):
    """Transactional sources have one supported runtime; Python must not be selectable."""
    state_path = tmp_path / f"{source}-state.json"
    state_path.write_text(json.dumps({"version": 1, "sources": {source: {}}}))

    with pytest.raises(registry_artifact.RegistryCrawlError,
                       match=f"unsupported registry source: {source}"):
        registry_artifact.crawl_registry_sources(
            [source], state_path, tmp_path / "intermediate", tmp_path / "report.json", package_budget=0)


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


def test_nuget_continuously_refreshes_completed_catalog_and_replaces_old_commands(tmp_path, monkeypatch):
    catalog = tmp_path / "tools.txt"
    registry_artifact.write_catalog(catalog, ["alpha", "beta"])
    output = tmp_path / "nuget.jsonl"
    output.write_text(
        json.dumps({"command": "old", "ecosystem": "nuget", "package": "alpha", "source": "old"}) + "\n" +
        json.dumps({"command": "keep", "ecosystem": "nuget", "package": "beta", "source": "keep"}) + "\n"
    )

    monkeypatch.setattr(registry_artifact, "fetch", lambda *args, **kwargs:
                        (json.dumps({"versions": ["2.0.0"]}).encode(), {"downloaded_bytes": 1}))
    monkeypatch.setattr(registry_artifact, "_nuget_tool_commands", lambda url, timeout: (["new"], 1))
    state = {"tools_file": str(catalog), "cursor": 2, "refresh_cursor": 0,
             "catalog_advertised": 2, "catalog_truncated": False}

    report = registry_artifact._crawl_nuget(state, output, 1, 1_000_000, 120)

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert report["complete"] is True and report["refreshed"] == 1
    assert state["refresh_cursor"] == 1
    assert state["catalog_size"] == 2 and state["catalog_complete"] is True
    assert {(row["package"], row["command"]) for row in rows} == {("alpha", "new"), ("beta", "keep")}
