"""Conservative negative-query completeness and machine-readable caveats."""
from __future__ import annotations

from typing import Any


def limited_population(source: dict[str, Any]) -> bool:
    """Legacy scoped/catalog metadata cannot establish registry-wide absence."""
    return bool(source.get("scope")) or any(key.startswith("catalog_") for key in source)


def coverage_scope(metadata: dict[str, Any], scope: dict[str, str] | None = None) -> str:
    coverage = metadata.get("coverage", {})
    # Provider filters do not describe the population searched. Until coverage
    # declares completeness for those dimensions, a scoped negative is unknown.
    if scope or metadata.get("negative_lookup") != "exhaustive" or not coverage:
        return "unknown"
    return "exhaustive" if all(
        source.get("status") == "success"
        and source.get("coverage_kind") == "exhaustive"
        and not limited_population(source)
        for source in coverage.values()
    ) else "unknown"


def coverage_warnings(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    npm = metadata.get("coverage", {}).get("npm", {})
    if (npm.get("status") == "success" and npm.get("coverage_kind") == "exhaustive"
            and not limited_population(npm)):
        return []
    provenance = {key: npm[key] for key in (
        "status", "coverage_kind", "source", "scope", "catalog_complete", "catalog_packages",
        "catalog_snapshot", "catalog_source", "catalog_digest",
    ) if key in npm}
    return [{
        "code": "npm_coverage_incomplete",
        "ecosystem": "npm",
        "message": "npm coverage is incomplete or limited to a declared population. "
                   "A missing result is not proof that no npm package uses this executable "
                   "and is not permission to adopt the name.",
        "coverage": provenance,
        "reference": "https://github.com/asopitech-labs/global-executables/issues/54",
    }]
