"""On-demand package discovery contract, evidence checks, cache, and CLI parity."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import zipfile

import pytest

from global_executables import cli
from global_executables.discovery import (
    LATEST_TTL_SECONDS,
    MAX_METADATA_BYTES,
    AcquisitionError,
    PackageDiscovery,
    Response,
)


def npm_tar(manifest: dict, files: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    members = {"package/package.json": json.dumps(manifest).encode(), **(files or {})}
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def wheel(entry_points: str | None, files: dict[str, bytes] | None = None, *,
          name: str = "sample", version: str = "1.0") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        if entry_points is not None:
            archive.writestr(f"{name}-{version}.dist-info/entry_points.txt", entry_points)
        for name, body in (files or {}).items():
            archive.writestr(name, body)
    return output.getvalue()


class Routes:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, _timeout, _limit):
        self.requests.append(request)
        value = self.routes[request.full_url]
        if callable(value):
            value = value(request)
        status, headers, body = value
        return Response(status, {key.lower(): val for key, val in headers.items()}, body, request.full_url)


def test_npm_verifies_bin_declaration_digest_and_archive_member(tmp_path):
    artifact = npm_tar({"name": "sample", "version": "1.2.3", "bin": {"sample-cli": "bin/cli.js"}},
                       {"package/bin/cli.js": b"#!/usr/bin/env node\n"})
    metadata = {"name": "sample", "version": "1.2.3", "dist": {
        "tarball": "https://registry.npmjs.org/sample/-/sample-1.2.3.tgz",
        "shasum": hashlib.sha1(artifact).hexdigest(),
    }}
    routes = Routes({
        "https://registry.npmjs.org/sample/1.2.3": (200, {"ETag": '"one"'}, json.dumps(metadata).encode()),
        metadata["dist"]["tarball"]: (200, {}, artifact),
    })

    result = PackageDiscovery(tmp_path, fetcher=routes).discover("npm", "sample", "1.2.3", "sample-cli")

    assert result["outcome"] == "verified_entry_point"
    assert result["commands"] == [{"command": "sample-cli", "entry_point": "bin/cli.js"}]
    assert result["artifact_digest"] == f"sha256:{hashlib.sha256(artifact).hexdigest()}"
    assert result["publication_status"] == "not_indexed"


def test_npm_rejects_declared_bin_missing_from_exact_artifact(tmp_path):
    artifact = npm_tar({"name": "sample", "version": "1.0", "bin": {"sample": "missing.js"}})
    metadata = {"name": "sample", "version": "1.0", "dist": {
        "tarball": "https://registry.npmjs.org/sample/-/sample.tgz",
        "shasum": hashlib.sha1(artifact).hexdigest(),
    }}
    routes = Routes({
        "https://registry.npmjs.org/sample/latest": (200, {}, json.dumps(metadata).encode()),
        metadata["dist"]["tarball"]: (200, {}, artifact),
    })

    result = PackageDiscovery(tmp_path, fetcher=routes).discover("npm", "sample")

    assert result["outcome"] == "unverified"
    assert "absent from artifact" in result["unresolved"][0]


def test_pypi_verifies_console_script_and_reports_command_mismatch(tmp_path):
    artifact = wheel("[console_scripts]\nsample = sample.cli:main\n",
                     {"sample/cli.py": b"def main(): pass\n"}, version="2.0")
    metadata = {"info": {"name": "Sample", "version": "2.0"}, "urls": [{
        "packagetype": "bdist_wheel", "filename": "sample-2.0-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/sample.whl",
        "digests": {"sha256": hashlib.sha256(artifact).hexdigest()},
    }]}
    routes = Routes({
        "https://pypi.org/pypi/sample/json": (200, {}, json.dumps(metadata).encode()),
        metadata["urls"][0]["url"]: (200, {}, artifact),
    })

    result = PackageDiscovery(tmp_path, fetcher=routes).discover(
        "pypi", "sample", command="other", target={"python_tag": "py3", "platform_tag": "any"})

    assert result["outcome"] == "unverified"
    assert result["commands"] == [{"command": "sample", "entry_point": "sample.cli:main"}]
    assert result["canonical_package"] == "Sample"
    assert result["unresolved"] == ["requested command is not declared: other"]


def test_pypi_library_only_and_target_mismatch_are_explicit(tmp_path):
    artifact = wheel(None, {"sample/__init__.py": b""})
    item = {"packagetype": "bdist_wheel", "filename": "sample-1.0-cp312-cp312-win_amd64.whl",
            "url": "https://files.pythonhosted.org/packages/sample.whl",
            "digests": {"sha256": hashlib.sha256(artifact).hexdigest()}}
    metadata = {"info": {"name": "sample", "version": "1.0"}, "urls": [item]}
    routes = Routes({"https://pypi.org/pypi/sample/1.0/json": (200, {}, json.dumps(metadata).encode()),
                     item["url"]: (200, {}, artifact)})
    engine = PackageDiscovery(tmp_path, fetcher=routes)

    mismatch = engine.discover("pypi", "sample", "1.0", target={"platform_tag": "manylinux_2_17_x86_64"})
    library = engine.discover("pypi", "sample", "1.0", target={"platform_tag": "win_amd64"})

    assert mismatch["outcome"] == "unverified"
    assert mismatch["unresolved"] == ["no target-compatible wheel"]
    assert library["outcome"] == "unverified"
    assert library["unresolved"] == ["release declares no installed commands"]


def test_artifact_identity_mismatch_and_yanked_wheel_are_explicit(tmp_path):
    wrong = wheel("[console_scripts]\nsample = sample.cli:main\n",
                  {"sample/cli.py": b""}, name="other")
    item = {"packagetype": "bdist_wheel", "filename": "sample-1.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/sample.whl",
            "digests": {"sha256": hashlib.sha256(wrong).hexdigest()}}
    metadata = {"info": {"name": "sample", "version": "1.0"}, "urls": [item]}
    routes = Routes({"https://pypi.org/pypi/sample/json": (
        200, {}, json.dumps(metadata).encode()), item["url"]: (200, {}, wrong)})

    mismatch = PackageDiscovery(tmp_path / "mismatch", fetcher=routes).discover("pypi", "sample")
    item["yanked"] = "broken release"
    yanked_routes = Routes({"https://pypi.org/pypi/sample/json": (
        200, {}, json.dumps(metadata).encode())})
    yanked = PackageDiscovery(tmp_path / "yanked", fetcher=yanked_routes).discover("pypi", "sample")

    assert mismatch["outcome"] == "unverified"
    assert "identity or version" in mismatch["unresolved"][0]
    assert yanked["outcome"] == "unverified"
    assert yanked["yanked"] is True
    assert yanked["verification_complete"] is True


@pytest.mark.parametrize(("status", "outcome"), [(404, "not_published"), (410, "not_published"),
                                                 (429, "temporarily_unavailable"), (503, "temporarily_unavailable")])
def test_authoritative_missing_is_distinct_from_temporary_failure(tmp_path, status, outcome):
    routes = Routes({"https://registry.npmjs.org/missing/latest": (
        status, {"Retry-After": "30"}, b"")})

    result = PackageDiscovery(tmp_path, fetcher=routes).discover("npm", "missing")

    assert result["outcome"] == outcome
    if status >= 429:
        assert result["retry_after"] == "30"


def test_unsupported_ecosystem_does_not_touch_network(tmp_path):
    result = PackageDiscovery(tmp_path, fetcher=lambda *_: pytest.fail("network used")).discover(
        "github-release", "owner/project")
    assert result["outcome"] == "unsupported"


def test_redirect_to_unapproved_artifact_host_fails_before_following(tmp_path):
    metadata = {"name": "sample", "version": "1.0", "dist": {
        "tarball": "https://registry.npmjs.org/sample/-/sample.tgz", "shasum": "0" * 40}}
    routes = Routes({
        "https://registry.npmjs.org/sample/latest": (200, {}, json.dumps(metadata).encode()),
        metadata["dist"]["tarball"]: (302, {"Location": "https://example.com/evil.tgz"}, b""),
    })

    result = PackageDiscovery(tmp_path, fetcher=routes).discover("npm", "sample")

    assert result["outcome"] == "unverified"
    assert "not allowed" in result["unresolved"][0]
    assert len(routes.requests) == 2


def test_oversized_metadata_and_timeout_fail_without_absence_claim(tmp_path):
    oversized = Routes({"https://registry.npmjs.org/sample/latest": (
        200, {}, b"x" * (MAX_METADATA_BYTES + 1))})
    timeout = lambda *_: (_ for _ in ()).throw(AcquisitionError("timed out"))

    unsafe = PackageDiscovery(tmp_path / "large", fetcher=oversized).discover("npm", "sample")
    unavailable = PackageDiscovery(tmp_path / "timeout", fetcher=timeout).discover("npm", "sample")

    assert unsafe["outcome"] == "unverified"
    assert "exceeds" in unsafe["unresolved"][0]
    assert unavailable["outcome"] == "temporarily_unavailable"
    assert unavailable["unresolved"] == ["timed out"]


def test_cache_coalesces_hits_and_conditionally_revalidates(tmp_path):
    current = [1000.0]
    artifact = npm_tar({"name": "sample", "version": "1.0", "bin": "cli.js"},
                       {"package/cli.js": b""})
    metadata = {"name": "sample", "version": "1.0", "dist": {
        "tarball": "https://registry.npmjs.org/sample/-/sample.tgz",
        "shasum": hashlib.sha1(artifact).hexdigest(),
    }}
    def metadata_route(request):
        if request.headers.get("If-none-match") == '"v1"':
            return 304, {"ETag": '"v1"'}, b""
        return 200, {"ETag": '"v1"'}, json.dumps(metadata).encode()
    routes = Routes({"https://registry.npmjs.org/sample/latest": metadata_route,
                     metadata["dist"]["tarball"]: (200, {}, artifact)})
    engine = PackageDiscovery(tmp_path, fetcher=routes, clock=lambda: current[0])

    first = engine.discover("npm", "sample")
    second = engine.discover("npm", "sample")
    current[0] += LATEST_TTL_SECONDS + 1
    third = engine.discover("npm", "sample")

    assert first["cache"]["status"] == "miss"
    assert second["cache"]["status"] == "fresh"
    assert third["cache"]["status"] == "revalidated"
    assert len(routes.requests) == 3  # metadata + artifact, then metadata 304


def test_concurrent_duplicate_queries_share_one_acquisition(tmp_path):
    artifact = npm_tar({"name": "sample", "version": "1.0", "bin": "cli.js"},
                       {"package/cli.js": b""})
    metadata = {"name": "sample", "version": "1.0", "dist": {
        "tarball": "https://registry.npmjs.org/sample/-/sample.tgz",
        "shasum": hashlib.sha1(artifact).hexdigest(),
    }}
    routes = Routes({"https://registry.npmjs.org/sample/latest": (200, {}, json.dumps(metadata).encode()),
                     metadata["dist"]["tarball"]: (200, {}, artifact)})
    engine = PackageDiscovery(tmp_path, fetcher=routes)
    results = []
    threads = [threading.Thread(target=lambda: results.append(engine.discover("npm", "sample"))) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()

    assert len(results) == 4
    assert len(routes.requests) == 2
    assert {item["outcome"] for item in results} == {"verified_entry_point"}


def test_expired_latest_observation_moves_to_new_registry_release(tmp_path):
    current = [1000.0]
    release = ["1.0"]

    def metadata_route(_request):
        version = release[0]
        artifact = npm_tar({"name": "sample", "version": version, "bin": "cli.js"},
                           {"package/cli.js": b""})
        return 200, {}, json.dumps({
            "name": "sample", "version": version, "dist": {
                "tarball": f"https://registry.npmjs.org/sample/-/sample-{version}.tgz",
                "shasum": hashlib.sha1(artifact).hexdigest(),
            },
        }).encode()

    def fetch(request, _timeout, _limit):
        if request.full_url.endswith("/latest"):
            status, headers, body = metadata_route(request)
        else:
            version = release[0]
            body = npm_tar({"name": "sample", "version": version, "bin": "cli.js"},
                           {"package/cli.js": b""})
            status, headers = 200, {}
        return Response(status, headers, body, request.full_url)

    engine = PackageDiscovery(tmp_path, fetcher=fetch, clock=lambda: current[0])
    first = engine.discover("npm", "sample")
    release[0] = "2.0"
    current[0] += LATEST_TTL_SECONDS + 1
    second = engine.discover("npm", "sample")

    assert first["version"] == "1.0"
    assert second["version"] == "2.0"


def test_cli_returns_the_shared_engine_contract(monkeypatch, capsys):
    expected = {"contract_version": "package-discovery-v1", "outcome": "verified_entry_point"}
    class FakeEngine:
        def __init__(self, *_args, **_kwargs): pass
        def discover(self, *_args, **_kwargs): return expected
    monkeypatch.setattr(cli, "PackageDiscovery", FakeEngine)

    cli.main(["discover-package", "npm", "sample", "--version", "1.0"])

    assert json.loads(capsys.readouterr().out) == {**expected, "reservation": {
        "status": "unavailable", "reason": "intake_repository_not_configured"}}


@pytest.mark.live
@pytest.mark.parametrize(("ecosystem", "package", "version", "command"), [
    ("npm", "typescript", "5.9.3", "tsc"),
    ("pypi", "global-executables", "1.4.1", "global-executables"),
])
def test_pinned_public_package_smoke(tmp_path, ecosystem, package, version, command):
    result = PackageDiscovery(tmp_path).discover(ecosystem, package, version, command)
    assert result["outcome"] == "verified_entry_point", result
    assert result["version"] == version
    assert command in {item["command"] for item in result["commands"]}
