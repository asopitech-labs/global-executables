import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
MERGER = ROOT / "tools/merge_registry_publication.py"
spec = importlib.util.spec_from_file_location("merge_registry_publication", MERGER)
merger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merger)
module_merge_state = merger.merge_state
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


def test_merger_accepts_cursor_reset_for_a_new_catalog(tmp_path):
    published_state = tmp_path / "published-state.json"
    local_state = tmp_path / "local-state.json"
    published_report = tmp_path / "published-report.json"
    local_report = tmp_path / "local-report.json"
    write_json(published_state, {"sources": {"npm": {
        "cursor": 344_774,
        "catalog_size": 4_311_362,
        "packages_file": "data/production/npm-packages.txt",
    }}})
    write_json(local_state, {"sources": {"npm": {
        "cursor": 2_295,
        "catalog_size": 2_295,
        "packages_file": "data/production/npm-critical-packages.txt",
    }}})
    write_json(published_report, {"sources": {"npm": {
        "cursor": 344_774,
        "catalog_size": 4_311_362,
        "coverage_kind": "partial",
    }}})
    write_json(local_report, {"sources": {"npm": {
        "cursor": 2_295,
        "catalog_size": 2_295,
        "packages_file": "data/production/npm-critical-packages.txt",
        "coverage_kind": "exhaustive",
        "status": "success",
    }}})

    completed = subprocess.run(
        [sys.executable, MERGER, "--source", "npm",
         "--published-state", published_state, "--local-state", local_state,
         "--published-report", published_report, "--local-report", local_report],
        check=False, capture_output=True, text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(published_state.read_text())["sources"]["npm"]["cursor"] == 2_295
    assert json.loads(published_report.read_text())["sources"]["npm"]["coverage_kind"] == "exhaustive"


def test_merger_uses_digest_to_distinguish_daily_catalog_generations(tmp_path):
    published_state = tmp_path / "published-state.json"
    local_state = tmp_path / "local-state.json"
    write_json(published_state, {"sources": {"npm": {
        "cursor": 2_295,
        "catalog_digest": "sha256:" + "a" * 64,
        "catalog_snapshot": "2026-08-25",
        "packages_file": "data/production/npm-critical-packages.txt",
    }}})
    write_json(local_state, {"sources": {"npm": {
        "cursor": 0,
        "catalog_digest": "sha256:" + "b" * 64,
        "catalog_snapshot": "2026-08-26",
        "packages_file": "data/production/npm-critical-packages.txt",
    }}})

    merged, before, after = module_merge_state("npm", published_state, local_state)

    assert merged is True
    assert (before, after) == (2_295, 0)


def test_merger_rejects_regression_with_the_same_catalog_digest(tmp_path):
    published_state = tmp_path / "published-state.json"
    local_state = tmp_path / "local-state.json"
    entry = {
        "catalog_digest": "sha256:" + "a" * 64,
        "catalog_snapshot": "2026-08-25",
        "packages_file": "data/production/npm-critical-packages.txt",
    }
    write_json(published_state, {"sources": {"npm": {**entry, "cursor": 2_295}}})
    write_json(local_state, {"sources": {"npm": {**entry, "cursor": 0}}})

    merged, before, after = module_merge_state("npm", published_state, local_state)

    assert merged is False
    assert (before, after) == (2_295, 0)


def test_merger_rejects_replay_of_an_older_catalog_generation(tmp_path):
    published_state = tmp_path / "published-state.json"
    local_state = tmp_path / "local-state.json"
    write_json(published_state, {"sources": {"npm": {
        "cursor": 2_295,
        "catalog_digest": "sha256:" + "b" * 64,
        "catalog_snapshot": "2026-08-26",
    }}})
    write_json(local_state, {"sources": {"npm": {
        "cursor": 0,
        "catalog_digest": "sha256:" + "a" * 64,
        "catalog_snapshot": "2026-08-25",
    }}})

    merged, before, after = module_merge_state("npm", published_state, local_state)

    assert merged is False
    assert (before, after) == (2_295, 0)


def test_merger_rejects_newer_snapshot_without_a_catalog_digest(tmp_path):
    published_state = tmp_path / "published-state.json"
    local_state = tmp_path / "local-state.json"
    write_json(published_state, {"sources": {"npm": {
        "cursor": 2_295,
        "catalog_snapshot": "2026-08-25",
    }}})
    write_json(local_state, {"sources": {"npm": {
        "cursor": 0,
        "catalog_snapshot": "2026-08-26",
    }}})

    merged, before, after = module_merge_state("npm", published_state, local_state)

    assert merged is False
    assert (before, after) == (2_295, 0)


def test_both_publishers_use_source_owned_merge():
    assert "bash tools/crawl_parallel.sh publish" in WORKFLOW
    assert "SOURCES=crates" in WORKFLOW
    assert "cp data/production/registry-state.json /tmp/artifact-publish" not in WORKFLOW
    assert "merge_registry_publication.py" in PARALLEL
    assert "merge_status" in PARALLEL


def test_ci_publisher_reports_whether_canonical_data_changed():
    publish = WORKFLOW.split("Publish resumable state and normalized observations", 1)[1].split(
        "Queue the next crawl or the generated refresh", 1
    )[0]

    assert "id: publish" in publish
    assert 'echo "changed=true" >> "$GITHUB_OUTPUT"' in publish
    assert 'echo "changed=false" >> "$GITHUB_OUTPUT"' in publish


def test_unchanged_crates_dump_does_not_republish_a_run_only_report_change():
    publish = WORKFLOW.split("Publish resumable state and normalized observations", 1)[1].split(
        "Queue the next crawl or the generated refresh", 1
    )[0]

    unchanged_guard = "'.sources.crates.unchanged == true'"
    assert unchanged_guard in publish
    assert publish.index(unchanged_guard) < publish.index("bash tools/crawl_parallel.sh publish")
    assert "The crates dump is unchanged; skipping canonical publication." in publish


def test_shared_publisher_retries_from_a_fresh_remote_snapshot():
    assert 'PUBLISH_MAX_ATTEMPTS="${PUBLISH_MAX_ATTEMPTS:-3}"' in PARALLEL
    assert 'local attempt="${PUBLISH_ATTEMPT:-1}"' in PARALLEL
    assert 'PUBLISH_ATTEMPT=$((attempt + 1)) publish' in PARALLEL
    assert 'PUBLISH_MAX_ATTEMPTS=3' in WORKFLOW
