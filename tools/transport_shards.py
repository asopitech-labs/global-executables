#!/usr/bin/env python3
"""Pack growing compatibility files into bounded, verified Git transport shards."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


FORMAT = "global-executables-gzip-shards-v1"
DEFAULT_MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
COPY_BUFFER = 1024 * 1024


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_BUFFER):
            digest.update(chunk)
    return digest.hexdigest()


def write_part(directory: Path, index: int, data: bytes) -> dict[str, Any]:
    name = f"part-{index:05d}.jsonl.gz"
    path = directory / name
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as stream:
            stream.write(data)
        raw.flush()
        os.fsync(raw.fileno())
    return {
        "file": name,
        "compressed_bytes": path.stat().st_size,
        "compressed_sha256": digest_file(path),
        "uncompressed_bytes": len(data),
        "uncompressed_sha256": hashlib.sha256(data).hexdigest(),
    }


def replace_directory(temporary: Path, output: Path) -> None:
    backup = output.with_name(f".{output.name}.{os.getpid()}.old")
    shutil.rmtree(backup, ignore_errors=True)
    moved_old = False
    try:
        if output.exists():
            os.replace(output, backup)
            moved_old = True
        os.replace(temporary, output)
    except Exception:
        if moved_old and not output.exists():
            os.replace(backup, output)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def pack(source: Path, output: Path, max_uncompressed_bytes: int) -> dict[str, Any]:
    if max_uncompressed_bytes <= 0:
        raise ValueError("max uncompressed bytes must be positive")
    if not source.is_file():
        raise ValueError(f"input is not a file: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir()
    parts: list[dict[str, Any]] = []
    whole_digest = hashlib.sha256()
    total = 0
    buffer = bytearray()
    try:
        with source.open("rb") as stream:
            for line in stream:
                if len(line) > max_uncompressed_bytes:
                    raise ValueError(
                        f"one input line is {len(line)} bytes, above shard limit {max_uncompressed_bytes}"
                    )
                if buffer and len(buffer) + len(line) > max_uncompressed_bytes:
                    parts.append(write_part(temporary, len(parts), bytes(buffer)))
                    buffer.clear()
                buffer.extend(line)
                whole_digest.update(line)
                total += len(line)
        if buffer:
            parts.append(write_part(temporary, len(parts), bytes(buffer)))
        manifest = {
            "format": FORMAT,
            "max_uncompressed_bytes": max_uncompressed_bytes,
            "parts": parts,
            "uncompressed_bytes": total,
            "uncompressed_sha256": whole_digest.hexdigest(),
        }
        manifest_path = temporary / "manifest.json"
        with manifest_path.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_directory(temporary, output)
        return manifest
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def checked_manifest(input_dir: Path) -> dict[str, Any]:
    manifest_path = input_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ValueError("unsupported transport manifest format")
    parts = manifest.get("parts")
    if not isinstance(parts, list):
        raise ValueError("manifest parts must be a list")
    expected = [f"part-{index:05d}.jsonl.gz" for index in range(len(parts))]
    declared = [part.get("file") if isinstance(part, dict) else None for part in parts]
    actual = sorted(path.name for path in input_dir.glob("part-*.jsonl.gz"))
    if declared != expected or actual != expected:
        raise ValueError("transport part set does not match manifest")
    return manifest


def unpack(input_dir: Path, output: Path) -> None:
    manifest = checked_manifest(input_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    whole_digest = hashlib.sha256()
    total = 0
    try:
        with temporary.open("wb") as destination:
            for part in manifest["parts"]:
                path = input_dir / part["file"]
                if path.stat().st_size != part.get("compressed_bytes"):
                    raise ValueError(f"compressed size mismatch: {path.name}")
                if digest_file(path) != part.get("compressed_sha256"):
                    raise ValueError(f"compressed digest mismatch: {path.name}")
                part_digest = hashlib.sha256()
                part_total = 0
                with gzip.open(path, "rb") as stream:
                    while chunk := stream.read(COPY_BUFFER):
                        destination.write(chunk)
                        part_digest.update(chunk)
                        whole_digest.update(chunk)
                        part_total += len(chunk)
                        total += len(chunk)
                if part_total != part.get("uncompressed_bytes"):
                    raise ValueError(f"uncompressed size mismatch: {path.name}")
                if part_digest.hexdigest() != part.get("uncompressed_sha256"):
                    raise ValueError(f"uncompressed digest mismatch: {path.name}")
            destination.flush()
            os.fsync(destination.fileno())
        if total != manifest.get("uncompressed_bytes"):
            raise ValueError("transport total size mismatch")
        if whole_digest.hexdigest() != manifest.get("uncompressed_sha256"):
            raise ValueError("transport total digest mismatch")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    pack_parser = subparsers.add_parser("pack")
    pack_parser.add_argument("--input", type=Path, required=True)
    pack_parser.add_argument("--output-dir", type=Path, required=True)
    pack_parser.add_argument(
        "--max-uncompressed-bytes", type=int, default=DEFAULT_MAX_UNCOMPRESSED_BYTES
    )
    unpack_parser = subparsers.add_parser("unpack")
    unpack_parser.add_argument("--input-dir", type=Path, required=True)
    unpack_parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "pack":
            manifest = pack(args.input, args.output_dir, args.max_uncompressed_bytes)
            print(
                f"packed {manifest['uncompressed_bytes']} bytes into {len(manifest['parts'])} shards"
            )
        else:
            unpack(args.input_dir, args.output)
            print(f"restored {args.output}")
        return 0
    except Exception as error:
        print(f"transport shards: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
