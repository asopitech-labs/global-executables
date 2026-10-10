#!/usr/bin/env python3
"""Local booster helper: leases on artifact-data, the Actions exclusion and delta rows.

    tools/booster.py show      --source pypi
    tools/booster.py exclusions --source pypi [--ref origin/artifact-data]
    tools/booster.py acquire   --source pypi --id my-laptop --ranges 0-255 [--ttl-hours 12]
    tools/booster.py renew     --source pypi --id my-laptop     (same as acquire, keeps the ranges)
    tools/booster.py release   --source pypi --id my-laptop
    tools/booster.py rows-delta --published P --local L --base B --output O

``acquire``/``renew``/``release`` commit to ``artifact-data`` through a throw-away worktree
and your own git credentials; a non-fast-forward push re-reads the branch and applies the
operation again.  Only ``data/production/booster/<source>.json`` is written.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from global_executables import booster  # noqa: E402


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def read_remote_document(source: str, ref: str) -> dict:
    shown = git("show", f"{ref}:{booster.LEASE_PATH.format(source=source)}", check=False)
    if shown.returncode != 0:
        return booster.empty_document()
    try:
        document = json.loads(shown.stdout)
    except ValueError:
        return booster.empty_document()
    return document if isinstance(document, dict) and isinstance(document.get("leases"), dict) else booster.empty_document()


def publish(source: str, operation, remote: str, branch: str, attempts: int, message: str) -> dict:
    """Apply ``operation(document)`` to the freshest branch head and push; retry on a race."""
    path = booster.LEASE_PATH.format(source=source)
    for attempt in range(1, attempts + 1):
        git("fetch", "--quiet", "--depth=1", remote, branch)
        worktree = Path(tempfile.mkdtemp(prefix="ge-booster-"))
        shutil.rmtree(worktree)
        try:
            git("worktree", "add", "--quiet", "--detach", str(worktree), f"{remote}/{branch}")
            target = worktree / path
            document = operation(booster.read_document(target))
            before = target.read_text() if target.is_file() else None
            booster.write_document(target, document)
            if target.read_text() == before:
                return document
            git("add", "-f", path, cwd=worktree)
            git("commit", "--quiet", "-m", message, cwd=worktree)
            pushed = git("push", "--quiet", remote, f"HEAD:{branch}", cwd=worktree, check=False)
            if pushed.returncode == 0:
                return document
            if attempt == attempts:
                raise SystemExit(f"lease publish failed after {attempts} attempts: {pushed.stderr.strip()}")
        finally:
            git("worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(worktree, ignore_errors=True)
        time.sleep(min(30, attempt * 3))
    raise SystemExit("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["show", "exclusions", "acquire", "renew", "release", "rows-delta"])
    parser.add_argument("--source", default="pypi")
    parser.add_argument("--id", default=os.environ.get("BOOSTER_ID", ""))
    parser.add_argument("--ranges", default=os.environ.get("BOOSTER_RANGES", "0-255"))
    parser.add_argument("--ttl-hours", type=float, default=float(os.environ.get("BOOSTER_TTL_HOURS", booster.DEFAULT_TTL_HOURS)))
    parser.add_argument("--ref", default="origin/artifact-data")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="artifact-data")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--progress", default="", help="JSON object stored with the lease (informational)")
    parser.add_argument("--published", type=Path)
    parser.add_argument("--local", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.command == "rows-delta":
        if not (args.local and args.output):
            parser.error("rows-delta needs --local and --output")
        touched, written = booster.merge_rows_delta(args.published, args.local, args.base, args.output)
        print(f"{touched} packages changed, {written} rows applied")
        return 0
    if args.command == "show":
        print(json.dumps(read_remote_document(args.source, args.ref), indent=2, sort_keys=True))
        return 0
    if args.command == "exclusions":
        # Nothing but the ranges on stdout: callers put it on a command line.
        print(booster.actions_exclusion(read_remote_document(args.source, args.ref)))
        return 0
    if not args.id:
        parser.error("--id (or BOOSTER_ID) names this machine's lease")
    progress = json.loads(args.progress) if args.progress else None
    if args.command == "release":
        publish(args.source, lambda document: booster.release(document, args.id), args.remote, args.branch,
                args.attempts, f"Release {args.source} booster lease {args.id}")
        print(f"released {args.id}")
        return 0
    existing = read_remote_document(args.source, args.ref).get("leases", {}).get(args.id)
    ranges = args.ranges if args.command == "acquire" or not isinstance(existing, dict) else existing.get("ranges", args.ranges)
    try:
        document = publish(args.source, lambda doc: booster.acquire(doc, args.id, ranges, ttl_hours=args.ttl_hours, progress=progress),
                           args.remote, args.branch, args.attempts, f"Heartbeat for {args.source} booster lease {args.id}")
    except booster.LeaseConflict as error:
        print(f"lease refused: {error}", file=sys.stderr)
        return 4
    print(f"{args.id} holds {document['leases'][args.id]['ranges']} (ttl {args.ttl_hours} h)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
