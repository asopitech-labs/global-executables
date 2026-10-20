"""Validation for a materialized published dictionary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .model import filename, shard


def _json(path: Path):
    return json.loads(path.read_text())


def validate_dictionary(
    root: Path,
    schema_root: Path,
    *,
    validate_schema: bool = True,
) -> dict[str, int]:
    data = root / "data"
    metadata_path = data / "metadata.json"
    history_path = data / "history.json"
    executables = data / "executables"
    indexes = data / "indexes"
    for required in (metadata_path, history_path, executables, indexes):
        if not required.exists():
            raise ValueError(f"published dictionary is missing {required.relative_to(root)}")

    executable_validator = None
    metadata_validator = None
    if validate_schema:
        provider_schema = _json(schema_root / "schema/provider.schema.json")
        executable_schema = _json(schema_root / "schema/executable.schema.json")
        metadata_schema = _json(schema_root / "schema/metadata.schema.json")
        registry = Registry().with_resource(
            provider_schema["$id"], Resource.from_contents(provider_schema)
        )
        executable_validator = Draft202012Validator(
            executable_schema, registry=registry, format_checker=FormatChecker()
        )
        metadata_validator = Draft202012Validator(
            metadata_schema, format_checker=FormatChecker()
        )

    metadata = _json(metadata_path)
    if metadata_validator is not None:
        metadata_validator.validate(metadata)
    history = _json(history_path)
    if not isinstance(history, dict):
        raise ValueError("data/history.json must be a JSON object")

    commands: set[str] = set()
    executable_paths = sorted(executables.glob("**/*.json"))
    for path in executable_paths:
        record = _json(path)
        if executable_validator is not None:
            executable_validator.validate(record)
        command = record["command"]
        expected = executables / shard(command) / filename(command)
        if path != expected:
            raise ValueError(
                f"canonical path mismatch for {command!r}: {path.relative_to(root)}"
            )
        if command in commands:
            raise ValueError(f"duplicate canonical command: {command}")
        commands.add(command)

    if not commands:
        raise ValueError("published dictionary must contain at least one executable")
    if metadata["unique_executables"] != len(commands):
        raise ValueError(
            "metadata unique_executables does not match canonical file count: "
            f"{metadata['unique_executables']} != {len(commands)}"
        )

    manifest = metadata["index_manifest"]
    index_paths = sorted(indexes.glob("**/*.json"))
    relative_indexes = {path.relative_to(data).as_posix() for path in index_paths}
    if set(manifest) != relative_indexes:
        missing = sorted(set(manifest) - relative_indexes)
        extra = sorted(relative_indexes - set(manifest))
        raise ValueError(f"index manifest mismatch; missing={missing}, extra={extra}")
    for path in index_paths:
        relative = path.relative_to(data).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != manifest[relative]:
            raise ValueError(f"index digest mismatch: {relative}")

    return {"executables": len(commands), "indexes": len(index_paths)}
