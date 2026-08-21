#!/usr/bin/env python3
"""Acquire real upstream indexes and emit normalized production intermediates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from global_executables.production import SOURCE_URLS, crawl_sources

parser = argparse.ArgumentParser()
parser.add_argument("--source", action="append", choices=sorted(SOURCE_URLS), dest="sources")
parser.add_argument("--output-dir", type=Path, default=Path("data/production/intermediate"))
parser.add_argument("--report", type=Path, default=Path("reports/production-crawl.json"))
parser.add_argument("--timeout", type=int, default=300)
args = parser.parse_args()

sources = args.sources or sorted(SOURCE_URLS)
report = crawl_sources(sources, args.output_dir, args.report, args.timeout)
print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
if report["status"] == "failed":
    raise SystemExit(1)
