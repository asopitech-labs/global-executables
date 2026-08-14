#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from global_executables.live_sources import run_all

parser = argparse.ArgumentParser(description="Download and inspect representative content from every upstream")
parser.add_argument("--output", type=Path)
args = parser.parse_args()
report = run_all()
serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
if args.output:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialized)
print(serialized, end="")
