#!/usr/bin/env python3
"""Conditionally refresh PyPI's append-only project catalogue."""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

from global_executables.registry_state import load_state, save_state


SIMPLE_URL = "https://pypi.org/simple/"
MEDIA_TYPE = "application/vnd.pypi.simple.v1+json"
MINIMUM_LIVE_RATIO = 0.95
MAXIMUM_GROWTH_RATIO = 0.50
MINIMUM_FETCH_INTERVAL = timedelta(hours=24)
MAXIMUM_RESPONSE_BYTES = 256 * 1024 * 1024
_NORMALIZE = re.compile(r"[-_.]+")


class RefreshResult(NamedTuple):
    added: int = 0
    missing: int = 0
    not_modified: bool = False
    skipped: bool = False
    error: str = ""


def normalize_name(name: str) -> str:
    """Return the PEP 503 identity used to compare old and live project names."""
    return _NORMALIZE.sub("-", name).lower()


def user_agent(contact: str = "") -> str:
    details = "+https://github.com/asopitech-labs/global-executables"
    if contact.strip():
        details += f"; {contact.strip()}"
    return f"global-executables-pypi-catalog/1.0 ({details})"


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _catalog_names(path: Path) -> tuple[bytes, list[str], dict[str, str]]:
    body = path.read_bytes()
    if body and not body.endswith(b"\n"):
        raise ValueError("local catalog does not end with a newline")
    try:
        names = body.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("local catalog is not UTF-8") from error
    identities: dict[str, str] = {}
    for name in names:
        if not name or name != name.strip():
            raise ValueError("local catalog contains an empty or padded name")
        identity = normalize_name(name)
        if identity in identities:
            raise ValueError(f"local catalog has duplicate normalized name {identity!r}")
        identities[identity] = name
    return body, names, identities


def _live_names(body: bytes) -> tuple[dict[str, str], int]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"malformed PyPI JSON: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        raise ValueError("malformed PyPI JSON: missing meta object")
    serial = payload["meta"].get("_last-serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise ValueError("malformed PyPI JSON: meta._last-serial is not a non-negative integer")
    projects = payload.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("malformed PyPI JSON: projects is not a non-empty array")
    names: dict[str, str] = {}
    for project in projects:
        name = project.get("name") if isinstance(project, dict) else None
        if not isinstance(name, str) or not name or name != name.strip() or "\n" in name or "\r" in name:
            raise ValueError("malformed PyPI JSON: project name is missing or invalid")
        identity = normalize_name(name)
        if identity in names:
            raise ValueError(f"malformed PyPI JSON: duplicate normalized project name {identity!r}")
        names[identity] = name
    return names, serial


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_attempt(state_path: Path, state: dict, entry: dict, now: datetime, error: str) -> None:
    entry["catalog_fetched_at"] = _utc_text(now)
    entry["catalog_refresh_error"] = error
    save_state(state_path, state)


def refresh_catalog(
    catalog_path: Path,
    companion_path: Path,
    state_path: Path,
    *,
    opener: Callable = urllib.request.urlopen,
    now: datetime | None = None,
    contact: str | None = None,
) -> RefreshResult:
    """Fetch and append new names; every rejection keeps both catalogue files intact."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = load_state(state_path)
    sources = state.setdefault("sources", {})
    entry = sources.setdefault("pypi", {})
    fetched_at = _parse_time(entry.get("catalog_fetched_at"))
    if fetched_at is not None and now - fetched_at < MINIMUM_FETCH_INTERVAL:
        return RefreshResult(skipped=True)

    try:
        old_body, old_names, old_identities = _catalog_names(catalog_path)
    except (OSError, ValueError) as error:
        message = f"local catalog error: {error}"
        _save_attempt(state_path, state, entry, now, message)
        return RefreshResult(error=message)

    entry["catalog_size"] = len(old_names)
    headers = {"Accept": MEDIA_TYPE, "User-Agent": user_agent(contact if contact is not None else os.environ.get("REGISTRY_CONTACT", ""))}
    if isinstance(entry.get("catalog_etag"), str) and entry["catalog_etag"]:
        headers["If-None-Match"] = entry["catalog_etag"]
    request = urllib.request.Request(SIMPLE_URL, headers=headers)
    try:
        with opener(request, timeout=120) as response:
            response_body = response.read(MAXIMUM_RESPONSE_BYTES + 1)
            if len(response_body) > MAXIMUM_RESPONSE_BYTES:
                raise ValueError(f"malformed PyPI JSON: response exceeds {MAXIMUM_RESPONSE_BYTES} bytes")
            etag = response.headers.get("ETag")
    except urllib.error.HTTPError as error:
        if error.code == 304:
            _save_attempt(state_path, state, entry, now, "")
            return RefreshResult(not_modified=True)
        message = f"PyPI catalog request failed: HTTP {error.code}"
        _save_attempt(state_path, state, entry, now, message)
        return RefreshResult(error=message)
    except (OSError, TimeoutError, ValueError) as error:
        message = f"PyPI catalog request failed: {error}"
        _save_attempt(state_path, state, entry, now, message)
        return RefreshResult(error=message)

    try:
        live, serial = _live_names(response_body)
        old_count = len(old_identities)
        if len(live) < math.ceil(old_count * MINIMUM_LIVE_RATIO):
            raise ValueError(f"live catalog shrunk below 95%: {len(live)} < {math.ceil(old_count * MINIMUM_LIVE_RATIO)}")
        added_identities = live.keys() - old_identities.keys()
        if len(added_identities) > math.floor(old_count * MAXIMUM_GROWTH_RATIO):
            raise ValueError(f"catalog growth exceeds 50%: {len(added_identities)} new for {old_count} existing")
    except ValueError as error:
        message = str(error)
        _save_attempt(state_path, state, entry, now, message)
        return RefreshResult(error=message)

    additions = sorted((live[identity] for identity in added_identities), key=lambda name: (normalize_name(name), name))
    missing = len(old_identities.keys() - live.keys())
    if additions:
        appended = "".join(f"{name}\n" for name in additions).encode("utf-8")
        new_body = old_body + appended
        # Install the companion first. A crash can leave it ahead of the plain file, but
        # never leave the crawler's canonical plain file ahead of its publication copy.
        _atomic_write(companion_path, gzip.compress(new_body, compresslevel=9, mtime=0))
        _atomic_write(catalog_path, new_body)

    entry.update({
        "catalog_size": len(old_names) + len(additions),
        "catalog_etag": etag or entry.get("catalog_etag", ""),
        "catalog_serial": serial,
        "catalog_live_size": len(live),
        "catalog_missing": missing,
    })
    _save_attempt(state_path, state, entry, now, "")
    return RefreshResult(added=len(additions), missing=missing)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--companion", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    result = refresh_catalog(args.catalog, args.companion, args.state)
    print(json.dumps(result._asdict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
