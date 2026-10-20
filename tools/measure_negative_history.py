#!/usr/bin/env python3
"""Measure what a git-tracked "negative history" would cost, to justify keeping it in the cache.

A negative history would store ``name<TAB>version<TAB>revision`` for every inspected package,
including the ones with no command, in 256 hash shards.  This builds such shards from a list
of package names (one per line on stdin), commits them to a throw-away repository, then
changes ``--changes`` random versions (what one feed batch does) and reports the packed size
of the first commit and the thin pack the second one would push.  Nothing in the real
repository or data branches is touched.

    python tools/measure_negative_history.py --changes 2300 < names.txt
"""
from __future__ import annotations

import argparse
import hashlib
import random
import subprocess
import sys
import tempfile
from pathlib import Path


def shard_of(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()[:2]


def write(root: Path, versions: dict[str, str]) -> None:
    shards: dict[str, list[str]] = {}
    for name in sorted(versions):
        shards.setdefault(shard_of(name), []).append(f"{name}\t{versions[name]}\t1\n")
    (root / "negative").mkdir(exist_ok=True)
    for shard, lines in shards.items():
        (root / "negative" / f"{shard}.tsv").write_text("".join(lines))


def git(root: Path, *args: str, stdin: str | None = None) -> bytes:
    return subprocess.run(["git", "-c", "user.email=m@x", "-c", "user.name=m", *args], cwd=root, input=stdin and stdin.encode(),
                          check=True, capture_output=True).stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--changes", type=int, default=2300, help="versions that change between the two commits")
    parser.add_argument("--scale-to", type=int, default=0, help="extrapolate the sizes to this many packages")
    args = parser.parse_args()
    random.seed(1)
    names = [line.strip() for line in sys.stdin if line.strip()]
    versions = {name: "1.0.0" for name in names}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        git(root, "init", "-q", ".")
        write(root, versions)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "one")
        git(root, "gc", "-q", "--aggressive")
        packed = sum(path.stat().st_size for path in (root / ".git" / "objects").rglob("*") if path.is_file())
        for name in random.sample(names, min(args.changes, len(names))):
            versions[name] = f"9.{len(name)}.1"
        write(root, versions)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "two")
        thin = len(git(root, "pack-objects", "--thin", "--revs", "--stdout", stdin="HEAD\n^HEAD~1\n"))
        files = len(git(root, "diff", "--name-only", "HEAD~1").split())
    factor = args.scale_to / len(names) if args.scale_to else 1
    print(f"packages: {len(names)} (scaled x{factor:.1f})")
    print(f"initial commit packed: {packed * factor / 1e6:.1f} MB")
    print(f"{args.changes} version changes: {files} of 256 shards touched, thin pack {thin * factor / 1e6:.2f} MB per run")


if __name__ == "__main__":
    main()
