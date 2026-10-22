#!/usr/bin/env python3
"""Operate the coordinator-free Git work ledger on artifact-data."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from global_executables import work_ledger  # noqa: E402
from global_executables.registry_state import load_state  # noqa: E402


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def read(path: Path, source: str) -> dict:
    if not path.is_file():
        return work_ledger.empty_document(source)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return work_ledger.empty_document(source)
    return value if isinstance(value, dict) else work_ledger.empty_document(source)


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def publish(source: str, operation, *, remote: str, branch: str, attempts: int,
            message: str) -> tuple[dict, object]:
    ledger_path = work_ledger.LEDGER_PATH.format(source=source)
    for attempt in range(1, attempts + 1):
        git("fetch", "--quiet", "--depth=1", remote, branch)
        directory = Path(tempfile.mkdtemp(prefix="ge-work-ledger-"))
        shutil.rmtree(directory)
        try:
            git("worktree", "add", "--quiet", "--detach", str(directory), f"{remote}/{branch}")
            target = directory / ledger_path
            before = target.read_text(encoding="utf-8") if target.is_file() else None
            document, result = operation(read(target, source))
            write(target, document)
            if target.read_text(encoding="utf-8") == before:
                return document, result
            git("add", "-f", ledger_path, cwd=directory)
            git("commit", "--quiet", "-m", message, cwd=directory)
            pushed = git("push", "--quiet", remote, f"HEAD:{branch}", cwd=directory, check=False)
            if pushed.returncode == 0:
                return document, result
            if attempt == attempts:
                raise SystemExit(f"ledger publish failed after {attempts} attempts: {pushed.stderr.strip()}")
        finally:
            git("worktree", "remove", "--force", str(directory), check=False)
            shutil.rmtree(directory, ignore_errors=True)
        time.sleep(min(30, attempt * 3))
    raise SystemExit("unreachable")


def catalog_inputs(args) -> tuple[dict, dict[str, int], dict[str, int]]:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    digest = manifest.get("catalog_digest")
    size = manifest.get("catalog_size")
    if type(size) is not int or size < 0:
        raise ValueError("catalog manifest size must be a non-negative integer")
    catalog = {"digest": digest, "size": size, "snapshot": manifest.get("catalog_snapshot"),
               "source": manifest.get("catalog_source")}
    state = load_state(args.state).get("sources", {}).get(args.source, {})
    cursor = state.get("cursor", 0)
    if type(cursor) is not int or cursor < 0:
        raise ValueError("legacy source cursor must be a non-negative integer")
    sizes, positions = work_ledger.catalogue_counts(args.catalog, min(cursor, size))
    return catalog, sizes, positions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["claim", "heartbeat", "checkpoint", "release", "status"])
    parser.add_argument("--source", default="npm-registry")
    parser.add_argument("--id", default="")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--ttl-hours", type=float, default=work_ledger.DEFAULT_TTL_HOURS)
    parser.add_argument("--unit")
    parser.add_argument("--position", type=int)
    parser.add_argument("--failures", type=int, default=0)
    parser.add_argument("--complete", action="store_true")
    parser.add_argument("--result-commit")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="artifact-data")
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args()
    if not args.id and args.command != "status":
        parser.error("--id is required")

    if args.command == "claim":
        if not (args.catalog and args.manifest and args.state):
            parser.error("claim needs --catalog, --manifest, and --state")
        catalog, sizes, positions = catalog_inputs(args)
        def operation(document):
            document = work_ledger.ensure_catalog(document, args.source, catalog, sizes, positions)
            return work_ledger.claim(document, args.id, count=args.count,
                                     ttl_hours=args.ttl_hours)
        try:
            document, result = publish(args.source, operation, remote=args.remote, branch=args.branch,
                                       attempts=args.attempts,
                                       message=f"Claim {args.source} work for {args.id}")
        except work_ledger.NoWork:
            print(json.dumps({"status": "complete", "units": []}, sort_keys=True))
            return 3
        positions = {unit: document["units"][unit]["position"] for unit in result["units"]}
        print(json.dumps({"status": "claimed", **result, "positions": positions}, sort_keys=True))
        return 0

    if args.command == "checkpoint":
        if args.unit is None or args.position is None or args.result_commit is None:
            parser.error("checkpoint needs --unit, --position, and --result-commit")
        def operation(document):
            updated = work_ledger.checkpoint(document, args.id, args.unit, args.position,
                                             failures=args.failures, complete=args.complete,
                                             result_commit=args.result_commit)
            return updated, work_ledger.status(updated)
        document, _ = publish(args.source, operation, remote=args.remote, branch=args.branch,
                              attempts=args.attempts,
                              message=f"Checkpoint {args.source} unit {args.unit} from {args.id}")
        print(json.dumps(work_ledger.status(document), sort_keys=True))
        return 0

    if args.command == "heartbeat":
        operation = lambda document: (work_ledger.heartbeat(document, args.id), None)
        document, _ = publish(args.source, operation, remote=args.remote, branch=args.branch,
                              attempts=args.attempts, message=f"Heartbeat {args.source} work for {args.id}")
        print(json.dumps(work_ledger.status(document), sort_keys=True))
        return 0

    if args.command == "release":
        operation = lambda document: (work_ledger.release(document, args.id), None)
        document, _ = publish(args.source, operation, remote=args.remote, branch=args.branch,
                              attempts=args.attempts, message=f"Release {args.source} work for {args.id}")
        print(json.dumps(work_ledger.status(document), sort_keys=True))
        return 0

    git("fetch", "--quiet", "--depth=1", args.remote, args.branch)
    shown = git("show", f"{args.remote}/{args.branch}:" + work_ledger.LEDGER_PATH.format(source=args.source),
                check=False)
    document = json.loads(shown.stdout) if shown.returncode == 0 else work_ledger.empty_document(args.source)
    print(json.dumps(work_ledger.status(document), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
