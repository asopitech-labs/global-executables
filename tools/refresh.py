#!/usr/bin/env python3
"""Run a fail-closed, reproducible dataset refresh from normalized inputs."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from global_executables.pipeline import rebuild


parser = argparse.ArgumentParser()
parser.add_argument("inputs", nargs="+", type=Path)
parser.add_argument("--root", type=Path, default=Path.cwd())
parser.add_argument("--snapshot", default=date.today().isoformat())
parser.add_argument("--coverage-kind", choices=["fixture", "smoke", "partial", "exhaustive"], default="partial")
parser.add_argument("--coverage-map", type=Path, help="JSON object mapping input stem to coverage kind")
parser.add_argument("--report", type=Path, default=Path("reports/refresh.json"))
args = parser.parse_args()

missing = [str(path) for path in args.inputs if not path.is_file()]
report = {"snapshot": args.snapshot, "coverage_kind": args.coverage_kind, "inputs": [str(p) for p in args.inputs], "failed": missing}
if missing:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    raise SystemExit(f"refresh blocked; missing collector inputs: {', '.join(missing)}")

coverage = json.loads(args.coverage_map.read_text()) if args.coverage_map else args.coverage_kind
if not isinstance(coverage, (str, dict)):
    raise SystemExit("coverage map must be a JSON object")
rebuild(args.root, args.inputs, args.snapshot, coverage)
report["status"] = "success"
report["records"] = sum(1 for path in args.inputs for line in path.read_text().splitlines() if line.strip())
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps(report, indent=2, sort_keys=True))
