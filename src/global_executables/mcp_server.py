"""Read-only MCP and HTTP transports over a checked-out JSON dataset."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .assessment import assess
from .coverage import coverage_scope as _scope, coverage_warnings
from .installation import resolve_installation as _resolve_installation
from .remote import DEFAULT_DATASET_URL, RemoteDataset
from .search import Dataset

SCHEMAS = {"executable", "provider", "intermediate", "metadata"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def open_dataset(
    root: str | Path,
    dataset_root: str | Path | None = None,
    *,
    dataset_url: str | None = None,
    cache_dir: str | Path | None = None,
    offline: bool = False,
) -> Dataset:
    """Choose the dataset source.

    An explicit ``dataset_root`` is always used as is and fails closed when it is
    missing. Otherwise a ``dataset_url`` selects remote access; failing that, a
    ``root`` that already holds ``data/metadata.json`` is used locally, and with
    neither the public dictionary branch is read over HTTPS with a local cache.
    """
    if dataset_root is not None:
        return Dataset(Path(dataset_root).resolve())
    if dataset_url:
        return RemoteDataset(dataset_url, cache_dir, offline=offline)
    root = Path(root).resolve()
    if (root / "data" / "metadata.json").is_file():
        return Dataset(root)
    return RemoteDataset(DEFAULT_DATASET_URL, cache_dir, offline=offline)


def schema_text(root: Path, name: str) -> str:
    """Read a bundled JSON Schema, preferring a source checkout's copy."""
    if name not in SCHEMAS:
        raise ValueError(f"unknown schema: {name}")
    package = Path(__file__).parent
    candidates = (
        root / "schema",              # --root pointing at a source checkout
        package / "schema",           # installed wheel (force-included at build time)
        package.parents[1] / "schema",  # editable install from a source checkout
    )
    for directory in candidates:
        path = directory / f"{name}.schema.json"
        if path.is_file():
            return path.read_text()
    raise FileNotFoundError(f"schema not bundled: {name}")


READ_ONLY = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _tool(mcp: FastMCP, title: str):
    return mcp.tool(title=title, annotations=ToolAnnotations(title=title, **READ_ONLY))


def create_server(
    root: str | Path,
    *,
    dataset_root: str | Path | None = None,
    dataset_url: str | None = None,
    cache_dir: str | Path | None = None,
    offline: bool = False,
    host: str = "127.0.0.1",
    port: int = 8000,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    root = Path(root).resolve()
    dataset = open_dataset(root, dataset_root, dataset_url=dataset_url, cache_dir=cache_dir, offline=offline)
    dataset.metadata  # Fail at startup if the checkout has no dataset.
    transport_security = None
    if allowed_hosts is None and host not in {"127.0.0.1", "localhost", "::1"}:
        allowed_hosts = [f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"]
    if allowed_hosts is not None:
        transport_security = TransportSecuritySettings(allowed_hosts=allowed_hosts)
    mcp = FastMCP("Global Executables", instructions=(
        "Read-only executable collision lookup over the published Git JSON dataset. "
        "Absence is clear_in_index only for explicitly exhaustive snapshots; otherwise unknown. "
        "Read result warnings and coverage; a missing result is not permission to adopt a name."
    ), website_url="https://asopitech-labs.github.io/global-executables/",
        host=host, port=port, json_response=True, transport_security=transport_security)
    # FastMCP has no version argument; without this the SDK's own version is reported.
    mcp._mcp_server.version = __version__

    @_tool(mcp, "Check one executable name")
    def check_executable(name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Check one exact, case-sensitive executable name."""
        return dataset.check(name, scope)

    @_tool(mcp, "Check executable names in batch")
    def check_executables(names: list[str], scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Batch-check naming candidates; this is the preferred agent path."""
        metadata = dataset.metadata
        return {"results": [dataset.check(name, scope) for name in names], "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "warnings": coverage_warnings(metadata)}

    @_tool(mcp, "Resolve installation for one executable")
    def resolve_installation(name: str, environment: dict[str, Any], preferences: dict[str, Any] | None = None) -> dict[str, Any]:
        """Give conservative, environment-specific install and invoke steps."""
        return _resolve_installation(dataset, name, environment, preferences)

    @_tool(mcp, "Resolve installations in batch")
    def resolve_installations(names: list[str], environment: dict[str, Any], preferences: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve several commands against the same target environment."""
        return {"results": [_resolve_installation(dataset, name, environment, preferences) for name in names],
                "snapshot": dataset.metadata["snapshot"], "contract_version": "installation-v1",
                "warnings": coverage_warnings(dataset.metadata)}

    @_tool(mcp, "Get an executable record")
    def get_executable(name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Return one canonical record, with snapshot context, if it exists."""
        metadata = dataset.metadata
        return {"record": dataset.get(name, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "scope": scope}

    @_tool(mcp, "Search executable names")
    def search_executables(prefix: str = "", length: int | None = None, ecosystem: str | None = None, limit: int = 100, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Search derived indexes by prefix, length, and/or ecosystem."""
        metadata = dataset.metadata
        return {"executables": dataset.search(prefix, length, ecosystem, limit, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "scope": scope}

    @_tool(mcp, "Find similar executable names")
    def search_similar_executables(name: str, limit: int = 20, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Find typo/confusion candidates using trigram postings and edit distance."""
        metadata = dataset.metadata
        return {"matches": dataset.similar(name, limit, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "scope": scope}

    @_tool(mcp, "Get dataset coverage")
    def get_coverage() -> dict[str, Any]:
        """Return snapshot provenance and negative-query completeness."""
        return {**dataset.metadata, "negative_lookup": _scope(dataset.metadata), "freshness": dataset.freshness,
                "warnings": coverage_warnings(dataset.metadata)}

    @_tool(mcp, "Assess one executable name")
    def assess_executable(name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Assess factual freshness/activity signals separately from existence."""
        return assess(dataset, name, scope)

    @_tool(mcp, "Assess executable names in batch")
    def assess_executables(names: list[str], scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Batch-assess candidate names while preserving the simple check contract."""
        return {"results": [assess(dataset, name, scope) for name in names],
                "snapshot": dataset.metadata["snapshot"], "coverage_scope": _scope(dataset.metadata, scope)}

    @mcp.resource("global-executables://metadata", mime_type="application/json")
    def metadata_resource() -> str:
        return json.dumps(dataset.metadata)

    @mcp.resource("global-executables://coverage", mime_type="application/json")
    def coverage_resource() -> str:
        metadata = dataset.metadata
        return json.dumps({"snapshot": metadata["snapshot"], "negative_lookup": _scope(metadata),
                           "checked_sources": metadata.get("checked_sources", []), "coverage": metadata.get("coverage", {}),
                           "freshness": dataset.freshness, "warnings": coverage_warnings(metadata)})

    @mcp.resource("global-executables://schema/{name}", mime_type="application/schema+json")
    def schema_resource(name: str) -> str:
        return schema_text(root, name)

    @mcp.resource("global-executables://executables/{name}", mime_type="application/json")
    def executable_resource(name: str) -> str:
        record = dataset.get(name)
        if record is None:
            raise ValueError(f"executable not found: {name}")
        return json.dumps(record)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        metadata = dataset.metadata
        return JSONResponse({"status": "ok", "service_version": __version__, "snapshot": metadata["snapshot"],
                             "coverage_scope": _scope(metadata), "read_only": True})

    return mcp


def create_app():
    """ASGI factory for uvicorn and container deployments."""
    return create_server(
        os.getenv("GLOBAL_EXECUTABLES_ROOT", Path.cwd()),
        dataset_root=os.getenv("GLOBAL_EXECUTABLES_DATASET_ROOT"),
        dataset_url=os.getenv("GLOBAL_EXECUTABLES_DATASET_URL"),
        cache_dir=os.getenv("GLOBAL_EXECUTABLES_CACHE_DIR"),
        offline=_truthy(os.getenv("GLOBAL_EXECUTABLES_OFFLINE")),
    ).streamable_http_app()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="global-executables mcp",
        description="Read-only MCP server over the Global Executables dataset. With no local "
                    "dataset, files are fetched on demand from the public dictionary branch and cached.")
    parser.add_argument("--root", default=os.getenv("GLOBAL_EXECUTABLES_ROOT", Path.cwd()))
    parser.add_argument(
        "--dataset-root",
        default=os.getenv("GLOBAL_EXECUTABLES_DATASET_ROOT"),
        help="checkout or extracted archive of the dictionary branch; when unset, --root is used "
             "if it holds data/metadata.json, otherwise the published dataset is fetched over HTTPS",
    )
    parser.add_argument("--dataset-url", default=os.getenv("GLOBAL_EXECUTABLES_DATASET_URL"),
                        help=f"raw-file base URL of a published dictionary (default {DEFAULT_DATASET_URL})")
    parser.add_argument("--cache-dir", default=os.getenv("GLOBAL_EXECUTABLES_CACHE_DIR"),
                        help="cache for fetched files (default: the per-user cache directory)")
    parser.add_argument("--offline", action="store_true", default=_truthy(os.getenv("GLOBAL_EXECUTABLES_OFFLINE")),
                        help="serve only previously cached files; never touch the network")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allowed-host", action="append", default=None)
    args = parser.parse_args(argv)
    server = create_server(
        args.root,
        dataset_root=args.dataset_root,
        dataset_url=args.dataset_url,
        cache_dir=args.cache_dir,
        offline=args.offline,
        host=args.host,
        port=args.port,
        allowed_hosts=args.allowed_host,
    )
    if args.transport == "streamable-http":
        server.settings.host = args.host
        server.settings.port = args.port
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
