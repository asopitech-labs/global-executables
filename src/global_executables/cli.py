from __future__ import annotations
import argparse, json
from datetime import datetime
from pathlib import Path
from .collectors import crates_manifest, homebrew_metadata, npm_metadata, package_files
from .model import write_jsonl
from .pipeline import RebuildPolicy, rebuild
from .assessment import assess
from .freshness import run_scan
from .search import Dataset

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="action",required=True)
    build=sub.add_parser("build"); build.add_argument("inputs",nargs="+",type=Path); build.add_argument("--root",type=Path,default=Path.cwd()); build.add_argument("--snapshot"); build.add_argument("--coverage-kind",choices=["fixture","smoke","partial","exhaustive"],default="fixture"); build.add_argument("--allow-shrink-reason")
    collect=sub.add_parser("collect"); collect.add_argument("ecosystem",choices=["debian","ubuntu","arch","homebrew","npm","crates"]); collect.add_argument("input",type=Path); collect.add_argument("output",type=Path)
    query=sub.add_parser("assess"); query.add_argument("name"); query.add_argument("--root",type=Path,default=Path.cwd()); query.add_argument("--scope",action="append",default=[],help="dimension=value; repeatable")
    freshness=sub.add_parser("freshness", help="run one bounded partial freshness scan")
    freshness.add_argument("--manifest", type=Path, required=True)
    freshness.add_argument("--root", type=Path, default=Path.cwd())
    freshness.add_argument("--state", type=Path, default=Path("data/freshness/state.json"))
    freshness.add_argument("--report", type=Path, default=Path("reports/freshness.json"))
    freshness.add_argument("--partition-budget", type=int, default=1)
    freshness.add_argument("--record-budget", type=int, default=1000)
    freshness.add_argument("--byte-budget", type=int)
    freshness.add_argument("--retries", type=int, default=2)
    freshness.add_argument("--backoff-seconds", type=float, default=1.0)
    freshness.add_argument("--observed-at", help="UTC ISO timestamp, primarily for reproducible runs")
    a=p.parse_args()
    if a.action=="build":
        rebuild(a.root, a.inputs, a.snapshot, a.coverage_kind,
                policy=RebuildPolicy(shrink_reason=a.allow_shrink_reason))
        return
    if a.action=="assess":
        scope=dict(item.split("=",1) for item in a.scope)
        print(json.dumps(assess(Dataset(a.root),a.name,scope),indent=2,sort_keys=True)); return
    if a.action=="freshness":
        observed_at = datetime.fromisoformat(a.observed_at.replace("Z", "+00:00")) if a.observed_at else None
        report = run_scan(a.root, a.manifest, a.state, a.report, a.partition_budget,
                          a.record_budget, a.byte_budget, observed_at,
                          retries=a.retries, backoff_seconds=a.backoff_seconds)
        print(json.dumps(report, indent=2, sort_keys=True))
        if report["status"] != "success":
            raise SystemExit(1)
        return
    if a.ecosystem in {"debian","ubuntu","arch"}: rows=package_files(a.input.read_text(),a.ecosystem,str(a.input))
    elif a.ecosystem=="npm": rows=npm_metadata(json.loads(a.input.read_text()),str(a.input))
    elif a.ecosystem=="homebrew": rows=homebrew_metadata(json.loads(a.input.read_text()),str(a.input))
    else:
        value=json.loads(a.input.read_text()); rows=crates_manifest(value["manifest"],value["package"],value.get("version"),value.get("repository"),str(a.input))
    write_jsonl(sorted(rows,key=lambda r:(r["command"],r["package"])),a.output)
