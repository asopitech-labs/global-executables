#!/usr/bin/env python3
"""Run a fail-closed, reproducible dataset refresh from normalized inputs."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path

from global_executables.pipeline import RebuildPolicy, rebuild


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--snapshot", default=date.today().isoformat())
    parser.add_argument("--coverage-kind", choices=["fixture", "smoke", "partial", "exhaustive"], default="partial")
    parser.add_argument("--coverage-map", type=Path, help="JSON object mapping input stem to coverage kind")
    parser.add_argument("--report", type=Path, default=Path("reports/refresh.json"))
    parser.add_argument("--allow-shrink-reason",
                        help="required explanation when intentionally publishing fewer executable names")
    args = parser.parse_args(argv)

    missing = [str(path) for path in args.inputs if not path.is_file()]
    report = {"snapshot": args.snapshot, "coverage_kind": args.coverage_kind,
              "inputs": [str(p) for p in args.inputs], "failed": missing}
    if missing:
        report["status"] = "failed"
        _write_report(args.report, report)
        raise SystemExit(f"refresh blocked; missing collector inputs: {', '.join(missing)}")

    coverage = json.loads(args.coverage_map.read_text()) if args.coverage_map else args.coverage_kind
    if not isinstance(coverage, (str, dict)):
        raise SystemExit("coverage map must be a JSON object")
    try:
        result = rebuild(
            args.root,
            args.inputs,
            args.snapshot,
            coverage,
            policy=RebuildPolicy(shrink_reason=args.allow_shrink_reason),
        )
    except Exception as error:
        report.update({"status": "failed", "error": str(error)})
        _write_report(args.report, report)
        raise
    report.update(asdict(result))
    report["status"] = "success"
    # Preserve the old field while making accepted/rejected accounting explicit.
    report["records"] = result.input_records
    report["accepted_records"] = result.input_records - result.rejected_records
    _write_report(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
