from __future__ import annotations
import argparse, json, sys
from datetime import datetime
from pathlib import Path
from .collectors import crates_manifest, homebrew_metadata, npm_metadata, package_files
from .model import write_jsonl
from .pipeline import RebuildPolicy, rebuild
from .assessment import assess
from .freshness import run_scan
from .search import Dataset
from .installation import resolve_installation
from .coverage import coverage_warnings
from .discovery import PackageDiscovery
from .on_demand import persist_observation, reconcile_indexed

def main(argv=None):
    argv=sys.argv[1:] if argv is None else list(argv)
    if argv[:1]==["mcp"]:
        # Forward everything after "mcp" so `uvx global-executables mcp --help` shows the server's options.
        from .mcp_server import main as mcp_main
        return mcp_main(argv[1:])
    p=argparse.ArgumentParser(prog="global-executables"); sub=p.add_subparsers(dest="action",required=True)
    sub.add_parser("mcp", help="run the read-only MCP server (stdio by default; see `mcp --help`)")
    build=sub.add_parser("build"); build.add_argument("inputs",nargs="+",type=Path); build.add_argument("--root",type=Path,default=Path.cwd()); build.add_argument("--snapshot"); build.add_argument("--coverage-kind",choices=["fixture","smoke","partial","exhaustive"],default="fixture"); build.add_argument("--allow-shrink-reason")
    collect=sub.add_parser("collect"); collect.add_argument("ecosystem",choices=["debian","ubuntu","arch","homebrew","npm","crates"]); collect.add_argument("input",type=Path); collect.add_argument("output",type=Path)
    discover=sub.add_parser("discover-package", help="verify commands published by one npm or PyPI package")
    discover.add_argument("ecosystem", choices=["npm", "pypi"])
    discover.add_argument("package")
    discover.add_argument("--version")
    discover.add_argument("--command")
    discover.add_argument("--target", default="{}", help="target constraints as a JSON object")
    discover.add_argument("--cache-dir", type=Path)
    discover.add_argument("--offline", action="store_true")
    discover.add_argument("--timeout", type=float, default=20.0)
    persist=sub.add_parser("persist-discovery", help="reverify and persist one package to artifact-data")
    persist.add_argument("ecosystem", choices=["npm", "pypi"])
    persist.add_argument("package")
    persist.add_argument("--artifact-root", type=Path, required=True)
    persist.add_argument("--cache-dir", type=Path)
    persist.add_argument("--timeout", type=float, default=20.0)
    reconcile=sub.add_parser("reconcile-discovery", help="mark durable claims indexed after dictionary read-back")
    reconcile.add_argument("--artifact-root", type=Path, required=True)
    reconcile.add_argument("--dictionary-root", type=Path, required=True)
    query=sub.add_parser("assess"); query.add_argument("name"); query.add_argument("--root",type=Path,default=Path.cwd()); query.add_argument("--scope",action="append",default=[],help="dimension=value; repeatable")
    for action in ("resolve-installation", "resolve-installations"):
        resolve = sub.add_parser(action, help="show environment-specific installation guidance")
        resolve.add_argument("names", nargs=1 if action == "resolve-installation" else "+")
        resolve.add_argument("--root", type=Path, default=Path.cwd())
        resolve.add_argument("--environment", required=True, help="target environment as a JSON object")
        resolve.add_argument("--preferences", default="{}", help="optional package preference as a JSON object")
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
    a=p.parse_args(argv)
    if a.action=="build":
        rebuild(a.root, a.inputs, a.snapshot, a.coverage_kind,
                policy=RebuildPolicy(shrink_reason=a.allow_shrink_reason))
        return
    if a.action=="assess":
        scope=dict(item.split("=",1) for item in a.scope)
        print(json.dumps(assess(Dataset(a.root),a.name,scope),indent=2,sort_keys=True)); return
    if a.action == "discover-package":
        try:
            target = json.loads(a.target)
            if not isinstance(target, dict):
                raise ValueError("--target must be a JSON object")
            result = PackageDiscovery(a.cache_dir, offline=a.offline, timeout=a.timeout).discover(
                a.ecosystem, a.package, a.version, a.command, target)
        except (ValueError, TypeError) as error:
            p.error(str(error))
        print(json.dumps(result, indent=2, sort_keys=True)); return
    if a.action == "persist-discovery":
        observation = PackageDiscovery(a.cache_dir, timeout=a.timeout).discover(
            a.ecosystem, a.package, revalidate=True)
        try:
            result = persist_observation(a.artifact_root, observation)
        except ValueError as error:
            p.error(str(error))
        print(json.dumps(result, indent=2, sort_keys=True)); return
    if a.action == "reconcile-discovery":
        try:
            result = reconcile_indexed(a.artifact_root, a.dictionary_root)
        except (OSError, ValueError, KeyError) as error:
            p.error(str(error))
        print(json.dumps(result, indent=2, sort_keys=True)); return
    if a.action in {"resolve-installation", "resolve-installations"}:
        try:
            environment = json.loads(a.environment)
            preferences = json.loads(a.preferences)
            dataset = Dataset(a.root)
            results = [resolve_installation(dataset, name, environment, preferences) for name in a.names]
        except (ValueError, TypeError) as error:
            p.error(str(error))
        result = results[0] if a.action == "resolve-installation" else {
            "results": results, "snapshot": dataset.metadata["snapshot"],
            "contract_version": "installation-v1", "warnings": coverage_warnings(dataset.metadata),
        }
        print(json.dumps(result, indent=2, sort_keys=True)); return
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


if __name__ == "__main__":
    main()
