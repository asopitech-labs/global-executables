"""Verify that recipe evidence selects the right indexed provider and target."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from global_executables.installation import resolve_installation


ROOT = Path(__file__).parents[1]
RUN = "https://github.com/asopitech-labs/global-executables/actions/runs/39363743911"


class Records:
    def __init__(self, providers):
        self.providers = providers

    def check(self, name):
        return {"name": name, "found": bool(self.providers), "providers": self.providers,
                "snapshot": "2026-10-20", "coverage_scope": "partial", "warnings": []}


def provider(ecosystem, package, version, source, confidence="direct"):
    return {"ecosystem": ecosystem, "package": package, "version": version,
            "source": source, "confidence": confidence}


def browser_result(checked, environment, preferences=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Playground parity")
    script = (ROOT / "playground/app.js").read_text().rsplit("boot();", 1)[0]
    script += """
const fixture = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(resolveInstallationFromCheck(fixture.checked, fixture.environment, fixture.preferences)));
"""
    return json.loads(subprocess.check_output([node, "-e", script], input=json.dumps({
        "checked": checked, "environment": environment, "preferences": preferences or {},
    }), text=True))


TYPESCRIPT = provider("npm", "typescript", "7.0.2", "https://registry.npmjs.org/typescript/latest")
BLACK = provider("pypi", "black", "26.10.1", "https://files.pythonhosted.org/packages/black-26.10.1.whl")


@pytest.mark.parametrize("name,selected,other,manager,runtime,spec", [
    ("tsc", TYPESCRIPT, provider("npm", "forked-typescript", "7.0.2", "https://registry.npmjs.org/forked-typescript/latest"), "npm", "24.19.0", "typescript@7.0.2"),
    ("black", BLACK, provider("pypi", "black-fork", "26.10.1", "https://files.pythonhosted.org/packages/black-fork.whl"), "pip", "3.12.14", "black==26.10.1"),
])
def test_verified_recipe_matches_provider_and_target(name, selected, other, manager, runtime, spec):
    scope = "project"
    environment = {"os": "linux", "distribution": "ubuntu", "distribution_release": "24.04",
                   "architecture": "amd64", "package_manager": manager,
                   "runtime_version": runtime, "install_scope": scope}
    if manager == "pip" and scope == "project":
        environment["virtualenv_bin"] = "/tmp/venv/bin"
    dataset = Records([other, selected])
    result = resolve_installation(dataset, name, environment)
    assert result == browser_result(dataset.check(name), environment)
    assert result["status"] == "ready"
    assert result["provider"] == selected
    assert result["steps"][0]["argv"][-1] == spec
    assert result["verification"]["recipe_validation"]["evidence_url"] == RUN
    assert len(result["candidates"]) == 2


@pytest.mark.parametrize("change", [
    {"architecture": "arm64"}, {"distribution_release": "22.04"},
    {"runtime_version": "23"}, {"os": "macos"},
])
def test_verified_recipe_does_not_escape_tested_target(change):
    environment = {"os": "linux", "distribution": "ubuntu", "distribution_release": "24.04",
                   "architecture": "amd64", "package_manager": "npm", "runtime_version": "24",
                   "install_scope": "project", **change}
    dataset = Records([TYPESCRIPT])
    result = resolve_installation(dataset, "tsc", environment)
    assert result == browser_result(dataset.check("tsc"), environment)
    assert result["status"] == "unverified"


def test_stale_version_and_wrong_package_never_become_ready():
    environment = {"os": "linux", "distribution": "ubuntu", "distribution_release": "24.04",
                   "architecture": "amd64", "package_manager": "npm", "runtime_version": "24",
                   "install_scope": "project"}
    for candidate in [
        {**TYPESCRIPT, "version": "7.0.3"},
        {**TYPESCRIPT, "package": "other-typescript"},
        {**TYPESCRIPT, "source": "https://example.invalid/metadata"},
        {**TYPESCRIPT, "confidence": "inferred"},
    ]:
        dataset = Records([candidate])
        result = resolve_installation(dataset, "tsc", environment)
        assert result == browser_result(dataset.check("tsc"), environment)
        assert result["status"] == "unverified"


def test_user_scope_remains_unverified_without_an_invocation_path():
    environment = {"os": "linux", "distribution": "ubuntu", "distribution_release": "24.04",
                   "architecture": "amd64", "package_manager": "npm", "runtime_version": "24",
                   "install_scope": "user"}
    dataset = Records([TYPESCRIPT])
    result = resolve_installation(dataset, "tsc", environment)
    assert result == browser_result(dataset.check("tsc"), environment)
    assert result["status"] == "unverified"


def test_ubuntu_hello_matches_verified_repository_source():
    source = "https://archive.ubuntu.com/ubuntu/dists/noble/Contents-amd64.gz"
    candidate = provider("ubuntu", "hello", None, source, "filesystem")
    candidate["distribution"] = "ubuntu"
    environment = {"os": "linux", "package_manager": "apt", "distribution": "ubuntu",
                   "distribution_release": "24.04", "architecture": "amd64", "has_admin": True}
    dataset = Records([candidate])
    result = resolve_installation(dataset, "hello", environment)
    assert result == browser_result(dataset.check("hello"), environment)
    assert result["status"] == "ready"
    assert result["steps"][0]["argv"] == ["sudo", "apt-get", "install", "--", "hello"]
    assert result["verification"]["source_release_matches_target"] is True
    candidate["source"] = "https://archive.ubuntu.com/ubuntu/dists/other/Contents-amd64.gz"
    assert resolve_installation(Records([candidate]), "hello", environment)["status"] == "unverified"


def test_homebrew_hello_requires_tested_version_and_target():
    candidate = provider("homebrew", "hello", "2.12.3", "https://formulae.brew.sh/api/formula.json", "filesystem")
    environment = {"os": "macos", "package_manager": "brew",
                   "distribution_release": "15", "architecture": "arm64"}
    dataset = Records([candidate])
    result = resolve_installation(dataset, "hello", environment)
    assert result == browser_result(dataset.check("hello"), environment)
    assert result["status"] == "ready"
    assert result["verification"]["recipe_validation"]["package_version"] == "2.12.3"
    candidate["version"] = "2.12.4"
    assert resolve_installation(Records([candidate]), "hello", environment)["status"] == "unverified"
