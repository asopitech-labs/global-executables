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
    assert "tools/registry_state.py restore --ref origin/artifact-data" in restore
    assert "--state data/production/registry-state" in restore
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


def test_daily_ci_owns_the_bounded_npm_critical_population():
    registry = workflow("registry-artifacts.yml")
    npm_job = registry.split("  npm-critical:", 1)[1]

    assert "needs: crawl" in npm_job
    assert "tools/npm_critical_catalog.py" in npm_job
    assert '"catalog_digest": "sha256:" + hashlib.sha256' in npm_job
    assert "--source npm --passes 0" in npm_job
    assert "SOURCES=npm" in npm_job
    assert "ALLOW_CI_OWNED_NPM=1" in npm_job
    assert 'SOURCES="${SOURCES-pypi rubygems packagist nuget go}"' in (
        ROOT / "tools/crawl_parallel.sh"
    ).read_text()


def test_local_crawlers_are_completion_boosters_not_refresh_workers():
    parallel = (ROOT / "tools/crawl_parallel.sh").read_text()

    start = parallel.split("start()", 1)[1].split("status()", 1)[0]
    watch = parallel.split("watch()", 1)[1]
    assert "--continuous" not in start
    assert "CONTINUOUS=1" not in start
    assert 'gh workflow run refresh.yml --ref main' not in watch


def test_ci_periodically_refreshes_every_completed_registry_in_parallel():
    refresh = workflow("registry-refresh.yml")
    triggers = trigger_block(refresh)

    assert 'cron: "17 */6 * * *"' in triggers
    assert "matrix:" in refresh and "max-parallel: 5" in refresh
    for source in ("go", "pypi", "rubygems", "packagist", "nuget"):
        assert source in refresh
    assert 'select(.cursor >= .catalog_size)' in refresh
    assert 'select((.catalog_size | type) == "number")' in refresh
    assert "--passes 1 --continuous" in refresh
    assert "tools/registry_artifact_crawl.py" in refresh
    assert 'observations changed' in refresh
    assert 'gh workflow run refresh.yml --ref main' in refresh
    assert 'gh workflow run pages.yml --ref main' in refresh
    assert "grep -qx 'published'" in refresh
    assert refresh.index('bash tools/crawl_parallel.sh publish)') < refresh.index(
        'gh workflow run pages.yml --ref main'
    )


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


def test_conan_walk_is_daily_resumable_and_restores_only_its_own_catalogue():
    cpp = workflow("cpp-registries.yml")
    triggers = trigger_block(cpp)
    restore = cpp.split("Restore the resumable ConanCenter walk", 1)[1].split(
        "Inspect a bounded batch of ConanCenter packages", 1
    )[0]

    assert 'cron: "23 5 * * *"' in triggers
    assert "*/6" not in triggers
    assert "tools/registry_state.py restore --ref origin/artifact-data" in restore
    assert "--state data/production/registry-state" in restore
    # The published cursor counts positions in this catalogue, so resuming without it
    # would walk a different list from the one the cursor described.
    assert "conan-recipes.txt.gz" in restore
    assert "conan.jsonl" in restore
    for unrelated in ("npm-packages", "pypi-projects", "rubygems-names",
                      "packagist-packages", "go-modules", "nuget-tools", "crates.jsonl"):
        assert unrelated not in restore
    assert "--source conan" in cpp
    assert "SOURCES=conan" in cpp


def test_ci_does_not_fetch_the_repository_history_for_data_jobs():
    for name in ("base-commands.yml", "cpp-registries.yml", "freshness.yml",
                 "pages.yml", "registry-artifacts.yml", "registry-refresh.yml"):
        assert "fetch-depth: 0" not in workflow(name)

    cpp = workflow("cpp-registries.yml")
    assert cpp.count("fetch-depth: 1") == 2
    assert cpp.count("git fetch --depth=1 origin artifact-data") == 2

    pages = workflow("pages.yml")
    assert "git fetch --shallow-since='7 days ago' origin artifact-data" in pages
    assert "git fetch --depth=1 origin dictionary" in pages

    for path in (ROOT / "tools/crawl_parallel.sh", ROOT / "tools/crawl_container.sh"):
        contents = path.read_text()
        assert "git fetch --quiet --depth=1 origin artifact-data" in contents
        assert "git fetch origin artifact-data --quiet" not in contents

    assert "if: always()" not in cpp
    registry = workflow("registry-artifacts.yml")
    assert "if: always()" not in registry
    assert "!cancelled()" in cpp and "!cancelled()" in registry


def test_conan_continuation_is_keyed_on_the_cursor_not_on_coverage():
    queue = workflow("cpp-registries.yml").split(
        "Continue the walk or refresh the dictionary", 1)[1]

    # ConanCenter is never exhaustive while recipes remain that nobody has built, so a
    # self-dispatch keyed on coverage would queue a run forever.
    assert "cursor < .sources.conan.catalog_size" in queue
    assert "coverage_kind" not in queue
    assert "gh workflow run cpp-registries.yml" in queue
    assert 'test "$PUBLISHED_CHANGED" = true' in queue
    assert "gh workflow run refresh.yml" in queue


def test_recipe_snapshots_publish_as_observations_and_derive_only_on_change():
    cpp = workflow("cpp-registries.yml")
    recipes = cpp.split("  recipes:", 1)[1]

    assert "--source vcpkg --source xmake" in recipes
    assert "OBSERVATION_SOURCES='vcpkg xmake'" in recipes
    assert "SOURCES=''" in recipes
    assert "steps.publish.outputs.changed == 'true'" in recipes
    assert "gh workflow run refresh.yml --ref main" in recipes


def test_recipe_snapshot_report_reaches_the_published_page():
    cpp = workflow("cpp-registries.yml")
    pages = workflow("pages.yml")
    parallel = (ROOT / "tools/crawl_parallel.sh").read_text()

    # A snapshot collector advances no cursor, so it has nothing to merge into the
    # registry report and needs its own published report to stay visible.
    assert "OBSERVATION_REPORT=reports/cpp-registry-crawl.json" in cpp
    assert 'OBSERVATION_REPORT:-' in parallel
    assert "origin/artifact-data:reports/cpp-registry-crawl.json" in pages
    assert "--recipe-report /tmp/cpp-registry-crawl.json" in pages
