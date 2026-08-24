import fcntl
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
REFRESH = (ROOT / ".github/workflows/refresh.yml").read_text()
PARALLEL_CRAWL = (ROOT / "tools/crawl_parallel.sh").read_text()
OS_SOURCES = {"arch", "debian", "ubuntu", "homebrew", "msys2", "scoop", "winget", "windows"}


def test_refresh_reuses_durable_os_samples_by_default():
    assert "refresh_os_samples:" in REFRESH
    os_input = REFRESH.split("refresh_os_samples:", 1)[1].split("permissions:", 1)[0]
    assert "default: false" in os_input

    assert REFRESH.index("Restore latest durable observations") < REFRESH.index("Rebuild from production and registry inputs")
    restore = REFRESH.split("Restore latest durable observations", 1)[1].split("Acquire production OS indexes", 1)[0]
    for source in OS_SOURCES | {"npm", "pypi", "crates", "go", "rubygems", "packagist",
                                "nuget", "macos", "shell"}:
        assert source in restore
    assert "origin/dictionary:reports/production-crawl.json" in REFRESH

    acquire = REFRESH.split("Acquire production OS indexes", 1)[1].split("Publish refreshed OS observations", 1)[0]
    assert "if: github.event_name == 'workflow_dispatch' && inputs.refresh_os_samples" in acquire


def test_refresh_publishes_os_observations_and_alerts_on_failure():
    publish = REFRESH.split("Publish refreshed OS observations", 1)[1]
    assert "if: github.event_name == 'workflow_dispatch' && inputs.refresh_os_samples" in publish
    for source in OS_SOURCES:
        assert f"${{source}}.jsonl" in publish
    assert "Unable to publish OS observations after three attempts" in publish
    assert "needs.refresh.result == 'failure'" in REFRESH
    assert "gh issue create" in REFRESH


def test_local_publish_includes_os_observations_from_the_checkout():
    assert 'SOURCES="${SOURCES-pypi rubygems packagist nuget npm go}"' in PARALLEL_CRAWL
    assert 'OBSERVATION_SOURCES="${OBSERVATION_SOURCES:-arch debian ubuntu homebrew msys2 scoop winget windows macos shell}"' in PARALLEL_CRAWL
    assert 'local rows="data/production/intermediate/${source}.jsonl"' in PARALLEL_CRAWL
    assert 'local observed="${ROOT_DIR}/${rows}"' in PARALLEL_CRAWL


def test_local_go_source_uses_dedicated_go_runtime_with_failure_restart():
    assert "tools/go_image.sh" in PARALLEL_CRAWL
    assert 'if [ "${source}" = go ]; then' in PARALLEL_CRAWL
    assert 'global-executables-go-crawler:local' in PARALLEL_CRAWL
    assert "--restart on-failure:5" in PARALLEL_CRAWL
    assert "crawl --passes 0" in PARALLEL_CRAWL


def test_manual_and_watched_publication_share_one_exclusive_lock(tmp_path):
    assert "publish_locked()" in PARALLEL_CRAWL
    assert "flock -n 9" in PARALLEL_CRAWL
    assert "publish_locked 2>&1" in PARALLEL_CRAWL
    assert "publish) publish_locked ;;" in PARALLEL_CRAWL

    base = tmp_path / "crawl"
    with open(f"{base}.publish.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            ["bash", "tools/crawl_parallel.sh", "publish"], cwd=ROOT,
            capture_output=True, text=True,
            env={**os.environ, "BASE": str(base), "SOURCES": "", "OBSERVATION_SOURCES": ""},
        )

    assert result.returncode == 0
    assert "publication already in progress" in result.stdout


def test_python_crawl_loop_rejects_go():
    result = subprocess.run(
        ["sh", "tools/crawl_loop.sh"], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "SOURCES": "go"},
    )

    assert result.returncode == 2
    assert "dedicated Go runtime" in result.stderr
