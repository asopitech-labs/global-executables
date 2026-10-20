#!/usr/bin/env python3
"""Inspect, copy and restore the sharded registry crawl state from shell steps.

Workflows and tools/crawl_parallel.sh used to ``git show`` or ``cp`` one
``registry-state.json``.  The state is now a directory (see
src/global_executables/registry_state.py); these subcommands keep shell callers away
from its internals and accept either layout wherever a ``--state`` path is taken.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from global_executables.registry_state import (  # noqa: E402
    DEFAULT_PATH,
    MANIFEST,
    load_state,
    read_manifest,
    save_state,
    state_exists,
    state_paths,
)

EMPTY: dict[str, Any] = {"version": 1, "sources": {}}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True)


def _git_has(repo: Path, ref: str, path: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{ref}:{path}", check=False).returncode == 0


def restore(repo: Path, ref: str, published: Path, target: Path) -> str:
    """Copy the state published at ``ref`` into ``target`` (either layout)."""
    source = state_paths(published)
    destination = state_paths(target)
    root = source.root.as_posix()
    if _git_has(repo, ref, f"{root}/{MANIFEST}"):
        archive = _git(repo, "archive", "--format=tar", ref, root).stdout
        destination.root.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.root.name}.", dir=destination.root.parent))
        try:
            prefix = PurePosixPath(root)
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                for member in bundle.getmembers():
                    if not member.isfile():
                        continue
                    relative = PurePosixPath(member.name).relative_to(prefix)
                    if any(part in {"", ".", ".."} for part in relative.parts):
                        raise SystemExit(f"unsafe path in {ref}: {member.name}")
                    output = temporary.joinpath(*relative.parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    extracted = bundle.extractfile(member)
                    assert extracted is not None
                    output.write_bytes(extracted.read())
            load_state(temporary)  # verify every file against the manifest before use
            shutil.rmtree(destination.root, ignore_errors=True)
            destination.legacy.unlink(missing_ok=True)
            os.replace(temporary, destination.root)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return "sharded"
    legacy = source.legacy.as_posix()
    if _git_has(repo, ref, legacy):
        body = _git(repo, "show", f"{ref}:{legacy}").stdout
        shutil.rmtree(destination.root, ignore_errors=True)
        destination.legacy.parent.mkdir(parents=True, exist_ok=True)
        destination.legacy.write_bytes(body)
        return "legacy"
    return "missing"


def _scalars(entry: Any) -> Any:
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if not isinstance(value, (dict, list))}


def summary(path: Path) -> dict[str, Any]:
    paths = state_paths(path)
    document = load_state(path)
    manifest = read_manifest(path)
    sources: dict[str, Any] = {}
    for name, entry in sorted(document.get("sources", {}).items()):
        counts = {key: len(value) for key, value in entry.items() if isinstance(value, (dict, list))} \
            if isinstance(entry, dict) else {}
        sources[name] = {"scalars": _scalars(entry), "sizes": counts}
    layout = "sharded" if manifest is not None else ("legacy" if paths.legacy.is_file() else "missing")
    result: dict[str, Any] = {"layout": layout, "sources": sources}
    if manifest is not None:
        files = manifest["files"]
        result["files"] = len(files) + 1
        result["bytes"] = sum(entry["bytes"] for entry in files.values()) + (paths.root / MANIFEST).stat().st_size
        result["largest_file_bytes"] = max((entry["bytes"] for entry in files.values()), default=0)
    elif paths.legacy.is_file():
        result["files"] = 1
        result["bytes"] = result["largest_file_bytes"] = paths.legacy.stat().st_size
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("restore", help="restore the state published at a Git ref")
    command.add_argument("--ref", required=True)
    command.add_argument("--state", type=Path, default=DEFAULT_PATH, help="local target (directory or legacy .json)")
    command.add_argument("--published-path", type=Path, default=DEFAULT_PATH, help="state path inside the ref")
    command.add_argument("--repo", type=Path, default=Path("."))
    command.add_argument("--require", action="store_true", help="fail when the ref holds no state")

    command = commands.add_parser("exists", help="exit 0 when a state exists in either layout")
    command.add_argument("--state", type=Path, default=DEFAULT_PATH)

    command = commands.add_parser("copy", help="copy a state (optionally only some sources)")
    command.add_argument("--from", dest="source", type=Path, required=True)
    command.add_argument("--to", dest="target", type=Path, required=True)
    command.add_argument("--only-source", action="append", default=[], help="keep only these registry sources")

    command = commands.add_parser("merge-sources", help="replace sources in --into with those of each --from")
    command.add_argument("--into", type=Path, required=True)
    command.add_argument("--from", dest="sources", type=Path, action="append", default=[])

    command = commands.add_parser("get", help="print a source's checkpoint as JSON")
    command.add_argument("--state", type=Path, default=DEFAULT_PATH)
    command.add_argument("--source", required=True)
    command.add_argument("--scalars", action="store_true", help="omit maps and lists")

    command = commands.add_parser("summary", help="print layout, sizes and per-source cursors as JSON")
    command.add_argument("--state", type=Path, default=DEFAULT_PATH)

    command = commands.add_parser("migrate", help="rewrite a state in the sharded layout, removing the legacy file")
    command.add_argument("--state", type=Path, default=DEFAULT_PATH)
    command.add_argument("--to", type=Path, help="write the sharded layout here instead of in place")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "restore":
        layout = restore(args.repo, args.ref, args.published_path, args.state)
        print(f"registry state at {args.ref}: {layout}")
        if layout == "missing" and args.require:
            return 1
    elif args.command == "exists":
        return 0 if state_exists(args.state) else 1
    elif args.command == "copy":
        if not state_exists(args.source):
            raise SystemExit(f"no registry state at {args.source}")
        document = load_state(args.source)
        if args.only_source:
            sources = document.get("sources", {})
            document["sources"] = {name: sources[name] for name in args.only_source if name in sources}
        save_state(args.target, document)
    elif args.command == "merge-sources":
        combined = load_state(args.into, EMPTY)
        for path in args.sources:
            if state_exists(path):
                combined.setdefault("sources", {}).update(load_state(path).get("sources", {}))
        save_state(args.into, combined)
        print(f"merged {len(args.sources)} source states into {state_paths(args.into).root}")
    elif args.command == "get":
        entry = load_state(args.state).get("sources", {}).get(args.source)
        print(json.dumps(_scalars(entry) if args.scalars else entry, ensure_ascii=False, sort_keys=True))
    elif args.command == "summary":
        print(json.dumps(summary(args.state), ensure_ascii=False, indent=2, sort_keys=True))
    elif args.command == "migrate":
        if not state_exists(args.state):
            raise SystemExit(f"no registry state at {args.state}")
        result = save_state(args.to or args.state, load_state(args.state))
        print(json.dumps(summary(args.to or args.state) | {"written": result.written}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
