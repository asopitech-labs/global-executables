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
