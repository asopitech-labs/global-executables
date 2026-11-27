"""Discovery reserves verified releases only on an explicitly configured runner."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from global_executables import cli, mcp_server


VERIFIED = {"contract_version": "package-discovery-v1", "outcome": "verified_entry_point",
            "ecosystem": "npm", "package": "sample", "version": "1.0",
            "commands": [{"command": "sample", "entry_point": "bin/sample.js"}]}


class Discovery:
    def __init__(self, *args, **kwargs):
        pass

    def discover(self, *args, **kwargs):
        return dict(VERIFIED)


def test_cli_reports_unavailable_without_git_runner(monkeypatch, capsys):
    monkeypatch.delenv("GLOBAL_EXECUTABLES_INTAKE_REPOSITORY", raising=False)
    monkeypatch.setattr(cli, "PackageDiscovery", Discovery)
    cli.main(["discover-package", "npm", "sample", "--version", "1.0"])
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "verified_entry_point"
    assert result["reservation"] == {
        "status": "unavailable", "reason": "intake_repository_not_configured"}


def test_cli_runner_reserves_only_verified_observations(monkeypatch, tmp_path, capsys):
    calls = []
    def reserve(observation, repository, *, remote):
        calls.append((observation["version"], repository, remote))
        return {"status": "reserved", "reservation_id": "abc", "branch": "intake/abc"}

    monkeypatch.setattr(cli, "PackageDiscovery", Discovery)
    monkeypatch.setitem(sys.modules, "global_executables.git_intake",
                        SimpleNamespace(reserve_observation=reserve))
    monkeypatch.setenv("GLOBAL_EXECUTABLES_INTAKE_REPOSITORY", str(tmp_path))
    cli.main(["discover-package", "npm", "sample", "--intake-remote", "intake"])
    result = json.loads(capsys.readouterr().out)
    assert result["reservation"]["status"] == "reserved"
    assert calls == [("1.0", tmp_path, "intake")]

    class NotVerified(Discovery):
        def discover(self, *args, **kwargs):
            return {"outcome": "unverified"}
    monkeypatch.setattr(cli, "PackageDiscovery", NotVerified)
    cli.main(["discover-package", "npm", "sample"])
    result = json.loads(capsys.readouterr().out)
    assert result["reservation"]["status"] == "not_eligible"
    assert calls == [("1.0", tmp_path, "intake")]


async def test_mcp_discovery_reserves_but_lookup_remains_read_only(monkeypatch, tmp_path):
    class Dataset:
        metadata = {"snapshot": "fixture"}

        def check(self, name, scope=None):
            return {"name": name, "snapshot": "fixture"}

    calls = []
    def reserve(observation, repository, *, remote):
        calls.append((observation["version"], repository, remote))
        return {"status": "reserved", "reservation_id": "abc"}

    monkeypatch.setattr(mcp_server, "open_dataset", lambda *args, **kwargs: Dataset())
    monkeypatch.setitem(sys.modules, "global_executables.git_intake",
                        SimpleNamespace(reserve_observation=reserve))
    server = mcp_server.create_server(tmp_path, discovery_engine=Discovery(),
                                      intake_repository=tmp_path, intake_remote="intake")
    _, discovery = await server.call_tool("discover_published_package", {
        "ecosystem": "npm", "package": "sample"})
    assert discovery["reservation"]["status"] == "reserved"
    _, lookup = await server.call_tool("check_executable", {"name": "sample"})
    assert lookup == {"name": "sample", "snapshot": "fixture"}
    assert calls == [("1.0", tmp_path, "intake")]


async def test_mcp_without_designated_runner_reports_reservation_unavailable(monkeypatch, tmp_path):
    class Dataset:
        metadata = {"snapshot": "fixture"}

    monkeypatch.delenv("GLOBAL_EXECUTABLES_INTAKE_REPOSITORY", raising=False)
    monkeypatch.setattr(mcp_server, "open_dataset", lambda *args, **kwargs: Dataset())
    server = mcp_server.create_server(tmp_path, discovery_engine=Discovery())
    _, result = await server.call_tool("discover_published_package", {
        "ecosystem": "npm", "package": "sample"})
    assert result["outcome"] == "verified_entry_point"
    assert result["reservation"] == {
        "status": "unavailable", "reason": "intake_repository_not_configured"}
