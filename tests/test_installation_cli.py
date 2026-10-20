"""Exercise the installed command-line query path for installation guidance."""
import json
import subprocess
import sys
from pathlib import Path

from global_executables.installation import resolve_installation
from test_query_contract import make_dataset

COMMAND = str(Path(sys.executable).with_name("global-executables"))


def invoke(root, action, *names, environment, preferences=None):
    command = [COMMAND, action, *names,
               "--root", str(root), "--environment", json.dumps(environment)]
    if preferences is not None:
        command += ["--preferences", json.dumps(preferences)]
    return subprocess.run(command, text=True, capture_output=True, check=False)


def test_cli_single_matches_resolver(tmp_path):
    dataset = make_dataset(tmp_path, "partial")
    environment = {"package_manager": "pip", "runtime_version": "3.12",
                   "install_scope": "project", "virtualenv_bin": "/tmp/venv/bin"}
    completed = invoke(tmp_path, "resolve-installation", "pygo", environment=environment)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == resolve_installation(dataset, "pygo", environment)


def test_cli_batch_preserves_order_and_missing_name_caveat(tmp_path):
    dataset = make_dataset(tmp_path, "partial")
    environment = {"package_manager": "npm", "runtime_version": "24",
                   "install_scope": "project"}
    completed = invoke(tmp_path, "resolve-installations", "envcp", "missing-cli", environment=environment)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["contract_version"] == "installation-v1"
    assert payload["results"] == [resolve_installation(dataset, name, environment)
                                  for name in ("envcp", "missing-cli")]
    assert payload["results"][1]["status"] == "not_found_in_index"
    assert payload["warnings"]


def test_cli_rejects_invalid_environment_json(tmp_path):
    make_dataset(tmp_path, "partial")
    completed = subprocess.run(
        [COMMAND, "resolve-installation", "envcp",
         "--root", str(tmp_path), "--environment", "{"],
        text=True, capture_output=True, check=False,
    )
    assert completed.returncode != 0
    assert "error:" in completed.stderr
