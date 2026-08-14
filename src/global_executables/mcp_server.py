from __future__ import annotations
import argparse, os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from .search import Dataset

def create_server(root: str | Path):
    dataset=Dataset(root); mcp=FastMCP("Global Executables",json_response=True)
    @mcp.tool()
    def check_executable(name: str): return dataset.check(name)
    @mcp.tool()
    def check_executables(names: list[str]): return {"results":[dataset.check(n) for n in names],"snapshot":dataset.metadata["snapshot"]}
    @mcp.tool()
    def get_executable(name: str): return dataset.get(name)
    @mcp.tool()
    def search_executables(prefix: str="", length: int|None=None, ecosystem: str|None=None, limit: int=100): return {"executables":dataset.search(prefix,length,ecosystem,limit),"snapshot":dataset.metadata["snapshot"]}
    @mcp.tool()
    def search_similar_executables(name: str, limit: int=20): return {"matches":dataset.similar(name,limit),"snapshot":dataset.metadata["snapshot"]}
    @mcp.tool()
    def get_coverage(): return dataset.metadata
    @mcp.resource("global-executables://metadata")
    def metadata(): return dataset.metadata
    return mcp

def main():
    p=argparse.ArgumentParser(); p.add_argument("--root",default=os.getenv("GLOBAL_EXECUTABLES_ROOT",Path.cwd())); p.add_argument("--transport",choices=["stdio","streamable-http"],default="stdio"); p.add_argument("--host",default="127.0.0.1"); p.add_argument("--port",type=int,default=8000); a=p.parse_args()
    server=create_server(a.root)
    if a.transport=="streamable-http": server.settings.host=a.host; server.settings.port=a.port
    server.run(transport=a.transport)
