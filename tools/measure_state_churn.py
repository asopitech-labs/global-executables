#!/usr/bin/env python3
"""Measure what per-package check bookkeeping costs the artifact-data branch.

Two layouts: the one #65 shipped (`checked` in the registry state, the default) and the
history/cache split (`--split`: checks live in a cache file outside git, the state is
untouched by an unchanged batch).

Builds a registry state with N packages, commits it to a scratch git repository, applies
one run's worth of check updates and reports how much a push would carry (a thin pack of
the second commit, which is what `git push` sends) and how many shard files change.
Nothing touches a real data branch.

    python3 tools/measure_state_churn.py --packages 870000 --visited 3000 --changed 8
"""
from __future__ import annotations

import argparse
import hashlib
import random
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from global_executables import refresh_policy  # noqa: E402
from global_executables.registry_state import save_state  # noqa: E402


def git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, input=stdin, check=True, capture_output=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", type=int, default=870_000)
    parser.add_argument("--visited", type=int, default=3000, help="packages whose check is rewritten by one run")
    parser.add_argument("--changed", type=int, default=8, help="of those, how many found a new version")
    parser.add_argument("--split", action="store_true", help="checks go to the schedule cache, not the state")
    parser.add_argument("--contiguous", action="store_true", help="visit a contiguous catalog range, not random keys")
    args = parser.parse_args()
    rng = random.Random(7)
    names = [hashlib.sha1(str(i).encode()).hexdigest()[:10] + f"-pkg{i}" for i in range(args.packages)]
    day = 20_000
    checked = {name: refresh_policy.pack(day - rng.randrange(0, 60), rng.randrange(0, 6), f"{rng.randrange(0, 9)}.{rng.randrange(0, 40)}.{rng.randrange(0, 9)}")
               for name in names}
    with tempfile.TemporaryDirectory() as scratch:
        repo = Path(scratch)
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "m@example.test"); git(repo, "config", "user.name", "m")
        entry = {"cursor": args.packages, "catalog_size": args.packages}
        if not args.split:
            entry["checked"] = checked
        document = {"version": 1, "sources": {"pypi": entry}}
        save_state(repo / "state", document)
        git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
        base_bytes = sum(path.stat().st_size for path in (repo / "state").rglob("*") if path.is_file())
        if args.contiguous:
            start = rng.randrange(0, args.packages - args.visited)
            visited = names[start:start + args.visited]
        else:
            visited = rng.sample(names, args.visited)
        for index, name in enumerate(visited):
            _, streak, version = refresh_policy.unpack(checked[name])
            if index < args.changed:
                checked[name] = refresh_policy.pack(day + 1, 0, version + ".1")
            else:
                checked[name] = refresh_policy.pack(day + 1, streak + 1, version)
        if args.split:
            # Unchanged packages only move their check in the cache (outside the repository);
            # a changed package would rewrite its own rows, not the state.
            refresh_policy.write_cache(Path(scratch).parent / "measure-cache.gz", checked, 0)
        save_state(repo / "state", document)
        git(repo, "add", "-A")
        changed_files = git(repo, "diff", "--cached", "--name-only").decode().split()
        if not changed_files:
            print(f"packages={args.packages} split={args.split} visited={args.visited}: nothing to commit")
            print("shard files changed: 0\npush payload (thin pack): 0 bytes")
            return 0
        git(repo, "commit", "-qm", "run")
        pack = git(repo, "pack-objects", "--thin", "--revs", "--stdout", stdin=b"HEAD\n^HEAD~1\n")
        shards = [name for name in changed_files if name.endswith(".jsonl")]
        print(f"split={args.split} packages={args.packages} state_bytes={base_bytes} visited={args.visited} changed={args.changed} "
              f"contiguous={args.contiguous}")
        print(f"shard files changed: {len(shards)} of {len(list((repo / 'state').rglob('*.jsonl')))}")
        print(f"push payload (thin pack): {len(pack)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
