"""Installation guidance is identical in Python, MCP, and the Pages client."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from global_executables.installation import resolve_installation
from global_executables.mcp_server import create_server
from test_query_contract import make_dataset

ROOT = Path(__file__).parents[1]
CASES = [
    ("git", {"os": "macos", "package_manager": "brew"}, "unverified"),
    ("envcp", {"package_manager": "apt", "distribution": "debian", "distribution_release": "12", "architecture": "amd64", "has_admin": True}, "unverified"),
    ("git", {"package_manager": "apt", "distribution": "ubuntu", "distribution_release": "24.04", "architecture": "amd64", "has_admin": True}, "unsupported"),
    ("envcp", {"package_manager": "npm", "runtime_version": "24.19.0", "install_scope": "project"}, "unverified"),
    ("pygo", {"package_manager": "pip", "runtime_version": "3.12.14", "install_scope": "project", "virtualenv_bin": "/tmp/venv/bin"}, "unverified"),
    ("git", {}, "needs_context"),
    ("git-old", {"os": "macos", "package_manager": "brew"}, "unverified"),
    ("missing-cli", {"os": "macos", "package_manager": "brew"}, "not_found_in_index"),
]


def browser_resolve(dataset, name, environment):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    records = {path.relative_to(dataset.data).as_posix(): json.loads(path.read_text())
               for path in (dataset.data / "executables").glob("**/*.json")}
    script = (ROOT / "playground/app.js").read_text().rsplit("boot();", 1)[0]
    script += '''
const fixture = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
state.metadata = fixture.metadata;
globalThis.fetch = async (url) => {
  const path = url.slice(DATA_BASE.length + 1);
  return {ok: Object.hasOwn(fixture.records, path), status: 404, statusText: "Not Found",
          json: async () => fixture.records[path]};
};
resolveInstallation(fixture.name, fixture.environment).then((result) => console.log(JSON.stringify(result)));
'''
    return json.loads(subprocess.check_output([node, "-e", script], input=json.dumps({
        "metadata": dataset.metadata, "records": records, "name": name, "environment": environment,
    }), text=True))


@pytest.mark.parametrize("name,environment,status", CASES)
async def test_installation_contract(tmp_path, name, environment, status):
    dataset = make_dataset(tmp_path, "partial")
    expected = resolve_installation(dataset, name, environment)
    assert expected["status"] == status
    assert browser_resolve(dataset, name, environment) == expected
    async with create_connected_server_and_client_session(create_server(ROOT, dataset_root=tmp_path)) as session:
        single = await session.call_tool("resolve_installation", {"name": name, "environment": environment})
        assert single.structuredContent == expected
        assert json.loads(single.content[0].text) == expected
        batch = await session.call_tool("resolve_installations", {"names": [name], "environment": environment})
        assert batch.structuredContent["results"] == [expected]


def test_install_and_invoke_steps_are_argv_not_shell(tmp_path):
    dataset = make_dataset(tmp_path, "partial")
    for name, environment, _ in CASES[:1] + CASES[3:5]:
        result = resolve_installation(dataset, name, environment)
        assert [step["action"] for step in result["steps"]] == ["install", "invoke"]
        assert result["steps"][-1]["argv"] == result["invocation"]["argv"]
        assert result["steps"][0]["argv"][-1] == result["provider"]["package"]


def test_project_method_probe_is_separate_from_provider_validation(tmp_path):
    dataset = make_dataset(tmp_path, "partial")
    npm = resolve_installation(dataset, "envcp", {"package_manager": "npm", "runtime_version": "24.19.0", "install_scope": "project"})
    pip = resolve_installation(dataset, "pygo", {"package_manager": "pip", "runtime_version": "3.12.14", "install_scope": "project", "virtualenv_bin": "/tmp/venv/bin"})
    assert npm["method_validation"]["result"] == "pass"
    assert pip["method_validation"]["result"] == "pass"
    assert npm["status"] == pip["status"] == "unverified"
    assert npm["invocation"]["argv"] == ["npm", "exec", "--", "envcp"]
    assert pip["invocation"]["argv"] == ["/tmp/venv/bin/pygo"]
    assert pip["steps"][0]["argv"][:4] == ["/tmp/venv/bin/python", "-m", "pip", "install"]


def test_apt_method_is_shown_with_an_availability_check_when_release_is_unknown(tmp_path):
    dataset = make_dataset(tmp_path, "partial")
    result = resolve_installation(dataset, "envcp", {"package_manager": "apt", "distribution": "debian", "distribution_release": "13", "architecture": "amd64", "has_admin": True})
    assert result["status"] == "unverified"
    assert [step["action"] for step in result["steps"]] == ["check_availability", "install", "invoke"]
    assert result["steps"][1]["argv"] == ["sudo", "apt-get", "install", "--", "envcp-deb"]
    assert result["verification"]["source_release_matches_target"] is False
    assert result["method"]["documentation"].startswith("https://manpages.debian.org/")
    assert result["method_validation"]["result"] == "pass"
