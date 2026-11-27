"""Run real CLI and MCP protocol discovery on the designated Actions runner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _require_reservation(label: str, result: dict) -> dict:
    if result.get("outcome") != "verified_entry_point" or not result.get("verification_complete"):
        raise RuntimeError(f"{label} did not verify a published entry point: {result.get('outcome')}")
    reservation = result.get("reservation") or {}
    if reservation.get("status") not in {"reserved", "already_processed"}:
        raise RuntimeError(f"{label} could not confirm a Git reservation: {reservation}")
    return {"outcome": result["outcome"], "package": result.get("canonical_package"),
            "version": result.get("version"), "commands": result.get("commands"),
            "reservation": reservation}


async def _mcp(ecosystem: str, package: str, version: str | None, cache: Path) -> dict:
    parameters = StdioServerParameters(command=sys.executable, env=dict(os.environ), args=[
        "-m", "global_executables.mcp_server", "--root", str(Path.cwd()),
        "--cache-dir", str(cache), "--intake-repository", str(Path.cwd()),
    ])
    arguments = {"ecosystem": ecosystem, "package": package}
    if version:
        arguments["version"] = version
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            reply = await session.call_tool("discover_published_package", arguments)
    if reply.isError or not isinstance(reply.structuredContent, dict):
        raise RuntimeError("MCP protocol discovery failed")
    return reply.structuredContent


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ecosystem", choices=("npm", "pypi"), required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--version", default="")
    args = parser.parse_args(argv)
    cache = Path(os.environ.get("GLOBAL_EXECUTABLES_CACHE_DIR", "/tmp/global-executables-cache"))
    version = args.version or None
    command = [sys.executable, "-m", "global_executables.cli", "discover-package",
               args.ecosystem, args.package, "--cache-dir", str(cache / "cli"),
               "--intake-repository", str(Path.cwd())]
    if version:
        command.extend(("--version", version))
    cli = subprocess.run(command, text=True, capture_output=True, timeout=360, check=True)
    cli_result = json.loads(cli.stdout)
    mcp_result = asyncio.run(_mcp(args.ecosystem, args.package, version, cache / "mcp"))
    summaries = {"cli": _require_reservation("CLI", cli_result),
                 "mcp": _require_reservation("MCP", mcp_result)}
    if (summaries["cli"]["commands"] != summaries["mcp"]["commands"] or
            summaries["cli"]["reservation"]["reservation_id"] !=
            summaries["mcp"]["reservation"]["reservation_id"]):
        raise RuntimeError("CLI and MCP did not reserve the same verified release")
    print(json.dumps(summaries, sort_keys=True))


if __name__ == "__main__":
    main()
