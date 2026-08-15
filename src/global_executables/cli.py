from __future__ import annotations
import argparse, json
from pathlib import Path
from .collectors import crates_manifest, homebrew_metadata, npm_metadata, package_files
from .model import write_jsonl
from .pipeline import rebuild

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="action",required=True)
    build=sub.add_parser("build"); build.add_argument("inputs",nargs="+",type=Path); build.add_argument("--root",type=Path,default=Path.cwd()); build.add_argument("--snapshot"); build.add_argument("--coverage-kind",choices=["fixture","smoke","partial","exhaustive"],default="fixture")
    collect=sub.add_parser("collect"); collect.add_argument("ecosystem",choices=["debian","ubuntu","arch","homebrew","npm","crates"]); collect.add_argument("input",type=Path); collect.add_argument("output",type=Path)
    a=p.parse_args()
    if a.action=="build": rebuild(a.root,a.inputs,a.snapshot,a.coverage_kind); return
    if a.ecosystem in {"debian","ubuntu","arch"}: rows=package_files(a.input.read_text(),a.ecosystem,str(a.input))
    elif a.ecosystem=="npm": rows=npm_metadata(json.loads(a.input.read_text()),str(a.input))
    elif a.ecosystem=="homebrew": rows=homebrew_metadata(json.loads(a.input.read_text()),str(a.input))
    else:
        value=json.loads(a.input.read_text()); rows=crates_manifest(value["manifest"],value["package"],value.get("version"),value.get("repository"),str(a.input))
    write_jsonl(sorted(rows,key=lambda r:(r["command"],r["package"])),a.output)
