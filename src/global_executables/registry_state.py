"""Sharded on-disk layout for the shared registry crawl state.

The logical state is one JSON document, ``{"version": 1, "sources": {...}}``, shared by
the Python crawlers and the Go transactional crawler.  Storing it as one file grew
past 90 MB on the ``artifact-data`` branch, close to GitHub's 100 MB hard limit, and
every run rewrote all of it.  This module stores the same document as a directory:

``manifest.json``
    Layout version, the non-``sources`` top-level keys, a per-source summary, and the
    SHA-256 and size of every data file.  It is the commit point: a file that the
    manifest does not list is not part of the state.
``<source>/source.json``
    The source's checkpoint with its large maps removed.
``<source>/<field>/<prefix>.jsonl``
    A large map (``unavailable``, ``failures`` ...) as sorted ``["key", value]`` lines,
    split by the leading hex digits of ``sha256(key)``.  A run that changes a few keys
    rewrites only the shards that hold them, and no file approaches the size limit.

Encoding is canonical and identical in ``internal/gocrawl/statestore.go``: the same
document always produces the same bytes, whichever language wrote it, so a Python
publisher re-saving a Go checkpoint does not churn the Git history.

Writes are crash-safe and readers never assemble a half-written state.  Changed files
are first written under ``.staging/files``; writing ``.staging/manifest.json``
commits the update; the staged files are then renamed into place, the manifest last,
and files the new manifest no longer lists are removed.  A writer interrupted before
the commit leaves the previous state intact; one interrupted after it is rolled
forward by the next reader or writer.  Readers verify every file against the manifest
and retry when a concurrent writer replaces files underneath them.

A path ending in ``.json`` names the legacy single file; the directory is the same
path without the suffix.  Reading falls back to the legacy file while no directory
layout exists, and the first save writes the directory and removes the legacy file,
which is how the published branch migrates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORMAT = "global-executables-registry-state"
LAYOUT_VERSION = 1
MANIFEST = "manifest.json"
STAGING = ".staging"
STAGED_FILES = "files"
SOURCE_FILE = "source.json"
INLINE_LIMIT = 1024
SHARD_TARGET = 8192
MAX_PREFIX_LENGTH = 3
SUMMARY_FIELDS = (
    "catalog_complete",
    "catalog_digest",
    "catalog_size",
    "catalog_snapshot",
    "cursor",
    "refresh_cursor",
    "snapshot_generation",
)
READ_ATTEMPTS = 8
DEFAULT_PATH = Path("data/production/registry-state")

_SOURCE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_FIELD_NAME = re.compile(r"[a-z0-9][a-z0-9_]*\Z")
_COMPACT = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
_PRETTY = json.JSONEncoder(ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


class RegistryStateError(ValueError):
    """The stored state is invalid or could not be read consistently."""


class _TornRead(Exception):
    """A file changed while it was being read; the caller retries."""


@dataclass(frozen=True)
class StatePaths:
    root: Path
    legacy: Path


@dataclass(frozen=True)
class SaveResult:
    files: int
    written: int
    removed: int
    legacy_removed: bool


def state_paths(path: str | os.PathLike[str]) -> StatePaths:
    """Map a ``--state`` argument to the layout directory and the legacy file."""
    path = Path(path)
    if path.suffix == ".json":
        return StatePaths(path.with_suffix(""), path)
    return StatePaths(path, path.with_name(path.name + ".json"))


def _escape(text: str) -> str:
    # Go's encoder always escapes these two separators; match it byte for byte.
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def encode_compact(value: Any) -> str:
    return _escape(_COMPACT.encode(value))


def encode_pretty(value: Any) -> bytes:
    return (_escape(_PRETTY.encode(value)) + "\n").encode("utf-8")


def prefix_length(entries: int) -> int:
    """Hex digits of ``sha256(key)`` that pick a shard: 1, 16, 256 or 4096 files."""
    length = 0
    while length < MAX_PREFIX_LENGTH and entries > SHARD_TARGET * 16**length:
        length += 1
    return length


def shard_file_name(prefix: str) -> str:
    """File name of the shard holding keys whose SHA-256 starts with ``prefix``.

    The prefix is repeated reversed (``a3`` -> ``a3-3a.jsonl``).  Git pairs a changed
    blob with its previous version, to send a small delta on push, through a hash of
    the path's last sixteen characters; for plain ``a3.jsonl`` names 112 of 256 such
    hashes collide, so a run touching every shard pushed whole files instead of deltas
    (7.5 MB rather than 43 KB for a simulated 600-module Go pass).
    """
    return f"{prefix}-{prefix[::-1]}.jsonl" if prefix else "all.jsonl"


def shard_name(key: str, length: int) -> str:
    return shard_file_name(hashlib.sha256(key.encode("utf-8")).hexdigest()[:length])


_SCALARS = (str, int, float, bool, type(None))
# Encoding a million-entry map costs seconds, and a Python crawler saves its checkpoint
# every few minutes while carrying the Go source's map unchanged.  Maps whose values are
# immutable scalars are remembered with their encoded shards and reused while equal.
_SHARD_CACHE: dict[tuple[str, str], tuple[dict[str, Any], dict[str, bytes]]] = {}


def _encode_shards(source: str, field: str, value: dict[str, Any], length: int) -> dict[str, bytes]:
    cached = _SHARD_CACHE.get((source, field))
    if cached is not None and cached[0] == value:
        return cached[1]
    width = 4 * length
    names = [shard_file_name(f"{index:0{length}x}") for index in range(16**length)] if length else ["all.jsonl"]
    shards: dict[str, list[str]] = {}
    sha256 = hashlib.sha256
    encode = _COMPACT.encode
    for key in sorted(value):
        if length:
            index = int.from_bytes(sha256(key.encode("utf-8")).digest()[:2], "big") >> (16 - width)
        else:
            index = 0
        shards.setdefault(names[index], []).append(encode([key, value[key]]))
    files = {name: (_escape("\n".join(lines)) + "\n").encode("utf-8") for name, lines in shards.items()}
    if all(type(item) in _SCALARS for item in value.values()):
        _SHARD_CACHE[(source, field)] = (dict(value), files)
    else:
        _SHARD_CACHE.pop((source, field), None)
    return files


def _sharded(field: str, value: Any) -> bool:
    return isinstance(value, dict) and len(value) >= INLINE_LIMIT and bool(_FIELD_NAME.match(field))


def encode_layout(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return the manifest and the data files (relative path to bytes) for a document."""
    if not isinstance(document, dict):
        raise RegistryStateError("registry state must be a JSON object")
    sources = document.get("sources", {})
    if not isinstance(sources, dict):
        raise RegistryStateError("registry state 'sources' must be a JSON object")
    files: dict[str, bytes] = {}
    summaries: dict[str, Any] = {}
    for source in sorted(sources):
        if not _SOURCE_NAME.match(source):
            raise RegistryStateError(f"registry source name cannot be stored as a directory: {source!r}")
        entry = sources[source]
        fields: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        inline = entry
        if isinstance(entry, dict):
            inline = {}
            for field, value in entry.items():
                if not _sharded(field, value):
                    inline[field] = value
                    continue
                length = prefix_length(len(value))
                fields[field] = {"entries": len(value), "prefix_length": length}
                for name, data in _encode_shards(source, field, value, length).items():
                    files[f"{source}/{field}/{name}"] = data
            summary = {
                name: entry[name]
                for name in SUMMARY_FIELDS
                if name in entry and not isinstance(entry[name], (dict, list))
            }
        files[f"{source}/{SOURCE_FILE}"] = encode_pretty(inline)
        summaries[source] = {"fields": fields, "summary": summary}
    manifest = {
        "document": {key: value for key, value in document.items() if key != "sources"},
        "files": {
            path: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(files.items())
        },
        "format": FORMAT,
        "layout_version": LAYOUT_VERSION,
        "sources": summaries,
    }
    return manifest, files


def _check_manifest(manifest: Any, origin: Path) -> dict[str, Any]:
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != FORMAT
        or not isinstance(manifest.get("files"), dict)
        or not isinstance(manifest.get("sources"), dict)
        or not isinstance(manifest.get("document"), dict)
    ):
        raise RegistryStateError(f"not a registry state manifest: {origin}")
    if manifest.get("layout_version") != LAYOUT_VERSION:
        raise RegistryStateError(
            f"unsupported registry state layout {manifest.get('layout_version')!r} in {origin}")
    for path in manifest["files"]:
        parts = path.split("/")
        if path.startswith("/") or any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
            raise RegistryStateError(f"unsafe path in registry state manifest: {path!r}")
    return manifest


def _read_verified(candidates: list[Path], expected: dict[str, Any]) -> bytes:
    for candidate in candidates:
        try:
            data = candidate.read_bytes()
        except FileNotFoundError:
            continue
        if len(data) == expected.get("bytes") and hashlib.sha256(data).hexdigest() == expected.get("sha256"):
            return data
    raise _TornRead(str(candidates[-1]))


def _parse_lines(data: bytes, origin: str) -> list[Any]:
    text = data.decode("utf-8")
    if not text.endswith("\n"):
        raise RegistryStateError(f"truncated registry state shard: {origin}")
    # Split on "\n" only: str.splitlines would also break on separators such as
    # U+0085 that JSON leaves unescaped inside strings.
    return json.loads("[" + ",".join(text[:-1].split("\n")) + "]")


def _load_layout(root: Path) -> dict[str, Any] | None:
    staged_manifest = root / STAGING / MANIFEST
    candidates_for: Any
    try:
        manifest_bytes = staged_manifest.read_bytes()
        origin = staged_manifest

        def candidates_for(path: str) -> list[Path]:
            return [root / STAGING / STAGED_FILES / path, root / path]
    except FileNotFoundError:
        try:
            manifest_bytes = (root / MANIFEST).read_bytes()
        except FileNotFoundError:
            if staged_manifest.is_file():
                raise _TornRead(str(staged_manifest)) from None
            return None
        origin = root / MANIFEST

        def candidates_for(path: str) -> list[Path]:
            return [root / path]
    try:
        manifest = _check_manifest(json.loads(manifest_bytes), origin)
    except json.JSONDecodeError as error:
        raise _TornRead(str(origin)) from error
    files = manifest["files"]
    sources: dict[str, Any] = {}
    for source, description in manifest["sources"].items():
        source_path = f"{source}/{SOURCE_FILE}"
        if source_path not in files:
            raise RegistryStateError(f"registry state manifest lists {source!r} without {source_path}")
        entry = json.loads(_read_verified(candidates_for(source_path), files[source_path]))
        for field in (description.get("fields") or {}):
            prefix = f"{source}/{field}/"
            merged: dict[str, Any] = {}
            for path in sorted(name for name in files if name.startswith(prefix)):
                for key, value in _parse_lines(_read_verified(candidates_for(path), files[path]), path):
                    merged[key] = value
            if not isinstance(entry, dict):
                raise RegistryStateError(f"registry source {source!r} has shards but is not an object")
            entry[field] = merged
        sources[source] = entry
    document = dict(manifest["document"])
    document["sources"] = sources
    return document


def load_state(path: str | os.PathLike[str], default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read the state at ``path`` (directory layout first, then the legacy file)."""
    paths = state_paths(path)
    for attempt in range(READ_ATTEMPTS):
        try:
            document = _load_layout(paths.root)
        except _TornRead:
            time.sleep(0.05 * (attempt + 1))
            continue
        if document is not None:
            return document
        if paths.legacy.is_file():
            value = json.loads(paths.legacy.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise RegistryStateError(f"registry state must be a JSON object: {paths.legacy}")
            return value
        return json.loads(json.dumps(default)) if default is not None else {"version": 1, "sources": {}}
    raise RegistryStateError(f"registry state at {paths.root} kept changing while it was read")


def state_exists(path: str | os.PathLike[str]) -> bool:
    paths = state_paths(path)
    return (
        (paths.root / MANIFEST).is_file()
        or (paths.root / STAGING / MANIFEST).is_file()
        or paths.legacy.is_file()
    )


def read_manifest(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    paths = state_paths(path)
    target = paths.root / MANIFEST
    if not target.is_file():
        return None
    return _check_manifest(json.loads(target.read_bytes()), target)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _collect_garbage(root: Path, listed: set[str]) -> int:
    removed = 0
    for current, directories, names in os.walk(root, topdown=False):
        here = Path(current)
        relative = here.relative_to(root)
        if relative.parts and relative.parts[0] == STAGING:
            continue
        for name in names:
            path = (relative / name).as_posix()
            if path == MANIFEST or path in listed:
                continue
            (here / name).unlink()
            removed += 1
        if relative.parts and not any(here.iterdir()):
            here.rmdir()
    return removed


def _apply_staging(root: Path) -> int:
    """Roll a committed staging area forward; idempotent after a crash."""
    staging = root / STAGING
    manifest = _check_manifest(json.loads((staging / MANIFEST).read_bytes()), staging / MANIFEST)
    staged = staging / STAGED_FILES
    touched: set[Path] = set()
    if staged.is_dir():
        for current, _directories, names in os.walk(staged):
            for name in names:
                source = Path(current) / name
                if name.startswith("."):
                    source.unlink()
                    continue
                target = root / source.relative_to(staged)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                touched.add(target.parent)
    for directory in touched:
        _fsync_directory(directory)
    os.replace(staging / MANIFEST, root / MANIFEST)
    _fsync_directory(root)
    removed = _collect_garbage(root, set(manifest["files"]))
    shutil.rmtree(staging, ignore_errors=True)
    return removed


def recover(path: str | os.PathLike[str]) -> None:
    """Finish or discard an interrupted save at ``path``."""
    root = state_paths(path).root
    staging = root / STAGING
    if (staging / MANIFEST).is_file():
        _apply_staging(root)
    elif staging.exists():
        shutil.rmtree(staging)


def save_state(path: str | os.PathLike[str], document: dict[str, Any]) -> SaveResult:
    """Write ``document`` to the directory layout, rewriting only changed files."""
    paths = state_paths(path)
    root = paths.root
    manifest, files = encode_layout(document)
    root.mkdir(parents=True, exist_ok=True)
    recover(root)
    previous: dict[str, Any] = {}
    manifest_path = root / MANIFEST
    if manifest_path.is_file():
        try:
            previous = _check_manifest(json.loads(manifest_path.read_bytes()), manifest_path)["files"]
        except (RegistryStateError, json.JSONDecodeError):
            previous = {}
    changed: dict[str, bytes] = {}
    for relative, data in files.items():
        expected = manifest["files"][relative]
        target = root / relative
        if previous.get(relative) == expected and target.is_file() and target.stat().st_size == expected["bytes"]:
            continue
        changed[relative] = data
    manifest_bytes = encode_pretty(manifest)
    unchanged = (
        not changed
        and manifest_path.is_file()
        and manifest_path.read_bytes() == manifest_bytes
    )
    removed = 0
    if unchanged:
        removed = _collect_garbage(root, set(files))
    else:
        staging = root / STAGING
        staged = staging / STAGED_FILES
        for relative, data in changed.items():
            _write_file(staged / relative, data)
        directories = {(staged / relative).parent for relative in changed}
        for directory in directories:
            _fsync_directory(directory)
        _write_file(staging / MANIFEST, manifest_bytes)
        _fsync_directory(staging)
        removed = _apply_staging(root)
    legacy_removed = False
    if paths.legacy.is_file():
        paths.legacy.unlink()
        _fsync_directory(paths.legacy.parent)
        legacy_removed = True
    return SaveResult(files=len(files), written=len(changed), removed=removed, legacy_removed=legacy_removed)
