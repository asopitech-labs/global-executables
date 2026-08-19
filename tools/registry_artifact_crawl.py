#!/usr/bin/env python3
"""Run a bounded, resumable artifact crawl for language package registries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from global_executables.registry_artifact import crawl_registry_sources

parser = argparse.ArgumentParser()
parser.add_argument("--source", action="append", choices=["npm", "pypi", "crates", "go", "rubygems", "packagist"], required=True)
parser.add_argument("--state", type=Path, default=Path("data/production/registry-state.json"))
parser.add_argument("--output-dir", type=Path, default=Path("data/production/intermediate"))
parser.add_argument("--report", type=Path, default=Path("reports/registry-artifact-crawl.json"))
parser.add_argument("--package-budget", type=int, default=100)
parser.add_argument("--source-package-budget", action="append", default=[], metavar="SOURCE=N",
                    help="raise or lower --package-budget for one source, e.g. crates=10000")
parser.add_argument("--byte-budget", type=int, default=500_000_000)
parser.add_argument("--timeout", type=int, default=120)
args = parser.parse_args()
source_budgets = {}
for override in args.source_package_budget:
    source, _, value = override.partition("=")
    if source not in args.source or not value.isdigit():
        parser.error(f"--source-package-budget expects SOURCE=N for a selected source: {override}")
    source_budgets[source] = int(value)
report = crawl_registry_sources(args.source, args.state, args.output_dir, args.report,
                                args.package_budget, args.byte_budget, args.timeout, source_budgets)
print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
if report["status"] != "success":
    raise SystemExit(1)
