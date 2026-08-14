from pathlib import Path
from global_executables.mcp_server import create_server
from global_executables.pipeline import rebuild

ROOT=Path(__file__).parents[1]

async def test_mcp_exposes_read_only_tool_contract(tmp_path):
    rebuild(tmp_path,sorted((ROOT/"fixtures/intermediate").glob("*.jsonl")),"2026-08-14")
    server=create_server(tmp_path)
    tools=await server.list_tools()
    names={tool.name for tool in tools}
    assert names=={"check_executable","check_executables","get_executable","search_executables","search_similar_executables","get_coverage"}
    assert not any("write" in name or "update" in name for name in names)
