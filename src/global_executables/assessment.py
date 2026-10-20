"""Derived freshness and collision-risk assessments over factual providers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .search import Dataset

METHODOLOGY_VERSION = "1.0.0"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _provider_assessment(provider: dict[str, Any], now: datetime) -> dict[str, Any]:
    release = _parse(provider.get("latest_release_at"))
    observed = _parse(provider.get("last_observed_at"))
    metrics = provider.get("usage_metrics") or []
    recent_counts = [int(item.get("count", 0)) for item in metrics if isinstance(item, dict)]
    has_usage = bool(recent_counts)
    active = bool(release and (now - release).days <= 365) or any(count > 0 for count in recent_counts)
    stale = bool(release and (now - release).days > 365)
    freshness = "recent" if release and (now - release).days <= 365 else ("stale" if stale else "unknown")
    activity = "active" if active else ("inactive" if stale and not has_usage else "unknown")
    popularity = "common" if any(count >= 1000 for count in recent_counts) else ("observed" if has_usage else "unknown")
    return {"freshness": freshness, "activity": activity, "popularity": popularity,
            "latest_release_at": provider.get("latest_release_at"),
            "last_observed_at": provider.get("last_observed_at"),
            "usage_metrics": metrics}


def assess(dataset: Dataset, name: str, scope: dict[str, str] | None = None) -> dict[str, Any]:
    observation = dataset.check(name, scope)
    providers = observation.get("providers", [])
    now = datetime.now(timezone.utc)
    signals = [_provider_assessment(provider, now) for provider in providers]
    if not providers:
        risk = "insufficient_evidence" if not observation["found"] else "historical_low_activity"
        freshness = activity = popularity = "unknown"
    elif any(signal["activity"] == "active" or signal["popularity"] == "common" for signal in signals):
        risk = "active_common"
        freshness = "recent" if any(signal["freshness"] == "recent" for signal in signals) else "stale"
        activity = "active"
        popularity = "common" if any(signal["popularity"] == "common" for signal in signals) else "observed"
    elif all(signal["freshness"] == "stale" and signal["activity"] == "inactive" for signal in signals):
        risk = "historical_low_activity"
        freshness, activity, popularity = "stale", "inactive", "unknown"
    else:
        risk = "insufficient_evidence"
        freshness = activity = popularity = "unknown"
    return {"name": name, "found": observation["found"], "snapshot": observation["snapshot"],
            "coverage": observation.get("coverage", {}), "coverage_scope": observation["coverage_scope"],
            "assessment": {"freshness": freshness, "activity": activity, "popularity": popularity,
                           "collision_risk": risk, "methodology_version": METHODOLOGY_VERSION},
            "signals": signals, "providers": providers, "scope": scope}
