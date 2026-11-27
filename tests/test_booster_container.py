"""The booster container (Dockerfile.booster, deploy/booster): entrypoint, health, no secrets."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "tools" / "booster_container.sh"
HEALTHCHECK = ROOT / "tools" / "booster_healthcheck.sh"
TOKEN = "ghp_test-token-never-printed"


def _app(tmp_path: Path) -> Path:
    """A copy of the entrypoint next to a stub supervisor that reports what git was given."""
    app = tmp_path / "app"
    (app / "tools").mkdir(parents=True)
    shutil.copy(ENTRYPOINT, app / "tools" / "booster_container.sh")
    stub = app / "tools" / "local_booster.sh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "args=$*"\n'
        'echo "origin=$(git -C "$(dirname "$0")/.." remote get-url origin)"\n'
        'echo "name=$(git config --global user.name)"\n'
        'printf "protocol=https\\nhost=github.com\\n\\n" | git credential fill | grep "^password=" > "$HOME/filled"\n'
    )
    stub.chmod(0o755)
    return app


def _run(app: Path, tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    base = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_STORE": str(tmp_path / "git"),
            "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["bash", str(app / "tools" / "booster_container.sh")], env={**base, **env},
                          capture_output=True, text=True, timeout=60)


def test_entrypoint_refuses_without_contact_id_or_token(tmp_path):
    app = _app(tmp_path)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    for env, word in (({"BOOSTER_ID": "x", "GITHUB_TOKEN_FILE": str(token)}, "CONTACT"),
                      ({"CONTACT": "a@b.test", "GITHUB_TOKEN_FILE": str(token)}, "BOOSTER_ID"),
                      ({"CONTACT": "a@b.test", "BOOSTER_ID": "x",
                        "GITHUB_TOKEN_FILE": str(tmp_path / "missing")}, "token")):
        result = _run(app, tmp_path, **env)
        assert result.returncode == 2 and word in result.stderr, (env, result.stderr)


def test_entrypoint_wires_git_to_the_token_file_and_survives_recreation(tmp_path):
    app = _app(tmp_path)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    env = {"CONTACT": "a@b.test", "BOOSTER_ID": "host-booster", "GITHUB_TOKEN_FILE": str(token)}
    for _ in range(2):  # the second start finds the object store already on the volume
        result = _run(app, tmp_path, **env)
        assert result.returncode == 0, result.stderr
        assert "args=pypi run" in result.stdout
        assert "origin=https://github.com/asopitech-labs/global-executables" in result.stdout
        assert "name=host-booster booster" in result.stdout
        assert TOKEN not in result.stdout + result.stderr
        assert (tmp_path / "home" / "filled").read_text().strip() == f"password={TOKEN}"
        (app / ".git").unlink()  # a re-created container has only the image's /app
    assert (tmp_path / "git" / "HEAD").is_file()


def _health(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "BASE": str(tmp_path / "ge"), "HEALTH_MAX_STALE": "600"}
    return subprocess.run(["bash", str(HEALTHCHECK)], env=env, capture_output=True, text=True)


def test_healthcheck_needs_a_live_supervisor_and_recent_progress(tmp_path):
    directory = tmp_path / "ge-pypi"
    directory.mkdir()
    assert _health(tmp_path).returncode == 1
    (directory / "supervisor.pid").write_text("999999999")
    assert _health(tmp_path).returncode == 1
    (directory / "supervisor.pid").write_text(str(os.getpid()))
    assert _health(tmp_path).returncode == 0  # starting: no progress yet
    progress = directory / "progress"
    progress.touch()
    assert _health(tmp_path).returncode == 0
    old = time.time() - 3600
    os.utime(progress, (old, old))
    assert _health(tmp_path).returncode == 1


def test_image_and_compose_carry_no_secret_and_keep_running():
    dockerfile = (ROOT / "Dockerfile.booster").read_text()
    assert "TOKEN=" not in dockerfile.replace("GITHUB_TOKEN_FILE=", "")
    ignored = (ROOT / "Dockerfile.booster.dockerignore").read_text().split()
    assert {".git", "deploy/booster/*.env", "deploy/booster/github_token"} <= set(ignored)
    compose = yaml.safe_load((ROOT / "deploy" / "booster" / "compose.yml").read_text())
    service = compose["services"]["pypi"]
    assert service["restart"] == "unless-stopped"
    assert service["secrets"] == ["github_token"]
    assert "booster-data:/data" in service["volumes"]
    assert not any("TOKEN" in str(key).upper() for key in service.get("environment", {}))
    assert (ROOT / "deploy" / "booster" / ".gitignore").read_text().split() == ["booster.env", "github_token"]
