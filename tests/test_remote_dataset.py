import hashlib
import json
import tomllib
import urllib.request
from pathlib import Path

import pytest

from global_executables import __version__
from global_executables.cli import main as cli_main
from global_executables.mcp_server import create_server, open_dataset
from global_executables.pipeline import rebuild
from global_executables.remote import DatasetUnavailableError, RemoteDataset
from global_executables.search import Dataset

ROOT = Path(__file__).parents[1]
BASE = "https://example.test/dictionary"


class FakeOrigin:
    """Serves a built dictionary like raw.githubusercontent: ETags, 304s, 404s."""

    def __init__(self, root: Path):
        self.root = root
        self.requests: list[tuple[str, str | None]] = []
        self.down = False

    def __call__(self, request: urllib.request.Request, timeout: float):
        relative = request.full_url.removeprefix(BASE + "/")
        etag_sent = request.get_header("If-none-match")
        self.requests.append((relative, etag_sent))
        if self.down:
            raise OSError("network is unreachable")
        path = self.root / relative
        if not path.is_file():
            return 404, {}, b""
        body = path.read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if etag_sent == etag:
            return 304, {"etag": etag}, b""
        return 200, {"etag": etag}, body

    def fetched(self, relative: str) -> list[str | None]:
        return [etag for path, etag in self.requests if path == relative]


@pytest.fixture
def origin(tmp_path):
    root = tmp_path / "origin"
    rebuild(root, sorted((ROOT / "fixtures/intermediate").glob("*.jsonl")), "2026-08-14")
    return FakeOrigin(root)


def remote(origin, tmp_path, **kwargs):
    return RemoteDataset(BASE, tmp_path / "cache", fetcher=origin, **kwargs)


def test_remote_lookup_matches_a_local_checkout_and_fetches_only_what_it_needs(origin, tmp_path):
    local = Dataset(origin.root)
    dataset = remote(origin, tmp_path)
    for name in ["envcp", "evpk", "git"]:
        assert dataset.check(name) == local.check(name)
    assert dataset.similar("envc") == local.similar("envc")
    assert dataset.search(prefix="env") == local.search(prefix="env")
    fetched = {path for path, _ in origin.requests}
    assert "data/metadata.json" in fetched
    assert not any(path.startswith("data/executables/gi/") and "git" not in path for path in fetched)
    assert len(origin.fetched("data/metadata.json")) == 1  # metadata is reused, not refetched per call


def test_unchanged_files_are_revalidated_with_their_etag(origin, tmp_path):
    clock = [0.0]
    remote(origin, tmp_path, clock=lambda: clock[0]).check("envcp")
    origin.requests.clear()
    # A new process (same cache) revalidates metadata with If-None-Match and reuses records.
    dataset = remote(origin, tmp_path, clock=lambda: clock[0])
    dataset.check("envcp")
    [metadata_etag] = origin.fetched("data/metadata.json")
    assert metadata_etag is not None
    assert not [p for p, _ in origin.requests if p.startswith("data/executables/")]


def test_a_new_metadata_generation_revalidates_cached_records_conditionally(origin, tmp_path):
    clock = [0.0]
    dataset = remote(origin, tmp_path, clock=lambda: clock[0], revalidate_seconds=10)
    dataset.check("envcp")
    metadata = origin.root / "data/metadata.json"
    value = json.loads(metadata.read_text()); value["snapshot"] = "2026-08-15"
    metadata.write_text(json.dumps(value))
    origin.requests.clear()
    clock[0] = 5
    assert dataset.check("envcp")["snapshot"] == "2026-08-14"  # within the revalidation window
    assert origin.requests == []
    clock[0] = 11
    assert dataset.check("envcp")["snapshot"] == "2026-08-15"
    record_requests = [etag for path, etag in origin.requests if path.startswith("data/executables/")]
    assert record_requests and all(etag is not None for etag in record_requests)


def test_absent_names_are_cached_as_absent(origin, tmp_path):
    dataset = remote(origin, tmp_path)
    assert dataset.get("definitely-not-a-command") is None
    origin.requests.clear()
    assert dataset.get("definitely-not-a-command") is None
    assert origin.requests == []


def test_offline_fallback_serves_the_cache_and_fails_clearly_without_one(origin, tmp_path):
    remote(origin, tmp_path).check("envcp")
    origin.down = True
    dataset = remote(origin, tmp_path)
    assert dataset.check("envcp")["found"] is True
    with pytest.raises(DatasetUnavailableError, match="not cached"):
        dataset.check("evpk")
    origin.requests.clear()
    offline = remote(origin, tmp_path, offline=True)
    assert offline.check("envcp")["found"] is True
    assert origin.requests == []
    with pytest.raises(DatasetUnavailableError):
        RemoteDataset(BASE, tmp_path / "empty-cache", fetcher=origin).metadata


def test_a_stale_cached_index_is_fetched_again_in_full(origin, tmp_path):
    dataset = remote(origin, tmp_path)
    expected = dataset.search(prefix="env")
    cached = next((dataset.cache.root / "data/indexes/prefix").glob("*.json"))
    cached.write_text("[]")
    origin.requests.clear()
    assert dataset.search(prefix="env") == expected
    assert any(etag is None for path, etag in origin.requests if path.startswith("data/indexes/"))


def test_unsafe_relative_paths_are_rejected(origin, tmp_path):
    with pytest.raises(ValueError):
        remote(origin, tmp_path).cache.get("../etc/passwd", None)


def test_dataset_source_selection(origin, tmp_path, monkeypatch):
    assert type(open_dataset(tmp_path, origin.root)) is Dataset
    assert type(open_dataset(origin.root)) is Dataset  # a root that holds data/metadata.json
    assert isinstance(open_dataset(tmp_path / "empty"), RemoteDataset)
    with pytest.raises(FileNotFoundError):
        open_dataset(tmp_path, tmp_path / "missing").metadata


async def test_server_over_remote_dataset_reports_its_version_and_read_only_tools(origin, tmp_path, monkeypatch):
    monkeypatch.setattr("global_executables.mcp_server.RemoteDataset",
                        lambda url, cache_dir, offline=False: RemoteDataset(BASE, tmp_path / "cache", fetcher=origin))
    server = create_server(tmp_path / "no-checkout")
    assert server._mcp_server.version == __version__
    tools = await server.list_tools()
    assert tools and all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)
    schema = await server.read_resource("global-executables://schema/executable")
    assert json.loads(schema[0].content)["title"] == "Canonical executable record"


def test_cli_mcp_subcommand_forwards_its_arguments(monkeypatch):
    seen = []
    monkeypatch.setattr("global_executables.mcp_server.main", lambda argv=None: seen.append(argv))
    cli_main(["mcp", "--offline", "--transport", "stdio"])
    assert seen == [["--offline", "--transport", "stdio"]]


def test_versions_and_packaging_metadata_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]["schema"] == "global_executables/schema"
    assert project["project"]["license"] == "MIT"
    server = json.loads((ROOT / "server.json").read_text())
    assert server["version"] == __version__
    assert server["name"] == "io.github.asopitech-labs/global-executables"
    for package in server["packages"]:
        version = package["identifier"].rsplit(":", 1)[1] if package["registryType"] == "oci" else package["version"]
        assert version == __version__
    assert f"mcp-name: {server['name']}" in (ROOT / "README.md").read_text()
    assert f'io.modelcontextprotocol.server.name="{server["name"]}"' in (ROOT / "Dockerfile.mcp").read_text()


def test_registry_metadata_keeps_browser_launch_zero_configuration():
    server = json.loads((ROOT / "server.json").read_text())
    pypi_package = next(package for package in server["packages"] if package["registryType"] == "pypi")

    assert "environmentVariables" not in pypi_package
