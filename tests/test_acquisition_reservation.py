"""Durable ACK, replay, and conflict handling for on-demand reservations."""
from __future__ import annotations

import base64
import json

import pytest

from global_executables.acquisition_reservation import (
    GitHubReservationStore, ReservationError, ReservationUnavailable,
    package_key,
)


class ContentsAPI:
    def __init__(self):
        self.content = None
        self.sha = None
        self.writes = 0
        self.conflict = False
        self.fail = False

    def __call__(self, method, path, payload):
        assert path.startswith("/repos/asopitech-labs/global-executables/contents/data/production/on-demand/reservations.json")
        if self.fail:
            return 503, {"message": "unavailable"}
        if method == "GET":
            if self.content is None:
                return 404, {"message": "Not Found"}
            return 200, {"content": self.content, "sha": self.sha}
        assert method == "PUT"
        if self.conflict:
            self.conflict = False
            return 409, {"message": "conflict"}
        if payload.get("sha") != self.sha:
            return 409, {"message": "conflict"}
        self.content = payload["content"]
        self.writes += 1
        self.sha = str(self.writes)
        return 201, {"content": {"sha": self.sha}}

    def state(self):
        return json.loads(base64.b64decode(self.content))


def test_durable_ack_and_package_keyed_replay():
    api = ContentsAPI()
    store = GitHubReservationStore("test-token", request=api)
    first = store.reserve("pypi", "My_Package", "request-1", "2026-10-21T10:00:00Z")
    again = store.reserve("pypi", "my.package", "request-2", "2026-10-21T10:01:00Z")
    assert first["acknowledged"] is True
    assert first["id"] == again["id"]
    assert first["key"] == "pypi:my-package"
    assert api.writes == 1
    assert len(api.state()["reservations"]) == 1


def test_conflict_reloads_before_acknowledgement():
    api = ContentsAPI()
    api.conflict = True
    store = GitHubReservationStore("test-token", request=api)
    result = store.reserve("npm", "@scope/tool", "request-1", "2026-10-21T10:00:00Z")
    assert result["acknowledged"] is True
    assert api.writes == 1
    assert api.state()["reservations"]["npm:@scope/tool"]["id"] == result["id"]


def test_unavailable_central_store_never_acknowledges():
    api = ContentsAPI()
    api.fail = True
    with pytest.raises(ReservationUnavailable):
        GitHubReservationStore("test-token", request=api).reserve(
            "npm", "tool", "request-1", "2026-10-21T10:00:00Z")
    assert api.writes == 0


@pytest.mark.parametrize("ecosystem,package", [
    ("npm", "https://evil.test/x"), ("npm", "../tool"), ("npm", "@scope/../../x"),
    ("pypi", "foo/bar"), ("pypi", "-bad"), ("go", "example.com/tool"),
])
def test_untrusted_package_identifiers_are_rejected(ecosystem, package):
    with pytest.raises(ReservationError):
        package_key(ecosystem, package)


def test_invalid_request_does_not_write():
    api = ContentsAPI()
    with pytest.raises(ReservationError):
        GitHubReservationStore("test-token", request=api).reserve(
            "npm", "tool", "bad/request", "2026-10-21T10:00:00Z")
    assert api.writes == 0


def test_claim_retry_and_terminal_requeue():
    api = ContentsAPI()
    store = GitHubReservationStore("test-token", request=api)
    original = store.reserve("npm", "tool", "request-1", "2026-10-21T10:00:00Z")
    assert [item["key"] for item in store.list_pending(10, now="2026-10-21T10:00:01Z")] == ["npm:tool"]
    lease = store.claim("npm:tool", "worker-1", "2026-10-21T10:00:02Z")
    assert lease["status"] == "leased"
    assert store.claim("npm:tool", "worker-1", "2026-10-21T10:00:02Z")["lease"] == lease["lease"]
    assert store.claim("npm:tool", "worker-2", "2026-10-21T10:01:00Z") is None
    retried = store.mark_retryable("npm:tool", lease["lease"]["id"], "HTTP 503",
                                   "2026-10-21T10:10:00Z")
    assert retried["status"] == "retryable"
    assert store.mark_retryable("npm:tool", lease["lease"]["id"], "HTTP 503",
                                "2026-10-21T10:10:00Z")["status"] == "retryable"
    assert store.list_pending(10, now="2026-10-21T10:09:59Z") == []
    lease = store.claim("npm:tool", "worker-2", "2026-10-21T10:10:01Z")
    assert lease["attempts"] == 2
    with pytest.raises(ReservationError, match="lease changed"):
        store.mark_processed("npm:tool", "stale-lease", "2026-10-21T10:10:02Z")
    result = store.mark_processed("npm:tool", lease["lease"]["id"],
                                  "2026-10-21T10:10:02Z", observed_version="1.0.0",
                                  evidence_digest="a" * 64)
    assert result["status"] == "processed"
    assert store.mark_processed("npm:tool", lease["lease"]["id"],
                                "2026-10-21T10:10:02Z", observed_version="1.0.0",
                                evidence_digest="a" * 64)["status"] == "processed"
    assert store.list_pending(10, now="2026-10-22T10:10:01Z") == []
    assert store.claim("npm:tool", "worker-3", "2026-10-22T10:10:01Z") is None
    assert [item["key"] for item in store.list_pending(10, now="2026-10-22T10:10:02Z")] == ["npm:tool"]
    stale_lease = store.claim("npm:tool", "worker-3", "2026-10-22T10:10:02Z")
    assert stale_lease["attempts"] == 3
    store.mark_processed("npm:tool", stale_lease["lease"]["id"],
                         "2026-10-22T10:10:03Z", observed_version=None,
                         evidence_digest=None, outcome="not_published")
    assert api.state()["reservations"]["npm:tool"]["outcome"] == "not_published"
    assert store.reserve("npm", "tool", "request-1", "2026-10-21T10:11:00Z")["status"] == "processed"
    again = store.reserve("npm", "tool", "request-2", "2026-10-21T10:12:00Z")
    assert again["id"] == original["id"]
    assert again["status"] == "queued"
    assert again["attempts"] == 0
