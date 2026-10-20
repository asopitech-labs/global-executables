from __future__ import annotations
import argparse, json
from pathlib import Path
from .collectors import crates_manifest, homebrew_metadata, npm_metadata, package_files
from .model import write_jsonl
from .pipeline import rebuild
from .assessment import assess
from .search import Dataset

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="action",required=True)
    build=sub.add_parser("build"); build.add_argument("inputs",nargs="+",type=Path); build.add_argument("--root",type=Path,default=Path.cwd()); build.add_argument("--snapshot"); build.add_argument("--coverage-kind",choices=["fixture","smoke","partial","exhaustive"],default="fixture")
    collect=sub.add_parser("collect"); collect.add_argument("ecosystem",choices=["debian","ubuntu","arch","homebrew","npm","crates"]); collect.add_argument("input",type=Path); collect.add_argument("output",type=Path)
    query=sub.add_parser("assess"); query.add_argument("name"); query.add_argument("--root",type=Path,default=Path.cwd()); query.add_argument("--scope",action="append",default=[],help="dimension=value; repeatable")
    a=p.parse_args()
    if a.action=="build": rebuild(a.root,a.inputs,a.snapshot,a.coverage_kind); return
    if a.action=="assess":
        scope=dict(item.split("=",1) for item in a.scope)
        print(json.dumps(assess(Dataset(a.root),a.name,scope),indent=2,sort_keys=True)); return
    if a.ecosystem in {"debian","ubuntu","arch"}: rows=package_files(a.input.read_text(),a.ecosystem,str(a.input))
    elif a.ecosystem=="npm": rows=npm_metadata(json.loads(a.input.read_text()),str(a.input))
    elif a.ecosystem=="homebrew": rows=homebrew_metadata(json.loads(a.input.read_text()),str(a.input))
    else:
        value=json.loads(a.input.read_text()); rows=crates_manifest(value["manifest"],value["package"],value.get("version"),value.get("repository"),str(a.input))
    write_jsonl(sorted(rows,key=lambda r:(r["command"],r["package"])),a.output)
