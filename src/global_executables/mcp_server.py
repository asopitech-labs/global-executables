"""Read-only MCP and HTTP transports over a checked-out JSON dataset."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .assessment import assess
from .search import Dataset


def _scope(metadata: dict[str, Any]) -> str:
    coverage = metadata.get("coverage", {})
    if metadata.get("negative_lookup") == "exhaustive" and coverage and all(
        value.get("status") == "success" and value.get("coverage_kind") == "exhaustive"
        for value in coverage.values()
    ):
        return "exhaustive"
    return "unknown"


def create_server(
    root: str | Path,
    *,
    dataset_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    allowed_hosts: list[str] | None = None,
) -> FastMCP:
    root = Path(root).resolve()
    dataset_root = Path(dataset_root).resolve() if dataset_root is not None else root
    dataset = Dataset(dataset_root)
    dataset.metadata  # Fail at startup if the checkout has no dataset.
    transport_security = None
    if allowed_hosts is None and host not in {"127.0.0.1", "localhost", "::1"}:
        allowed_hosts = [f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"]
    if allowed_hosts is not None:
        transport_security = TransportSecuritySettings(allowed_hosts=allowed_hosts)
    mcp = FastMCP("Global Executables", instructions=(
        "Read-only executable collision lookup over the published Git JSON dataset. "
        "Absence is clear_in_index only for explicitly exhaustive snapshots; otherwise unknown."
    ), host=host, port=port, json_response=True, transport_security=transport_security)

    @mcp.tool()
    def check_executable(name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Check one exact, case-sensitive executable name."""
        return dataset.check(name, scope)

    @mcp.tool()
    def check_executables(names: list[str], scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Batch-check naming candidates; this is the preferred agent path."""
        metadata = dataset.metadata
        return {"results": [dataset.check(name, scope) for name in names], "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata)}

    @mcp.tool()
    def get_executable(name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Return one canonical record, with snapshot context, if it exists."""
        metadata = dataset.metadata
        return {"record": dataset.get(name, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata), "scope": scope}

    @mcp.tool()
    def search_executables(prefix: str = "", length: int | None = None, ecosystem: str | None = None, limit: int = 100, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Search derived indexes by prefix, length, and/or ecosystem."""
        metadata = dataset.metadata
        return {"executables": dataset.search(prefix, length, ecosystem, limit, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata), "scope": scope}

    @mcp.tool()
    def search_similar_executables(name: str, limit: int = 20, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Find typo/confusion candidates using trigram postings and edit distance."""
        metadata = dataset.metadata
        return {"matches": dataset.similar(name, limit, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata), "scope": scope}

    @mcp.tool()
    def get_coverage() -> dict[str, Any]:
        """Return snapshot provenance and negative-query completeness."""
        return {**dataset.metadata, "freshness": dataset.freshness}

    @mcp.tool()
    def assess_executable(name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Assess factual freshness/activity signals separately from existence."""
        return assess(dataset, name, scope)

    @mcp.tool()
    def assess_executables(names: list[str], scope: dict[str, str] | None = None) -> dict[str, Any]:
        """Batch-assess candidate names while preserving the simple check contract."""
        return {"results": [assess(dataset, name, scope) for name in names],
                "snapshot": dataset.metadata["snapshot"], "coverage_scope": _scope(dataset.metadata)}

    @mcp.resource("global-executables://metadata", mime_type="application/json")
    def metadata_resource() -> str:
        return json.dumps(dataset.metadata)

    @mcp.resource("global-executables://coverage", mime_type="application/json")
    def coverage_resource() -> str:
        metadata = dataset.metadata
        return json.dumps({"snapshot": metadata["snapshot"], "negative_lookup": _scope(metadata),
                           "checked_sources": metadata.get("checked_sources", []), "coverage": metadata.get("coverage", {}),
                           "freshness": dataset.freshness})

    @mcp.resource("global-executables://schema/{name}", mime_type="application/schema+json")
    def schema_resource(name: str) -> str:
        if name not in {"executable", "provider", "intermediate", "metadata"}:
            raise ValueError(f"unknown schema: {name}")
        return (root / "schema" / f"{name}.schema.json").read_text()

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
    ).streamable_http_app()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.getenv("GLOBAL_EXECUTABLES_ROOT", Path.cwd()))
    parser.add_argument(
        "--dataset-root",
        default=os.getenv("GLOBAL_EXECUTABLES_DATASET_ROOT"),
        help="checkout or extracted archive of the dictionary branch; defaults to --root",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allowed-host", action="append", default=None)
    args = parser.parse_args()
    server = create_server(
        args.root,
        dataset_root=args.dataset_root,
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
