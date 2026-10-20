"""Exercise the published Playground installation form's submit path."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def test_installation_form_passes_selected_target_and_overrides_to_resolver():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Playground checks")

    page = (ROOT / "playground/index.html").read_text()
    assert 'data-operation="resolve_installation"' in page
    assert 'data-operation="resolve_installations"' in page

    script = (ROOT / "playground/app.js").read_text().rsplit("boot();", 1)[0]
    script += """
state.operation = "resolve_installation";
globalThis.FormData = class {
  get(key) { return ({name: "tsc", target_os: "linux", package_manager: "npm",
    architecture: "amd64", runtime_version: "24", install_scope: "project",
    environment: '{"execution_context":"container","architecture":"arm64"}',
    preferences: '{"package":"typescript"}'})[key] ?? null; }
};
const elements = {};
globalThis.document = {getElementById: (id) => elements[id] ||= {
  replaceChildren() {}, append() {}, textContent: "", hidden: true,
}};
resolveInstallation = async (name, environment, preferences) => ({name, environment, preferences});
runQuery({preventDefault() {}, target: {}}).then(() =>
  console.log(JSON.stringify(state.result)));
"""
    result = json.loads(subprocess.check_output([node, "-e", script], text=True))
    assert result == {
        "name": "tsc",
        "environment": {"os": "linux", "package_manager": "npm", "architecture": "arm64",
                        "runtime_version": "24", "install_scope": "project",
                        "execution_context": "container"},
        "preferences": {"package": "typescript"},
    }
