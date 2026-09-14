import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools/build_playground.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("build_playground", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(output: Path, crawl: Path, recipes: Path) -> Path:
    builder = load_builder()
    argv = sys.argv
    sys.argv = ["build_playground.py", "--output", str(output), "--crawl-report", str(crawl),
                "--recipe-report", str(recipes)]
    try:
        cwd = Path.cwd()
        import os
        os.chdir(ROOT)
        try:
            builder.main()
        finally:
            os.chdir(cwd)
    finally:
        sys.argv = argv
    return output / "status.json"


def test_next_ci_refresh_is_every_six_hours_at_minute_17():
    builder = load_builder()

    before = datetime(2026, 8, 25, 6, 16, tzinfo=timezone.utc)
    after = datetime(2026, 8, 25, 6, 18, tzinfo=timezone.utc)

    assert builder.next_crawl(before) == datetime(2026, 8, 25, 6, 17, tzinfo=timezone.utc)
    assert builder.next_crawl(after) == datetime(2026, 8, 25, 12, 17, tzinfo=timezone.utc)
    assert builder.CRAWL_SCHEDULE == "17 */6 * * *"


def test_playground_fallback_and_placeholder_match_ci_refresh_schedule():
    app = (ROOT / "playground/app.js").read_text()
    page = (ROOT / "playground/index.html").read_text()
    builder = SCRIPT.read_text()

    for contents in (app, page, builder):
        assert "17 */6 * * *" in contents or "every six hours" in contents
    assert "hour - (hour % 6) + 6" in app
    assert 'state.status?.schedule || "17 */6 * * *"' in app
    assert "Next CI registry refresh" in page


def test_playground_shows_ci_refresh_position_for_every_refreshed_registry():
    app = (ROOT / "playground/app.js").read_text()

    assert '["npm", "pypi", "crates", "go", "rubygems", "packagist", "nuget", "conan"]' in app
    assert "source.refresh_cursor" in app
    assert "refresh ${formatNumber(source.refresh_cursor)} / ${formatNumber(source.catalog_size)}" in app


def test_playground_discloses_the_breaking_npm_coverage_change():
    page = (ROOT / "playground/index.html").read_text()
    app = (ROOT / "playground/app.js").read_text()

    for required in (
        'id="npm-coverage-notice"',
        "Breaking npm coverage change",
        "We apologize",
        "4.3-million-package",
        "218,774",
        "221,774",
        "2,295",
        "npm overall is not 100% covered",
        "issues/54",
    ):
        assert required in page
    assert page.index('id="npm-coverage-notice"') < page.index('class="hero"')
    assert "critical 100%" in app


def test_forecast_uses_net_backlog_change_from_published_history():
    builder = load_builder()
    history = [
        {"observed_at": "2026-09-01T00:00:00Z", "source": {"cursor": 100, "catalog_size": 1_000, "retry_pending": 0}},
        {"observed_at": "2026-09-02T00:00:00Z", "source": {"cursor": 300, "catalog_size": 1_100, "retry_pending": 0}},
    ]

    forecast = builder.completion_forecast(history)

    assert forecast["status"] == "estimated"
    assert forecast["remaining_work"] == 800
    assert forecast["backlog_change_per_day"] == -100
    assert forecast["estimated_completion_at"] == "2026-09-10T00:00:00Z"


def test_forecast_refuses_a_completion_date_when_backlog_is_growing():
    builder = load_builder()
    history = [
        {"observed_at": "2026-09-01T00:00:00Z", "source": {"cursor": 100, "catalog_size": 1_000, "retry_pending": 0}},
        {"observed_at": "2026-09-02T00:00:00Z", "source": {"cursor": 200, "catalog_size": 1_200, "retry_pending": 0}},
    ]

    forecast = builder.completion_forecast(history)

    assert forecast["status"] == "non_converging"
    assert forecast["backlog_change_per_day"] == 100
    assert forecast["estimated_completion_at"] is None


def test_forecast_orders_mixed_timezone_offsets_chronologically():
    builder = load_builder()
    history = [
        {"observed_at": "2026-08-31T16:00:00Z", "source": {"cursor": 300, "catalog_size": 1_100}},
        {"observed_at": "2026-09-01T00:00:00+09:00", "source": {"cursor": 100, "catalog_size": 1_000}},
    ]

    forecast = builder.completion_forecast(history)

    assert forecast["status"] == "estimated"
    assert forecast["backlog_change_per_day"] == -2_400
    assert forecast["estimated_completion_at"] == "2026-09-01T00:00:00Z"


def test_forecast_ignores_old_publication_catchup_outside_recent_window():
    builder = load_builder()
    history = [
        {"observed_at": "2026-09-01T00:00:00Z", "source": {"cursor": 0, "catalog_size": 2_000}},
        {"observed_at": "2026-09-02T00:00:00Z", "source": {"cursor": 200, "catalog_size": 1_000}},
        {"observed_at": "2026-09-03T00:00:00Z", "source": {"cursor": 300, "catalog_size": 1_000}},
    ]

    forecast = builder.completion_forecast(history)

    assert forecast["samples"] == 2
    assert forecast["backlog_change_per_day"] == -100
    assert forecast["estimated_completion_at"] == "2026-09-10T00:00:00Z"


def test_pages_pipeline_exports_history_and_renders_forecast():
    workflow = (ROOT / ".github/workflows/pages.yml").read_text()
    app = (ROOT / "playground/app.js").read_text()
    page = (ROOT / "playground/index.html").read_text()

    assert "go-crawl-history.jsonl" in workflow
    assert "--crawl-history /tmp/go-crawl-history.jsonl" in workflow
    assert 'id="forecast-title"' in page
    assert 'state.status?.forecast' in app
    assert "forecast.backlog_change_per_day == null" in app


def test_status_carries_the_recipe_snapshot_report(tmp_path):
    crawl = tmp_path / "crawl.json"
    crawl.write_text(json.dumps({"status": "success", "coverage_kind": "partial", "sources": {}}))
    recipes = tmp_path / "cpp.json"
    recipes.write_text(json.dumps({"status": "success", "coverage_kind": "partial", "sources": {
        "vcpkg": {"coverage_kind": "partial", "records": 917,
                  "indexes": [{"packages": 2862, "declaring_packages": 278}]},
        "xmake": {"coverage_kind": "partial", "records": 114,
                  "indexes": [{"packages": 2004, "declaring_packages": 114}]},
    }}))
    output = tmp_path / "site"
    status = json.loads(_build(output, crawl, recipes) .read_text())

    assert status["recipe_report"]["sources"]["vcpkg"]["records"] == 917
    assert status["recipe_report"]["sources"]["xmake"]["records"] == 114
    # A recipe repository is read whole every run, so it never reports a cursor.
    assert "cursor" not in status["recipe_report"]["sources"]["vcpkg"]


def test_status_reports_an_uncollected_recipe_repository_rather_than_omitting_it(tmp_path):
    crawl = tmp_path / "crawl.json"
    crawl.write_text(json.dumps({"status": "success", "coverage_kind": "partial", "sources": {}}))
    output = tmp_path / "site"
    status = json.loads(_build(output, crawl, tmp_path / "missing.json").read_text())

    assert status["recipe_report"] == {"status": "unavailable", "coverage_kind": "partial", "sources": {}}


def test_playground_renders_recipe_snapshots_without_a_cursor():
    app = (ROOT / "playground/app.js").read_text()
    page = (ROOT / "playground/index.html").read_text()

    assert 'id="recipe-grid"' in page
    assert '["vcpkg", "xmake"]' in app
    assert "state.status?.recipe_report?.sources" in app
    assert "packages declare a command" in app
