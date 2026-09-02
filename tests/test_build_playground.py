import importlib.util
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


def test_next_crates_check_is_daily_at_0447_utc():
    builder = load_builder()

    before = datetime(2026, 8, 25, 4, 46, tzinfo=timezone.utc)
    after = datetime(2026, 8, 25, 4, 48, tzinfo=timezone.utc)

    assert builder.next_crawl(before) == datetime(2026, 8, 25, 4, 47, tzinfo=timezone.utc)
    assert builder.next_crawl(after) == datetime(2026, 8, 26, 4, 47, tzinfo=timezone.utc)
    assert builder.CRAWL_SCHEDULE == "47 4 * * *"


def test_playground_fallback_and_placeholder_match_daily_schedule():
    app = (ROOT / "playground/app.js").read_text()
    page = (ROOT / "playground/index.html").read_text()
    builder = SCRIPT.read_text()

    for contents in (app, page, builder):
        assert "47 */6 * * *" not in contents
        assert "every six hours" not in contents
    assert "setUTCHours(4, 47, 0, 0)" in app
    assert 'state.status?.schedule || "47 4 * * *"' in app
    assert "daily crates.io change check" in page


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
