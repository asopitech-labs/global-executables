from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

COMMAND = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,255}$")
PROVIDER_KEYS = ("ecosystem", "package", "version", "repository", "source", "confidence", "alias_of")


def valid_command(command: str) -> bool:
    return bool(COMMAND.fullmatch(command)) and command not in {".", ".."}


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
    return tuple("" if p.get(k) is None else str(p.get(k, "")) for k in PROVIDER_KEYS)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records))
