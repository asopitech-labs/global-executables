import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
MERGER = ROOT / "tools/merge_registry_publication.py"
WORKFLOW = (ROOT / ".github/workflows/registry-artifacts.yml").read_text()
PARALLEL = (ROOT / "tools/crawl_parallel.sh").read_text()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def run_merge(
    tmp_path: Path,
    source: str,
    published_cursor: int,
    local_cursor: int,
    expected_returncode: int = 0,
) -> tuple[dict, dict]:
    published_state = tmp_path / "published-state.json"
    local_state = tmp_path / "local-state.json"
    published_report = tmp_path / "published-report.json"
    local_report = tmp_path / "local-report.json"
    write_json(
        published_state,
        {"version": 1, "sources": {"crates": {"cursor": 100}, source: {"cursor": published_cursor}}},
    )
    write_json(local_state, {"version": 1, "sources": {source: {"cursor": local_cursor}}})
    write_json(
        published_report,
        {
            "status": "success",
            "coverage_kind": "partial",
            "sources": {
                # Older source reports predate the per-source status field. Missing
                # means no reported failure, not a failed aggregate publication.
                "crates": {"cursor": 100, "coverage_kind": "exhaustive"},
                source: {"cursor": published_cursor, "status": "success", "coverage_kind": "partial"},
            },
        },
    )
    write_json(
        local_report,
        {
            "status": "success",
            "coverage_kind": "partial",
            "finished_at": "2026-08-24T19:00:00Z",
            "sources": {
                source: {
                    "cursor": local_cursor,
                    "catalog_size": 2_000_000,
                    "status": "success",
                    "coverage_kind": "partial",
                }
            },
        },
    )
    completed = subprocess.run(
        [
            sys.executable,
            MERGER,
            "--source",
            source,
            "--published-state",
            published_state,
            "--local-state",
            local_state,
            "--published-report",
            published_report,
            "--local-report",
            local_report,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected_returncode, completed.stderr
    return json.loads(published_state.read_text()), json.loads(published_report.read_text())


def test_merger_updates_only_the_owned_source(tmp_path):
    state, report = run_merge(tmp_path, "go", published_cursor=23_638, local_cursor=29_612)

    assert state["sources"] == {"crates": {"cursor": 100}, "go": {"cursor": 29_612}}
    assert report["sources"]["crates"]["cursor"] == 100
    assert report["sources"]["go"]["cursor"] == 29_612
    assert report["sources"]["go"]["catalog_size"] == 2_000_000
    assert report["coverage_kind"] == "partial"
    assert report["status"] == "success"


def test_merger_refuses_state_and_report_regression(tmp_path):
    state, report = run_merge(
        tmp_path,
        "go",
        published_cursor=29_612,
        local_cursor=23_638,
        expected_returncode=3,
    )

    assert state["sources"]["go"]["cursor"] == 29_612
    assert report["sources"]["go"]["cursor"] == 29_612


def test_both_publishers_use_source_owned_merge():
    assert "bash tools/crawl_parallel.sh publish" in WORKFLOW
    assert "SOURCES=crates" in WORKFLOW
    assert "cp data/production/registry-state.json /tmp/artifact-publish" not in WORKFLOW
    assert "merge_registry_publication.py" in PARALLEL
    assert "merge_status" in PARALLEL


def test_shared_publisher_retries_from_a_fresh_remote_snapshot():
    assert 'PUBLISH_MAX_ATTEMPTS="${PUBLISH_MAX_ATTEMPTS:-3}"' in PARALLEL
    assert 'local attempt="${PUBLISH_ATTEMPT:-1}"' in PARALLEL
    assert 'PUBLISH_ATTEMPT=$((attempt + 1)) publish' in PARALLEL
    assert 'PUBLISH_MAX_ATTEMPTS=3' in WORKFLOW
