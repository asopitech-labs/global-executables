"""Read-only MCP and HTTP transports over a checked-out JSON dataset."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
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

ExecutableName = Annotated[str, Field(description=(
    "Exact, case-sensitive executable command name, not a package name or filesystem path; "
    "must be 1-255 characters without path separators or control characters."
))]
ExecutableNames = Annotated[list[str], Field(description=(
    "Executable command names to process in one request. Each value follows the same exact, "
    "case-sensitive rules as name, and results preserve input order."
))]
ProviderScope = Annotated[dict[str, str] | None, Field(description=(
    "Optional provider filters: source_type, package_system, distribution_family, distribution, "
    "distribution_release, language, registry, or ecosystem. All supplied key-value pairs must "
    "match; scoped negative results remain non-exhaustive."
))]
TargetEnvironment = Annotated[dict[str, Any], Field(description=(
    "Target installation context. Set package_manager to apt, brew, npm, or pip. Depending on the "
    "manager, also provide os, distribution, distribution_release, architecture, runtime_version, "
    "install_scope (user or project), virtualenv_bin, and has_admin as requested by the result."
))]
InstallPreferences = Annotated[dict[str, Any] | None, Field(description=(
    "Optional provider-selection preferences. Use package to select an exact package when more "
    "than one indexed provider exposes the command."
))]
SearchPrefix = Annotated[str, Field(description=(
    "Case-sensitive command-name prefix. Leave empty to search all indexed names subject to the "
    "other filters and limit."
))]
SearchLength = Annotated[int | None, Field(description=(
    "Optional exact character length of the executable name."
))]
SearchEcosystem = Annotated[str | None, Field(description=(
    "Optional exact provider ecosystem identifier, for example npm, pypi, homebrew, debian, or ubuntu."
))]
ResultLimit = Annotated[int, Field(description=(
    "Maximum number of sorted results to return."
))]


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
    def check_executable(name: ExecutableName, scope: ProviderScope = None) -> dict[str, Any]:
        """Check one proposed command name for an indexed executable collision.

        Use this exact, case-sensitive lookup for one candidate; use check_executables for several.
        Do not use it for fuzzy discovery; use search_similar_executables for confusingly close names.
        The tool only reads the published dataset and never runs or installs software. A collision
        returns found=true with providers. A miss is clear only when coverage_scope is exhaustive;
        otherwise status=unknown, so inspect coverage and warnings before adopting the name.
        """
        return dataset.check(name, scope)

    @_tool(mcp, "Check executable names in batch")
    def check_executables(names: ExecutableNames, scope: ProviderScope = None) -> dict[str, Any]:
        """Check several proposed command names for indexed collisions in one request.

        Use this tool when comparing or validating multiple names; use check_executable for one.
        Do not use it to rank similar spellings; use search_similar_executables for that task.
        Results preserve input order and include the shared snapshot, coverage scope, and warnings.
        The lookup is read-only and never runs software. Treat each miss as unknown unless the
        returned coverage_scope is exhaustive, especially when a provider scope is supplied.
        """
        metadata = dataset.metadata
        return {"results": [dataset.check(name, scope) for name in names], "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "warnings": coverage_warnings(metadata)}

    @_tool(mcp, "Resolve installation for one executable")
    def resolve_installation(name: ExecutableName, environment: TargetEnvironment,
                             preferences: InstallPreferences = None) -> dict[str, Any]:
        """Resolve advisory install and invocation steps for one indexed command provider.

        Use this after a check or lookup when the target environment is known; use
        resolve_installations for several commands. The tool never executes the returned argv.
        Do not use it to decide whether a name is available; use check_executable for that decision.
        status explains whether a recipe is ready, ambiguous, unsupported, unverified, missing from
        the index, or needs more context; required_context names missing environment fields. Review
        candidates, verification, applicability, coverage, and warnings before running any step.
        """
        return _resolve_installation(dataset, name, environment, preferences)

    @_tool(mcp, "Resolve installations in batch")
    def resolve_installations(names: ExecutableNames, environment: TargetEnvironment,
                              preferences: InstallPreferences = None) -> dict[str, Any]:
        """Resolve advisory install and invocation steps for several commands on one target.

        Use this batch form when every command shares the same environment and preferences; use
        resolve_installation for one command. Do not use it for name availability; use
        check_executables instead. Results preserve input order. The tool never executes
        its argv. Inspect each result's status, required_context, candidates, verification, and
        warnings because indexed presence does not guarantee installability on the target.
        """
        return {"results": [_resolve_installation(dataset, name, environment, preferences) for name in names],
                "snapshot": dataset.metadata["snapshot"], "contract_version": "installation-v1",
                "warnings": coverage_warnings(dataset.metadata)}

    @_tool(mcp, "Get an executable record")
    def get_executable(name: ExecutableName, scope: ProviderScope = None) -> dict[str, Any]:
        """Return the canonical provider record for one exact command name, if indexed.

        Use this to inspect package, ecosystem, source, version, and provenance after finding a name;
        use check_executable when deciding whether a proposed name collides. record is null when no
        matching provider remains after scope filtering. Do not use a null record as an availability
        decision; check_executable supplies the required coverage and warning details. A null record
        is not proof of global absence; interpret it with coverage_scope and the snapshot context.
        """
        metadata = dataset.metadata
        return {"record": dataset.get(name, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "scope": scope}

    @_tool(mcp, "Search executable names")
    def search_executables(prefix: SearchPrefix = "", length: SearchLength = None,
                           ecosystem: SearchEcosystem = None, limit: ResultLimit = 100,
                           scope: ProviderScope = None) -> dict[str, Any]:
        """Discover indexed command names using prefix, length, ecosystem, and provider filters.

        Use this for name exploration or filtered listings; use check_executable for an exact
        collision decision and search_similar_executables for typo/confusion candidates. Filters are
        combined, results are deterministically sorted and capped by limit, and the tool is read-only.
        Do not use search results to infer exact-name availability.
        An empty result describes only the indexed snapshot and is not proof that no command exists.
        """
        metadata = dataset.metadata
        return {"executables": dataset.search(prefix, length, ecosystem, limit, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "scope": scope}

    @_tool(mcp, "Find similar executable names")
    def search_similar_executables(name: ExecutableName, limit: ResultLimit = 20,
                                   scope: ProviderScope = None) -> dict[str, Any]:
        """Find indexed command names that may be confused with a proposed name.

        Use this after an exact check to review typo and visual-similarity risk; it does not determine
        whether the exact name exists. Candidates come from trigram overlap and are ranked by edit
        distance, trigram similarity, and name, then capped by limit. matches report both metrics.
        Do not use it as an exact collision check; use check_executable for that decision.
        An empty match list is limited to the indexed snapshot and optional provider scope.
        """
        metadata = dataset.metadata
        return {"matches": dataset.similar(name, limit, scope), "snapshot": metadata["snapshot"], "coverage_scope": _scope(metadata, scope), "scope": scope}

    @_tool(mcp, "Get dataset coverage")
    def get_coverage() -> dict[str, Any]:
        """Explain the dataset snapshot, source coverage, freshness, and negative-result limits.

        Call this before treating any missing command as available, or when check/search warnings need
        interpretation. negative_lookup is exhaustive only when every declared source is complete;
        otherwise absence remains unknown. freshness may be unavailable because it is a separate
        partial scan. Do not use it to look up a command; use check_executable or check_executables.
        This read-only metadata tool takes no parameters.
        """
        return {**dataset.metadata, "negative_lookup": _scope(dataset.metadata), "freshness": dataset.freshness,
                "warnings": coverage_warnings(dataset.metadata)}

    @_tool(mcp, "Assess one executable name")
    def assess_executable(name: ExecutableName, scope: ProviderScope = None) -> dict[str, Any]:
        """Assess freshness, activity, popularity, and collision risk for one command name.

        Use this after checking existence when release age or observed usage affects a naming decision;
        use assess_executables for several names. The assessment derives from indexed provider facts
        and never probes live registries. unknown or insufficient_evidence means the snapshot lacks
        enough signals, not that the name is safe. Do not use the assessment as an existence check;
        use check_executable for that. Existence and coverage remain separate fields.
        """
        return assess(dataset, name, scope)

    @_tool(mcp, "Assess executable names in batch")
    def assess_executables(names: ExecutableNames, scope: ProviderScope = None) -> dict[str, Any]:
        """Assess freshness, activity, popularity, and collision risk for several command names.

        Use this batch form to compare candidates; use assess_executable for one. Results preserve
        input order and derive only from indexed provider facts, without live registry probes. Treat
        unknown or insufficient_evidence as missing evidence rather than safety, and use the returned
        coverage_scope plus each result's existence fields when making a naming decision. Do not use
        it as a batch existence check; use check_executables for that cheaper, simpler task.
        """
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
