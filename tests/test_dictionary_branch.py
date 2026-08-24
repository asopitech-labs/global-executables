import json
from pathlib import Path

import pytest

from global_executables.pipeline import rebuild
from global_executables.validation import validate_dictionary


ROOT = Path(__file__).parents[1]
RAW_DICTIONARY = (
    "https://raw.githubusercontent.com/"
    "asopitech-labs/global-executables/dictionary"
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text()


def test_public_readers_use_the_dictionary_branch():
    assert f'const RAW_BASE = "{RAW_DICTIONARY}"' in _read("playground/app.js")
    assert f'"url": "{RAW_DICTIONARY}"' in _read("playground/openapi.json")
    assert f'{RAW_DICTIONARY}/data/metadata.json' in _read("playground/index.html")


def test_refresh_restores_and_publishes_only_the_dictionary_branch():
    workflow = _read(".github/workflows/refresh.yml")
    restore = workflow.index("Restore published dictionary")
    rebuild_step = workflow.index("Rebuild from production and registry inputs")
    publish = workflow.index("Publish generated dictionary")

    assert restore < rebuild_step < publish
    assert "set -o pipefail" in workflow[restore:rebuild_step]
    assert "origin/dictionary" in workflow
    assert "push origin HEAD:dictionary" in workflow
    assert "push origin HEAD:main" not in workflow
    assert "gh workflow run pages.yml --ref main" in workflow
    publish_body = workflow[publish:]
    copied = publish_body.index("cp -a data/executables")
    validated = publish_body.index("--root /tmp/dictionary-publish")
    pushed = publish_body.index("push origin HEAD:dictionary")
    assert copied < validated < pushed
    assert "steps.publish_dictionary.outputs.changed == 'true'" in publish_body


def test_ci_dataset_readers_restore_the_dictionary_without_replacing_pr_code():
    freshness = _read(".github/workflows/freshness.yml")
    validate = _read(".github/workflows/validate.yml")

    assert "origin/dictionary" in freshness
    assert "origin/dictionary" in validate
    assert "/tmp/published-dictionary" in validate
    assert "tools/validate_dictionary.py" in validate
    assert "--integrity-only" in validate
    assert "fixtures/intermediate/*.jsonl" in validate


def test_pages_reads_dictionary_and_has_a_report_fallback():
    pages = _read(".github/workflows/pages.yml")
    assert "git fetch origin artifact-data dictionary" in pages
    assert "origin/dictionary:reports/production-crawl.json" in pages
    assert '"status":"unavailable"' in pages
    assert '--dictionary-commit "$DICTIONARY_COMMIT"' in pages


def test_program_branch_ignores_generated_dictionary_artifacts():
    ignored = _read(".gitignore").splitlines()
    assert {
        "data/executables/",
        "data/indexes/",
        "data/metadata.json",
        "data/history.json",
        "reports/production-crawl.json",
        "reports/production-refresh.json",
    } <= set(ignored)


def test_container_sources_the_dataset_from_dictionary():
    dockerfile = _read("Dockerfile")
    assert "COPY data ./data" not in dockerfile
    assert "tar.gz/refs/heads/dictionary" in dockerfile
    assert "GLOBAL_EXECUTABLES_DATASET_ROOT=/dictionary" in dockerfile


def test_published_dictionary_validator_checks_generated_contract(tmp_path):
    rebuild(tmp_path, sorted((ROOT / "fixtures/intermediate").glob("*.jsonl")),
            "2026-08-14")
    assert validate_dictionary(tmp_path, ROOT)["executables"] > 0

    metadata_path = tmp_path / "data/metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["unique_executables"] += 1
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="canonical file count"):
        validate_dictionary(tmp_path, ROOT)


def test_published_dictionary_validator_accepts_shell_builtins(tmp_path):
    source = tmp_path / "shell.jsonl"
    source.write_text(json.dumps({
        "command": "printf",
        "confidence": "direct",
        "ecosystem": "shell",
        "package": "bash",
        "repository": None,
        "source": "bash builtin",
        "source_type": "shell_builtin",
        "version": "5.2",
    }) + "\n")
    rebuild(tmp_path, [source], "2026-08-24")

    result = validate_dictionary(tmp_path, ROOT)
    assert result["executables"] == 1
    assert result["indexes"] > 0


def test_published_dictionary_validator_rejects_corruption_and_empty_data(tmp_path):
    populated = tmp_path / "populated"
    rebuild(populated, sorted((ROOT / "fixtures/intermediate").glob("*.jsonl")),
            "2026-08-14")
    index = next((populated / "data/indexes").glob("**/*.json"))
    index.write_text("[]\n")
    with pytest.raises(ValueError, match="index digest mismatch"):
        validate_dictionary(populated, ROOT, validate_schema=False)

    empty_input = tmp_path / "empty.jsonl"
    empty_input.write_text("")
    empty = tmp_path / "empty"
    empty.mkdir()
    rebuild(empty, [empty_input], "2026-08-14")
    (empty / "data/executables").mkdir()
    (empty / "data/indexes").mkdir()
    with pytest.raises(ValueError, match="at least one executable"):
        validate_dictionary(empty, ROOT)
