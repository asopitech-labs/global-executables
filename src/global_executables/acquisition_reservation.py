"""Durable, package-keyed reservations for bounded on-demand acquisition.

The service owning a GitHub token uses this store; MCP and CLI clients never do.
The file lives on artifact-data so normal dictionary refreshes cannot erase it.
Only a successful Contents API compare-and-swap, or a read of an existing
reservation, is an acknowledgement.  This module does not inspect packages.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

SCHEMA_VERSION = 1
MAX_RESERVATIONS = 10000
STORE_PATH = "data/production/on-demand/reservations.json"
_NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]{0,213}/)?[a-z0-9][a-z0-9._~-]{0,213}$")
_PYPI_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,198}[a-z0-9])?$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ReservationError(ValueError):
    """Invalid request or corrupt durable state."""


class ReservationUnavailable(RuntimeError):
    """The central store did not durably accept a reservation."""


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ReservationError("requested_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ReservationError("requested_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def package_key(ecosystem: str, package: str) -> tuple[str, str, str]:
    """Normalize an exact npm/PyPI package identifier; never accept a URL/path."""
    if not isinstance(package, str) or len(package) > 255:
        raise ReservationError("invalid package identifier")
    if ecosystem == "npm" and _NPM_NAME.fullmatch(package.lower()):
        normalized = package.lower()
    elif ecosystem == "pypi" and _PYPI_NAME.fullmatch(package.lower()):
        normalized = re.sub(r"[-_.]+", "-", package.lower())
    else:
        raise ReservationError("unsupported ecosystem or invalid package identifier")
    return ecosystem, normalized, f"{ecosystem}:{normalized}"


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "reservations": {}}


def _decode(content: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(content, validate=True)
        if len(raw) > 5_000_000:
            raise ReservationError("reservation store exceeds size limit")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, binascii.Error) as error:
        raise ReservationError("invalid reservation store") from error
    if (not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION
            or not isinstance(data.get("reservations"), dict)):
        raise ReservationError("unsupported reservation store schema")
    return data


def reserve_in_state(state: dict[str, Any], ecosystem: str, package: str,
                     request_id: str, requested_at: str) -> tuple[dict[str, Any], bool]:
    """Pure mutation, suitable for replay after a branch compare-and-swap conflict."""
    ecosystem, package, key = package_key(ecosystem, package)
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ReservationError("invalid request_id")
    when = _utc(requested_at)
    if state.get("schema_version") != SCHEMA_VERSION or not isinstance(state.get("reservations"), dict):
        raise ReservationError("unsupported reservation store schema")
    reservations = state["reservations"]
    existing = reservations.get(key)
    if existing is not None:
        if not isinstance(existing, dict) or existing.get("ecosystem") != ecosystem or existing.get("package") != package:
            raise ReservationError("reservation identity conflict")
        if existing.get("request_id") == request_id:
            return existing, False
        if existing.get("status") in {"processed", "indexed", "rejected"}:
            previous = {name: existing.pop(name) for name in (
                "processed_at", "observed_version", "evidence_digest", "completed_lease_id",
                "dictionary_snapshot", "retry_reason", "outcome",
            ) if name in existing}
            if previous:
                existing["previous_result"] = previous
            existing.update(status="queued", requested_at=when, request_id=request_id, attempts=0)
            existing.pop("lease", None)
            existing.pop("next_attempt_at", None)
            return existing, True
        return existing, False
    if len(reservations) >= MAX_RESERVATIONS:
        raise ReservationUnavailable("reservation store capacity reached")
    reservation = {
        "id": hashlib.sha256(key.encode()).hexdigest(), "key": key,
        "ecosystem": ecosystem, "package": package, "selector": "latest",
        "status": "queued", "requested_at": when, "request_id": request_id,
        "attempts": 0,
    }
    reservations[key] = reservation
    return reservation, True


Request = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class GitHubReservationStore:
    """GitHub Contents API adapter with bounded read/modify/write retries.

    ``request`` can be injected for deterministic tests. The default sends only
    to api.github.com; the token stays in this trusted service process.
    """

    def __init__(self, token: str, *, repository: str = "asopitech-labs/global-executables",
                 branch: str = "artifact-data", request: Request | None = None):
        if not token or not isinstance(token, str):
            raise ReservationError("central GitHub token is required")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ReservationError("invalid repository")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
            raise ReservationError("invalid branch")
        self._token = token
        self._request = request or self._github_request
        self._path = f"/repos/{repository}/contents/{STORE_PATH}"
        self._branch = branch

    def _github_request(self, method: str, path: str,
                        payload: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "global-executables-on-demand/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request("https://api.github.com" + path, data=body,
                                     headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=15) as response:
                raw = response.read(5_000_001)
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read(5_000_001)
            status = error.code
        except (urllib.error.URLError, TimeoutError) as error:
            raise ReservationUnavailable("central reservation store is unavailable") from error
        if len(raw) > 5_000_000:
            raise ReservationUnavailable("central reservation response exceeds size limit")
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise ReservationUnavailable("invalid central reservation response") from error
        if not isinstance(data, dict):
            raise ReservationUnavailable("invalid central reservation response")
        return status, data

    def _read(self) -> tuple[dict[str, Any], str | None]:
        path = self._path + "?ref=" + urllib.parse.quote(self._branch, safe="")
        status, data = self._request("GET", path, None)
        if status == 404:
            return _empty(), None
        if status != 200:
            raise ReservationUnavailable(f"central reservation read failed: HTTP {status}")
        sha = data.get("sha")
        content = data.get("content")
        if not isinstance(sha, str) or not isinstance(content, str):
            raise ReservationUnavailable("central reservation file is malformed")
        return _decode("".join(content.split())), sha

    def reserve(self, ecosystem: str, package: str, request_id: str,
                requested_at: str) -> dict[str, Any]:
        """Return a durable ACK only after a successful write or confirmed read.

        A conflict causes a fresh read and deterministic replay, never a blind
        overwrite. A possible timeout after a successful PUT is deliberately not
        acknowledged; the caller can retry the same package safely.
        """
        return self._mutate(
            lambda state: reserve_in_state(state, ecosystem, package, request_id, requested_at),
            "Reserve on-demand package",
        )

    def _mutate(self, change: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, bool]],
                message: str) -> dict[str, Any] | None:
        for _ in range(3):
            state, sha = self._read()
            result, changed = change(state)
            if not changed:
                return None if result is None else {**result, "acknowledged": True}
            encoded = base64.b64encode((json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n").encode()).decode()
            payload: dict[str, Any] = {"message": message, "content": encoded, "branch": self._branch}
            if sha is not None:
                payload["sha"] = sha
            status, _ = self._request("PUT", self._path, payload)
            if status in (200, 201):
                return None if result is None else {**result, "acknowledged": True}
            if status not in (409, 422):
                raise ReservationUnavailable(f"central reservation write failed: HTTP {status}")
        raise ReservationUnavailable("central reservation changed too often")

    def list_pending(self, limit: int, *, now: str | None = None) -> list[dict[str, Any]]:
        """List due work from the durable central state without changing leases."""
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ReservationError("invalid pending limit")
        current = _utc(now or datetime.now(timezone.utc).isoformat())
        stale_before = (datetime.fromisoformat(current.replace("Z", "+00:00"))
                        - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        state, _ = self._read()
        pending = []
        for item in state["reservations"].values():
            status = item.get("status")
            if (status == "queued" or
                    status == "retryable" and item.get("next_attempt_at", "") <= current or
                    status == "leased" and item.get("lease", {}).get("until", "") <= current or
                    status in {"processed", "indexed"} and item.get("processed_at", "") <= stale_before):
                pending.append(item)
        return sorted(pending, key=lambda item: (item.get("requested_at", ""), item.get("key", "")))[:limit]

    def claim(self, key: str, worker_id: str, claimed_at: str, *,
              lease_seconds: int = 300) -> dict[str, Any] | None:
        """Lease a queued/due reservation; expired leases can be reclaimed."""
        if not isinstance(worker_id, str) or not _REQUEST_ID.fullmatch(worker_id):
            raise ReservationError("invalid worker_id")
        now = _utc(claimed_at)
        stale_before = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                        - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        if not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 3600:
            raise ReservationError("invalid lease duration")

        def change(state: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
            item = state["reservations"].get(key)
            if item is None:
                return None, False
            status = item.get("status")
            if status == "retryable" and item.get("next_attempt_at", "") > now:
                return None, False
            if status == "leased" and item.get("lease", {}).get("until", "") > now:
                lease = item["lease"]
                replay_id = hashlib.sha256(f"{key}:{worker_id}:{now}:{item.get('attempts', 0)}".encode()).hexdigest()
                if lease.get("id") == replay_id:
                    return item, False
                return None, False
            if status in {"processed", "indexed"} and item.get("processed_at", "") > stale_before:
                return None, False
            if status not in {"queued", "retryable", "leased", "processed", "indexed"}:
                return None, False
            attempts = int(item.get("attempts", 0)) + 1
            lease_id = hashlib.sha256(f"{key}:{worker_id}:{now}:{attempts}".encode()).hexdigest()
            until = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                     + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
            item.update(status="leased", attempts=attempts,
                        lease={"id": lease_id, "worker_id": worker_id, "until": until})
            item.pop("next_attempt_at", None)
            return item, True

        return self._mutate(change, f"Claim on-demand package {key}")

    def mark_processed(self, key: str, lease_id: str, processed_at: str, *,
                       observed_version: str | None = None,
                       evidence_digest: str | None = None,
                       outcome: str = "verified_entry_point") -> dict[str, Any]:
        """Record an independently verified result; publication is a later step."""
        when = _utc(processed_at)
        if observed_version is not None and (not isinstance(observed_version, str) or not 0 < len(observed_version) <= 128):
            raise ReservationError("invalid observed version")
        if evidence_digest is not None and (not isinstance(evidence_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", evidence_digest)):
            raise ReservationError("invalid evidence digest")
        if outcome not in {"verified_entry_point", "not_published", "unverified"}:
            raise ReservationError("invalid acquisition outcome")

        def change(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            item = state["reservations"].get(key)
            if (item and item.get("status") == "processed" and item.get("completed_lease_id") == lease_id
                    and item.get("observed_version") == observed_version
                    and item.get("evidence_digest") == evidence_digest
                    and item.get("outcome") == outcome):
                return item, False
            if not item or item.get("status") != "leased" or item.get("lease", {}).get("id") != lease_id:
                raise ReservationError("reservation lease changed")
            item.update(status="processed", processed_at=when,
                        observed_version=observed_version, evidence_digest=evidence_digest,
                        completed_lease_id=lease_id, outcome=outcome)
            item.pop("lease", None)
            return item, True

        result = self._mutate(change, f"Verify on-demand package {key}")
        assert result is not None
        return result

    def mark_retryable(self, key: str, lease_id: str, reason: str,
                       next_attempt_at: str) -> dict[str, Any]:
        """Keep transient failures queued for a bounded worker retry."""
        when = _utc(next_attempt_at)
        if not isinstance(reason, str) or not 0 < len(reason) <= 256:
            raise ReservationError("invalid retry reason")

        def change(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            item = state["reservations"].get(key)
            if (item and item.get("status") == "retryable" and item.get("completed_lease_id") == lease_id
                    and item.get("retry_reason") == reason and item.get("next_attempt_at") == when):
                return item, False
            if not item or item.get("status") != "leased" or item.get("lease", {}).get("id") != lease_id:
                raise ReservationError("reservation lease changed")
            item.update(status="retryable", next_attempt_at=when, retry_reason=reason,
                        completed_lease_id=lease_id)
            item.pop("lease", None)
            return item, True

        result = self._mutate(change, f"Retry on-demand package {key}")
        assert result is not None
        return result
