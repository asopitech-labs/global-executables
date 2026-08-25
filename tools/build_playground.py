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
    status = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "next_crawl_at": next_crawl(now).isoformat().replace("+00:00", "Z"),
        "schedule": CRAWL_SCHEDULE,
        "artifact_data_commit": args.artifact_data_commit,
        "dictionary_commit": args.dictionary_commit,
        "main_commit": args.main_commit,
        "crawl_report": report,
        "production_report": production,
        "freshness": {"status": "published", "coverage_kind": report.get("coverage_kind", "partial")},
    }
    (args.output / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
