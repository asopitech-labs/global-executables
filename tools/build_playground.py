#!/usr/bin/env python3
"""Assemble the small GitHub Pages shell and its crawl-status snapshot."""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path


CRAWL_SCHEDULE = "47 4 * * *"
CRAWL_HOUR = 4
CRAWL_MINUTE = 47
FORECAST_WINDOW = timedelta(hours=24)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def completion_forecast(history: list[dict]) -> dict:
    observations = []
    previous = None
    dated = [item for item in history if item.get("observed_at")]
    if dated:
        latest = max(_timestamp(item["observed_at"]) for item in dated)
        dated = [item for item in dated if _timestamp(item["observed_at"]) >= latest - FORECAST_WINDOW]
    for item in sorted(dated, key=lambda value: _timestamp(value["observed_at"])):
        source = item.get("source") or {}
        state = (
            int(source.get("cursor") or 0),
            int(source.get("catalog_size") or 0),
            int(source.get("retry_pending") or 0),
        )
        if state[1] <= 0 or state == previous:
            continue
        observations.append((_timestamp(item["observed_at"]), state))
        previous = state

    result = {
        "source": "go",
        "model": "rolling-24h-published-net-backlog-v1",
        "window_hours": 24,
        "status": "insufficient_data",
        "samples": len(observations),
        "observed_from": _iso(observations[0][0]) if observations else None,
        "observed_to": _iso(observations[-1][0]) if observations else None,
        "remaining_work": None,
        "backlog_change_per_day": None,
        "estimated_completion_at": None,
    }
    if not observations:
        return result

    latest_time, (cursor, catalog_size, retry_pending) = observations[-1]
    remaining = max(0, catalog_size - cursor) + retry_pending
    result["remaining_work"] = remaining
    result["progress_percent"] = round(cursor / catalog_size * 100, 2)
    if remaining == 0:
        result["status"] = "complete"
        result["estimated_completion_at"] = _iso(latest_time)
        return result
    if len(observations) < 2:
        return result

    first_time, (first_cursor, first_catalog, first_retry) = observations[0]
    elapsed_days = (latest_time - first_time).total_seconds() / 86_400
    if elapsed_days <= 0:
        return result
    first_remaining = max(0, first_catalog - first_cursor) + first_retry
    change_per_day = (remaining - first_remaining) / elapsed_days
    result["backlog_change_per_day"] = round(change_per_day, 1)
    if change_per_day >= 0:
        result["status"] = "non_converging"
        return result

    result["status"] = "estimated"
    result["estimated_completion_at"] = _iso(
        latest_time + timedelta(days=remaining / -change_per_day)
    )
    return result


def registry_progress(report: dict) -> float | None:
    sources = [
        source for source in (report.get("sources") or {}).values()
        if int(source.get("catalog_size") or 0) > 0
    ]
    catalog_size = sum(int(source["catalog_size"]) for source in sources)
    if not catalog_size:
        return None
    cursor = sum(min(int(source.get("cursor") or 0), int(source["catalog_size"])) for source in sources)
    return round(cursor / catalog_size * 100, 2)


def next_crawl(now: datetime) -> datetime:
    candidates = []
    for day in range(2):
        date = (now + timedelta(days=day)).date()
        candidates.append(
            datetime(
                date.year,
                date.month,
                date.day,
                CRAWL_HOUR,
                CRAWL_MINUTE,
                tzinfo=timezone.utc,
            )
        )
    return next(value for value in candidates if value > now)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--crawl-report", type=Path, required=True)
    # The OS indexes are crawled by a separate pipeline, so their coverage never
    # reached the page even though it is the exhaustive half of the dataset.
    parser.add_argument("--production-report", type=Path, default=Path("reports/production-crawl.json"))
    parser.add_argument("--crawl-history", type=Path)
    parser.add_argument("--artifact-data-commit", default="")
    parser.add_argument("--dictionary-commit", default="")
    parser.add_argument("--main-commit", default="")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for source in (Path("playground/index.html"), Path("playground/styles.css"), Path("playground/app.js"), Path("playground/openapi.json")):
        shutil.copy2(source, args.output / source.name)
    report = json.loads(args.crawl_report.read_text()) if args.crawl_report.is_file() else {
        "status": "unavailable", "coverage_kind": "partial", "sources": {}
    }
    production = json.loads(args.production_report.read_text()) if args.production_report.is_file() else {
        "status": "unavailable", "coverage_kind": "partial", "sources": {}
    }
    now = datetime.now(timezone.utc)
    history = []
    if args.crawl_history and args.crawl_history.is_file():
        history = [json.loads(line) for line in args.crawl_history.read_text().splitlines() if line.strip()]
    forecast = completion_forecast(history)
    forecast["overall_progress_percent"] = registry_progress(report)
    status = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "next_crawl_at": next_crawl(now).isoformat().replace("+00:00", "Z"),
        "schedule": CRAWL_SCHEDULE,
        "artifact_data_commit": args.artifact_data_commit,
        "dictionary_commit": args.dictionary_commit,
        "main_commit": args.main_commit,
        "crawl_report": report,
        "production_report": production,
        "forecast": forecast,
        "freshness": {"status": "published", "coverage_kind": report.get("coverage_kind", "partial")},
    }
    (args.output / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
