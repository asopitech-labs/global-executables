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
parser.add_argument("--report", type=Path, default=Path("reports/refresh.json"))
args = parser.parse_args()

missing = [str(path) for path in args.inputs if not path.is_file()]
report = {"snapshot": args.snapshot, "coverage_kind": args.coverage_kind, "inputs": [str(p) for p in args.inputs], "failed": missing}
if missing:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    raise SystemExit(f"refresh blocked; missing collector inputs: {', '.join(missing)}")

rebuild(args.root, args.inputs, args.snapshot, args.coverage_kind)
report["status"] = "success"
report["records"] = sum(1 for path in args.inputs for line in path.read_text().splitlines() if line.strip())
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps(report, indent=2, sort_keys=True))
