import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from global_executables.mcp_server import create_server
from global_executables.pipeline import rebuild

ROOT = Path(__file__).parents[1]
SNAPSHOT = "2026-08-14"
EXPECTED_TOOLS = {"check_executable", "check_executables", "get_executable", "search_executables", "search_similar_executables", "get_coverage", "assess_executable", "assess_executables"}


@pytest.fixture(scope="module")
def dictionary_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("mcp-dictionary")
    rebuild(root, sorted((ROOT / "fixtures/intermediate").glob("*.jsonl")), SNAPSHOT)
    return root


async def _assert_protocol(session: ClientSession):
    initialized = await session.initialize()
    assert initialized.serverInfo.name == "Global Executables"
    tools = await session.list_tools()
    assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
    assert not any("write" in tool.name or "update" in tool.name for tool in tools.tools)

    batch = await session.call_tool("check_executables", {"names": ["envcp", "evpk"]})
    assert not batch.isError
    payload = batch.structuredContent
    assert [item["status"] for item in payload["results"]] == ["collision", "unknown"]
    assert payload["results"][1]["found"] is False
    assert payload["results"][1]["absence"]["status"] == "not_found_in_current_index"
    assert payload["snapshot"] == SNAPSHOT and payload["coverage_scope"] == "unknown"
    assessment = await session.call_tool("assess_executables", {"names": ["envcp", "evpk"]})
    assert not assessment.isError
    assert assessment.structuredContent["results"][0]["found"] is True
    coverage = await session.call_tool("get_coverage", {})
    assert not coverage.isError
    assert coverage.structuredContent["freshness"]["status"] == "unavailable"
    unknown_tool = await session.call_tool("does_not_exist", {})
    assert unknown_tool.isError

    resources = await session.list_resources()
    assert {str(resource.uri) for resource in resources.resources} >= {
        "global-executables://metadata", "global-executables://coverage"
    }
    metadata = await session.read_resource("global-executables://metadata")
    assert json.loads(metadata.contents[0].text)["snapshot"] == SNAPSHOT
    schema = await session.read_resource("global-executables://schema/executable")
    assert json.loads(schema.contents[0].text)["title"] == "Canonical executable record"
    executable = await session.read_resource("global-executables://executables/envcp")
    assert json.loads(executable.contents[0].text)["command"] == "envcp"
    try:
        await session.read_resource("global-executables://unknown-resource")
    except Exception:
        pass
    else:
        raise AssertionError("unknown resource should fail")


async def test_local_stdio_protocol_works_without_network(dictionary_root):
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-m", "global_executables.mcp_server", "--root", str(ROOT),
        "--dataset-root", str(dictionary_root),
    ])
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await _assert_protocol(session)


async def test_streamable_http_protocol_and_health(dictionary_root):
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0)); port = candidate.getsockname()[1]
    process = subprocess.Popen([
        sys.executable, "-m", "global_executables.mcp_server", "--root", str(ROOT),
        "--dataset-root", str(dictionary_root),
        "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(port)
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        health = None
        for _ in range(300):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.2) as response:
                    health = json.load(response); break
            except Exception:
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read().decode())
                await asyncio.sleep(.05)
        assert health == {"status": "ok", "service_version": "1.2.0", "snapshot": SNAPSHOT, "coverage_scope": "unknown", "read_only": True}
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await _assert_protocol(session)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


async def test_server_exposes_no_write_contract_directly(dictionary_root):
    server = create_server(ROOT, dataset_root=dictionary_root)
    names = {tool.name for tool in await server.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_server_fails_closed_for_missing_dataset(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_server(ROOT, dataset_root=tmp_path)


async def test_server_accepts_a_dataset_root_separate_from_program_files(tmp_path):
    dataset_root = tmp_path / "dictionary"
    rebuild(dataset_root, sorted((ROOT / "fixtures/intermediate").glob("*.jsonl")),
            "2026-08-14")

    server = create_server(ROOT, dataset_root=dataset_root)

    metadata = await server.read_resource("global-executables://metadata")
    assert json.loads(metadata[0].content)["snapshot"] == "2026-08-14"
    schema = await server.read_resource("global-executables://schema/executable")
    assert json.loads(schema[0].content)["title"] == "Canonical executable record"
