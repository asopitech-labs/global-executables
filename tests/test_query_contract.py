"""Execute the same negative-lookup fixtures through Python, MCP, and browser JS."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from global_executables.mcp_server import create_server
from global_executables.pipeline import rebuild
from global_executables.search import Dataset

ROOT = Path(__file__).parents[1]
CASES = [
    ("partial", None, "missing-cli", "unknown"),
    ("exhaustive", None, "missing-cli", "clear_in_index"),
    ("exhaustive", {}, "missing-cli", "clear_in_index"),
    ("subset", None, "missing-cli", "unknown"),
    ("catalog", None, "missing-cli", "unknown"),
    ("failed", None, "missing-cli", "unknown"),
    ("missing", None, "missing-cli", "unknown"),
    ("exhaustive", {"registry": "npm"}, "missing-cli", "unknown"),
    ("exhaustive", {"registry": "uncovered"}, "missing-cli", "unknown"),
    ("exhaustive", {"unsupported": "value"}, "missing-cli", "unknown"),
    ("partial", None, "envcp", "collision"),
    ("subset", {"registry": "npm"}, "envcp", "collision"),
    ("exhaustive", {"registry": "uncovered"}, "envcp", "unknown"),
]


def make_dataset(tmp_path, kind):
    rebuild(tmp_path, sorted((ROOT / "fixtures/intermediate").glob("*.jsonl")),
            "2026-10-19", coverage_kind="exhaustive")
    path = tmp_path / "data/metadata.json"
    metadata = json.loads(path.read_text())
    npm = metadata["coverage"]["npm"]
    if kind == "partial":
        npm["coverage_kind"] = "partial"
    elif kind == "subset":
        # Simulate legacy metadata that called a completed critical subset exhaustive.
        npm.update(scope="ecosyste.ms critical npm population", catalog_packages=2295,
                   catalog_source="https://packages.ecosyste.ms/critical?registry=npmjs.org",
                   catalog_snapshot="2026-10-18", catalog_digest="sha256:" + "a" * 64)
    elif kind == "catalog":
        npm["catalog_complete"] = True
    elif kind == "failed":
        npm["status"] = "failed"
    elif kind == "missing":
        metadata["coverage"] = {}
    path.write_text(json.dumps(metadata))
    return Dataset(tmp_path)


def browser_check(dataset, name, scope, *, batch=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the Playground contract (installed in CI)")
    records = {path.relative_to(dataset.data).as_posix(): json.loads(path.read_text())
               for path in (dataset.data / "executables").glob("**/*.json")}
    # Execute the actual client functions with only boot/network replaced.
    script = (ROOT / "playground/app.js").read_text().rsplit("boot();", 1)[0]
    script += '''
const fixture = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
state.metadata = fixture.metadata;
globalThis.fetch = async (url) => {
  const path = url.slice(DATA_BASE.length + 1);
  return {ok: Object.hasOwn(fixture.records, path), status: 404, statusText: "Not Found",
          json: async () => fixture.records[path]};
};
async function query() {
  if (!fixture.batch) return checkExecutable(fixture.name, fixture.scope);
  state.operation = "check_executables";
  globalThis.FormData = class {
    get(key) { return key === "names" ? fixture.name : fixture.scope ? JSON.stringify(fixture.scope) : ""; }
  };
  const resultElement = {};
  globalThis.document = {getElementById: () => resultElement};
  await runQuery({preventDefault() {}, target: {}});
  return state.result;
}
query().then((result) => console.log(JSON.stringify({result})))
  .catch((error) => console.log(JSON.stringify({error: error.message})));
'''
    output = subprocess.check_output([node, "-e", script], input=json.dumps({
        "metadata": dataset.metadata, "records": records, "name": name, "scope": scope, "batch": batch,
    }), text=True)
    return json.loads(output)


@pytest.mark.parametrize("kind,scope,name,status", CASES)
async def test_python_mcp_and_browser_share_query_contract(tmp_path, kind, scope, name, status):
    dataset = make_dataset(tmp_path, kind)
    expected = dataset.check(name, scope)
    assert expected["status"] == status
    if not expected["found"]:
        assert expected["absence"]["confidence"] == (
            "exhaustive" if status == "clear_in_index" else "insufficient_coverage")
    if kind in {"partial", "subset", "catalog", "failed", "missing"}:
        warning = expected["warnings"][0]
        assert warning["code"] == "npm_coverage_incomplete"
        assert "not permission to adopt the name" in warning["message"]
        if kind == "subset":
            assert warning["coverage"]["catalog_packages"] == 2295
            assert warning["coverage"]["scope"] == dataset.metadata["coverage"]["npm"]["scope"]
    assert browser_check(dataset, name, scope) == {"result": expected}
    server = create_server(ROOT, dataset_root=tmp_path)
    async with create_connected_server_and_client_session(server) as session:
        single = await session.call_tool("check_executable", {"name": name, "scope": scope})
        assert not single.isError
        assert single.structuredContent == expected
        assert json.loads(single.content[0].text) == expected
        batch = await session.call_tool("check_executables", {"names": [name], "scope": scope})
        assert not batch.isError
        assert json.loads(batch.content[0].text) == batch.structuredContent
        assert batch.structuredContent["results"] == [expected]
        assert browser_check(dataset, name, scope, batch=True) == {"result": batch.structuredContent}
        assert batch.structuredContent["coverage_scope"] == expected["coverage_scope"]
        assert batch.structuredContent["warnings"] == expected["warnings"]
        assessment = await session.call_tool("assess_executable", {"name": name, "scope": scope})
        assert assessment.structuredContent["warnings"] == expected["warnings"]
        coverage = (await session.call_tool("get_coverage", {})).structuredContent
        assert coverage["warnings"] == expected["warnings"]
        if kind == "subset":
            assert coverage["negative_lookup"] == "unknown"
            resource = await session.read_resource("global-executables://coverage")
            assert json.loads(resource.contents[0].text)["negative_lookup"] == "unknown"


@pytest.mark.parametrize("name", ["", ".", "..", "bad/name", "bad\\name", "bad\x00name", "x" * 256])
async def test_invalid_names_are_input_errors_in_all_transports(tmp_path, name):
    dataset = make_dataset(tmp_path, "exhaustive")
    with pytest.raises(ValueError, match="invalid executable name"):
        dataset.check(name)
    assert "invalid executable name" in browser_check(dataset, name, None)["error"]
    async with create_connected_server_and_client_session(create_server(ROOT, dataset_root=tmp_path)) as session:
        for tool, arguments in (("check_executable", {"name": name}),
                                ("check_executables", {"names": ["envcp", name]})):
            result = await session.call_tool(tool, arguments)
            assert result.isError
            assert result.structuredContent is None
