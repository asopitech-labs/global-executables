from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

COMMAND = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
PROVIDER_KEYS = (
    "ecosystem", "package", "version", "repository", "source", "confidence", "alias_of",
    "source_type", "package_system", "distribution_family", "distribution",
    "distribution_release", "language", "registry", "latest_release_at",
    "latest_version", "last_observed_at", "release_history", "usage_metrics",
    "claim_id", "provenance",
)
SCOPE_KEYS = (
    "source_type", "package_system", "distribution_family", "distribution",
    "distribution_release", "language", "registry", "ecosystem",
)


def valid_command(command: str) -> bool:
    return bool(COMMAND.fullmatch(command)) and command not in {".", ".."}


# `go install` names a binary after the last element of its import path, and the go
# command accepts only elements that pass golang.org/x/mod/module.CheckImportPath.
# This mirrors that check (importPath kind) so names the Go extractor skips since
# #120 also leave the dictionary instead of surviving from earlier walks.
_GO_IMPORT_ELEMENT = re.compile(r"^[A-Za-z0-9._~+-]+$")
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{suffix}" for suffix in "123456789\u00b9\u00b2\u00b3"]
    + [f"LPT{suffix}" for suffix in "123456789\u00b9\u00b2\u00b3"]
)


def go_installable_command(command: str) -> bool:
    """Whether a Go binary can carry this name (a valid import path element)."""
    if not _GO_IMPORT_ELEMENT.fullmatch(command):
        return False
    if command.count(".") == len(command) or command.endswith("."):
        return False
    short = command.split(".", 1)[0]
    if short.upper() in _WINDOWS_RESERVED:
        return False
    tilde = short.rfind("~")
    return not (0 <= tilde < len(short) - 1 and short[tilde + 1:].isdigit())


def installable_provider(command: str, provider: dict[str, Any]) -> bool:
    """Ecosystem rules beyond `valid_command` for names an install can create."""
    return provider.get("ecosystem") != "go" or go_installable_command(command)


def shard(command: str) -> str:
    """Two normalized characters; unsafe/non-ASCII names use a stable hash shard."""
    folded = command.casefold()
    if re.match(r"^[a-z0-9][a-z0-9._+@-]*$", folded):
        return (folded + "_")[:2]
    return "_" + hashlib.sha256(command.encode()).hexdigest()[:2]


def filename(command: str) -> str:
    if re.match(r"^[A-Za-z0-9._+@-]+$", command):
        return command + ".json"
    return hashlib.sha256(command.encode()).hexdigest() + ".json"


def provider_key(p: dict[str, Any]) -> tuple[str, ...]:
    def stable(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return str(value)
    return tuple(stable(p.get(k)) for k in PROVIDER_KEYS)


def provider_matches_scope(provider: dict[str, Any], scope: dict[str, str] | None) -> bool:
    """Return whether every requested logical dimension matches a provider."""
    if not scope:
        return True
    return all(provider.get(key) == value for key, value in scope.items())


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records))
