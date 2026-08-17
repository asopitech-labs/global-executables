"""Probe the HTTP MCP service from the Codex test container."""

import asyncio
import json
import sys
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


EXPECTED_TOOLS = {
    "assess_executable",
    "assess_executables",
    "check_executable",
    "check_executables",
    "get_coverage",
    "get_executable",
    "search_executables",
    "search_similar_executables",
}


def read_health(url: str) -> dict:
    with urllib.request.urlopen(f"{url}/health", timeout=5) as response:
        return json.load(response)


async def probe(url: str) -> dict:
    health_url = url.removesuffix("/mcp")
    last_error = None
    for _ in range(60):
        try:
            health = read_health(health_url)
            break
        except OSError as error:
            last_error = error
            await asyncio.sleep(0.2)
    else:
        raise AssertionError(f"MCP server did not become ready: {last_error}")

    assert health["status"] == "ok"
    assert health["read_only"] is True

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Global Executables"

            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert tool_names == EXPECTED_TOOLS

            result = await session.call_tool(
                "check_executables", {"names": ["envcp", "evpk"]}
            )
            assert not result.isError
            payload = result.structuredContent
            assert payload["results"][0]["found"] is True
            assert payload["results"][1]["found"] is False

            coverage = await session.call_tool("get_coverage", {})
            assert not coverage.isError

            metadata = await session.read_resource("global-executables://metadata")
            metadata_payload = json.loads(metadata.contents[0].text)
            assert metadata_payload["snapshot"] == health["snapshot"]

            executable = await session.read_resource(
                "global-executables://executables/envcp"
            )
            assert json.loads(executable.contents[0].text)["command"] == "envcp"

    return {
        "health": health,
        "tools": sorted(EXPECTED_TOOLS),
        "checked": ["envcp", "evpk"],
        "resource": "global-executables://metadata",
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} http://mcp:8000/mcp")
    print(json.dumps(asyncio.run(probe(sys.argv[1])), sort_keys=True))


if __name__ == "__main__":
    main()
