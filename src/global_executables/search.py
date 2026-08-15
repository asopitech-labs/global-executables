from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import filename, provider_matches_scope, shard, valid_command


class DatasetIndexError(RuntimeError):
    """A required derived index is absent, malformed, or not from this build."""


class Dataset:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.data = self.root / "data"

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads((self.data / "metadata.json").read_text())

    @property
    def freshness(self) -> dict[str, Any]:
        """Return the latest partial-scan report without changing canonical metadata."""
        path = self.root / "reports/freshness.json"
        if not path.is_file():
            return {"status": "unavailable", "coverage_kind": "partial"}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {"status": "invalid", "coverage_kind": "partial"}

    def get(self, name: str, scope: dict[str, str] | None = None) -> dict[str, Any] | None:
        if not valid_command(name):
            return None
        path = self.data / "executables" / shard(name) / filename(name)
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if value["command"] != name:
            return None
        if scope:
            providers = [p for p in value["providers"] if provider_matches_scope(p, scope)]
            if not providers:
                return None
            value = {**value, "providers": providers}
        return value

    def check(self, name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
        meta = self.metadata
        record = self.get(name, scope)
        coverage = meta.get("coverage", {})
        exhaustive = meta.get("negative_lookup") == "exhaustive" and bool(coverage) and all(
            source.get("status") == "success" and source.get("coverage_kind") == "exhaustive"
            for source in coverage.values()
        )
        coverage_scope = "exhaustive" if exhaustive else "unknown"
        status = "collision" if record else ("clear_in_index" if exhaustive else "unknown")
        result = {"name": name, "found": bool(record), "status": status, "snapshot": meta["snapshot"],
                  "coverage_scope": coverage_scope,
                  "checked_sources": meta.get("checked_sources", []),
                  "searched_sources": meta.get("checked_sources", []),
                  "coverage": coverage,
                  "freshness": self.freshness.get("status", "unavailable")}
        if record:
            result["providers"] = record["providers"]
        else:
            result["absence"] = {
                "status": "not_found_in_current_index",
                "confidence": "exhaustive" if exhaustive else "insufficient_coverage",
                "searched_sources": meta.get("checked_sources", []),
            }
        if scope:
            result["scope"] = scope
        return result

    def _read_index(self, relative: str) -> set[str]:
        meta = self.metadata
        expected = meta.get("index_manifest", {}).get(relative)
        path = self.data / relative
        if expected is None or not path.is_file():
            raise DatasetIndexError(f"required index missing from manifest or disk: {relative}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise DatasetIndexError(f"derived index is stale or corrupt: {relative}")
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as error:
            raise DatasetIndexError(f"invalid JSON index: {relative}") from error
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise DatasetIndexError(f"invalid index entries: {relative}")
        return set(values)

    def _all_from_group(self, group: str) -> set[str]:
        paths = sorted(relative for relative in self.metadata.get("index_manifest", {}) if relative.startswith(f"indexes/{group}/"))
        if not paths:
            raise DatasetIndexError(f"index group is missing: {group}")
        return set().union(*(self._read_index(path) for path in paths))

    def _read_optional_index(self, relative: str) -> set[str]:
        if relative not in self.metadata.get("index_manifest", {}):
            return set()
        return self._read_index(relative)

    def search(self, prefix: str = "", length: int | None = None, ecosystem: str | None = None,
               limit: int = 100, scope: dict[str, str] | None = None) -> list[str]:
        candidates: list[set[str]] = []
        if prefix:
            # Two-character safe prefixes map to one shard. One-character and
            # unusual prefixes read the bounded prefix-index group, never canonical files.
            if len(prefix) >= 2 and shard(prefix)[:1] != "_":
                candidates.append(self._read_optional_index(f"indexes/prefix/{shard(prefix)}.json"))
            else:
                candidates.append(self._all_from_group("prefix"))
        if length is not None:
            candidates.append(self._read_optional_index(f"indexes/length/{length}.json"))
        if ecosystem:
            candidates.append(self._read_optional_index(f"indexes/ecosystem/{ecosystem}.json"))
        if scope:
            for dimension, value in scope.items():
                candidates.append(self._read_optional_index(f"indexes/scope/{dimension}/{value}.json"))
        names = set.intersection(*candidates) if candidates else self._all_from_group("prefix")
        filtered = (name for name in names if (not prefix or name.startswith(prefix)) and (length is None or len(name) == length))
        return sorted(filtered, key=lambda value: (value.casefold(), value))[:limit]

    def similar(self, name: str, limit: int = 20, scope: dict[str, str] | None = None) -> list[dict[str, Any]]:
        def distance(a: str, b: str) -> int:
            row = list(range(len(b) + 1))
            for i, left in enumerate(a, 1):
                following = [i] + [0] * len(b)
                for j, right in enumerate(b, 1):
                    following[j] = min(following[j - 1] + 1, row[j] + 1, row[j - 1] + (left != right))
                row = following
            return row[-1]

        def grams(value: str) -> set[str]:
            padded = f"  {value.casefold()}  "
            return {padded[i:i + 3] for i in range(len(padded) - 2)}

        target = grams(name)
        postings = []
        manifest = self.metadata.get("index_manifest", {})
        for trigram in target:
            relative = f"indexes/trigram/{trigram.encode().hex()}.json"
            if relative in manifest:
                postings.append(self._read_index(relative))
        candidates = set().union(*postings) if postings else set()
        if scope:
            scope_sets = [self._read_optional_index(f"indexes/scope/{dimension}/{value}.json")
                          for dimension, value in scope.items()]
            candidates &= set.intersection(*scope_sets) if scope_sets else candidates
        found = []
        for candidate in candidates:
            candidate_grams = grams(candidate)
            similarity = len(target & candidate_grams) / len(target | candidate_grams)
            edit_distance = distance(name.casefold(), candidate.casefold())
            if candidate.startswith(name) or name.startswith(candidate) or edit_distance <= 2 or similarity >= .3:
                found.append({"name": candidate, "edit_distance": edit_distance, "trigram_similarity": round(similarity, 3)})
        return sorted(found, key=lambda value: (value["edit_distance"], -value["trigram_similarity"], value["name"]))[:limit]
