from pathlib import Path


ROOT = Path(__file__).parents[1]


def workflow(name: str) -> str:
    return (ROOT / ".github/workflows" / name).read_text()


def trigger_block(contents: str) -> str:
    return contents.split("permissions:", 1)[0]


def test_durable_environment_samples_are_manual_only():
    base_commands = trigger_block(workflow("base-commands.yml"))

    assert "workflow_dispatch:" in base_commands
    assert "schedule:" not in base_commands


def test_fixture_freshness_is_manual_only_and_does_not_restore_dictionary():
    freshness = workflow("freshness.yml")

    assert "workflow_dispatch:" in trigger_block(freshness)
    assert "schedule:" not in trigger_block(freshness)
    assert "fixtures/freshness/manifest.json" in freshness
    assert "origin/dictionary" not in freshness


def test_crates_change_check_is_daily_and_restores_only_owned_crawl_data():
    registry = workflow("registry-artifacts.yml")
    triggers = trigger_block(registry)
    restore = registry.split("Restore resumable registry state", 1)[1].split(
        "Run bounded artifact inspection", 1
    )[0]

    assert 'cron: "47 4 * * *"' in triggers
    assert "*/6" not in triggers
    assert "registry-state.json" in restore
    assert "crates.jsonl" in restore
    for unrelated in (
        "npm-packages",
        "pypi-projects",
        "rubygems-names",
        "packagist-packages",
        "go-modules",
        "nuget-tools",
    ):
        assert unrelated not in restore
    assert "for source in" not in restore


def test_registry_derivations_run_only_after_a_changed_publication():
    registry = workflow("registry-artifacts.yml")
    publish = registry.split("Publish resumable state and normalized observations", 1)[1].split(
        "Queue the next crawl or the generated refresh", 1
    )[0]
    queue = registry.split("Queue the next crawl or the generated refresh", 1)[1]

    assert "id: publish" in publish
    assert "'.sources.crates.unchanged == true'" in publish
    assert 'echo "changed=true" >> "$GITHUB_OUTPUT"' in publish
    assert "steps.publish.outputs.changed" in queue
    assert 'test "$PUBLISHED_CHANGED" = true' in queue
    assert "gh workflow run refresh.yml" in queue
    assert "gh workflow run pages.yml" not in registry


def test_pages_has_no_duplicate_registry_completion_trigger():
    pages = trigger_block(workflow("pages.yml"))

    assert "workflow_dispatch:" in pages
    assert "workflow_run:" not in pages


def test_feature_pull_requests_do_not_also_run_push_validation():
    validate = trigger_block(workflow("validate.yml"))

    assert "pull_request:" in validate
    assert "push:\n    branches: [main]" in validate


def test_weekly_upstream_smoke_remains_a_bounded_live_monitor():
    smoke = workflow("upstream-smoke.yml")

    assert "schedule:" in trigger_block(smoke)
    assert "tools/live_smoke.py" in smoke
