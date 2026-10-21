"""Central reservation is acknowledged only after the central service accepts it.

These tests use an HTTP fixture. They do not prove that a hosted reservation
endpoint exists or that a scheduled collector has published the observation.
"""

from __future__ import annotations

import io
import importlib.util
import json
import base64
import urllib.error
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from global_executables import cli, mcp_server
from global_executables.acquisition_reservation import (
    GitHubReservationStore,
    ReservationError,
    ReservationUnavailable,
    package_key,
)
from global_executables.reservation_client import reserve_discovery
from global_executables.reservation_server import create_handler
from global_executables.pipeline import publish


def observation(*, outcome="verified_entry_point", complete=True):
    return {
        "outcome": outcome,
        "verification_complete": complete,
        "ecosystem": "npm",
        "canonical_package": "sample-package",
        "version": "1.2.3",
        "commands": [{"command": "sample", "entry_point": "bin/sample.js"}],
    }


class Response(io.BytesIO):
    def __init__(self, status: int, body: dict):
        super().__init__(json.dumps(body).encode())
        self.status = status


def test_unverified_observation_never_posts_to_central_service(monkeypatch):
    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("unverified observation must not be reserved")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_request)
    for item in (observation(outcome="unverified"), observation(complete=False)):
        assert reserve_discovery(item, "https://reservation.example.test/reserve") == {
            "status": "not_eligible"
        }


def test_online_client_requires_configured_https_endpoint(monkeypatch):
    monkeypatch.delenv("GLOBAL_EXECUTABLES_RESERVATION_URL", raising=False)
    assert reserve_discovery(observation())["status"] == "unavailable"
    for endpoint in ("http://reservation.example.test/reserve",
                     "https://user:secret@reservation.example.test/reserve",
                     "https://reservation.example.test/reserve#fragment"):
        assert reserve_discovery(observation(), endpoint)["status"] == "unavailable"


def test_verified_online_discovery_posts_canonical_package_and_requires_durable_ack(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response(202, {"status": "reserved", "reservation_id": "npm:sample-package"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = reserve_discovery(observation(), "https://reservation.example.test/reserve")
    assert result == {"status": "reserved", "reservation_id": "npm:sample-package"}
    assert len(requests) == 1
    request, timeout = requests[0]
    assert timeout <= 5
    assert request.get_method() == "POST"
    assert request.full_url == "https://reservation.example.test/reserve"
    assert json.loads(request.data) == {"ecosystem": "npm", "package": "sample-package"}


def test_failed_or_invalid_central_ack_never_claims_reservation(monkeypatch):
    for status, body in ((200, {"status": "reserved", "reservation_id": "claim"}),
                         (202, {"status": "queued"}),
                         (202, {"status": "reserved", "reservation_id": None})):
        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *_args, status=status, body=body, **_kwargs: Response(status, body))
        assert reserve_discovery(observation(), "https://reservation.example.test/reserve")["status"] == "unavailable"

    def timeout(*_args, **_kwargs):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    assert reserve_discovery(observation(), "https://reservation.example.test/reserve")["status"] == "unavailable"


class FakeContentsAPI:
    """A branch-file fixture with a compare-and-swap SHA, not a hosted service."""

    def __init__(self, *, conflicts=0, write_status=None):
        self.document = None
        self.sha = None
        self.conflicts = conflicts
        self.write_status = write_status
        self.reads = 0
        self.writes = 0

    def __call__(self, method, path, payload):
        assert path.startswith("/repos/asopitech-labs/global-executables/contents/")
        assert path.split("?", 1)[0].endswith("/data/production/on-demand/reservations.json")
        if method == "GET":
            self.reads += 1
            assert path.endswith("?ref=artifact-data")
            if self.document is None:
                return 404, {"message": "Not Found"}
            return 200, {"sha": self.sha, "content": base64.b64encode(
                json.dumps(self.document).encode()).decode()}
        assert method == "PUT"
        self.writes += 1
        assert payload["branch"] == "artifact-data"
        assert payload.get("sha") == self.sha
        if self.conflicts:
            self.conflicts -= 1
            return 409, {"message": "Conflict"}
        if self.write_status is not None:
            return self.write_status, {"message": "Unavailable"}
        self.document = json.loads(base64.b64decode(payload["content"]))
        self.sha = f"file-sha-{self.writes}"
        return 200, {"content": {"sha": self.sha}}


def test_central_store_ack_follows_durable_write_and_retry_is_idempotent():
    api = FakeContentsAPI()
    store = GitHubReservationStore("fixture-token", request=api)
    first = store.reserve("pypi", "Sample_Package", "request-1", "2026-10-21T00:00:00Z")
    repeated = store.reserve("pypi", "sample.package", "request-2", "2026-10-21T01:00:00Z")

    assert first["acknowledged"] is True
    assert first["key"] == "pypi:sample-package"
    assert repeated["id"] == first["id"]
    assert repeated["request_id"] == "request-1"
    assert api.writes == 1
    assert len(api.document["reservations"]) == 1
    assert api.document["reservations"][first["key"]]["status"] == "queued"


def test_central_store_replays_after_compare_and_swap_conflict():
    api = FakeContentsAPI(conflicts=1)
    result = GitHubReservationStore("fixture-token", request=api).reserve(
        "npm", "sample-package", "request-1", "2026-10-21T00:00:00Z")
    assert result["acknowledged"] is True
    assert api.reads == 2 and api.writes == 2
    assert list(api.document["reservations"]) == ["npm:sample-package"]


def test_central_store_failure_never_returns_ack():
    api = FakeContentsAPI(write_status=503)
    with pytest.raises(ReservationUnavailable, match="write failed"):
        GitHubReservationStore("fixture-token", request=api).reserve(
            "npm", "sample-package", "request-1", "2026-10-21T00:00:00Z")
    assert api.document is None


def test_reservation_rejects_urls_and_unsupported_ecosystems():
    with pytest.raises(ReservationError):
        package_key("npm", "https://registry.npmjs.org/sample-package")
    with pytest.raises(ReservationError):
        package_key("go", "example.org/command")


def post_fixture(handler_type, payload):
    """Drive the real HTTP handler without binding a socket in the sandbox."""
    body = json.dumps(payload).encode()
    handler = handler_type.__new__(handler_type)
    handler.path = "/v1/reservations"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    statuses = []
    handler.send_response = lambda status: statuses.append(status)
    handler.send_header = lambda *_args: None
    handler.end_headers = lambda: None
    handler.do_POST()
    return statuses[0], json.loads(handler.wfile.getvalue())


def test_central_http_rechecks_public_evidence_before_durable_ack():
    class Discovery:
        calls = []
        result = {"outcome": "verified_entry_point", "verification_complete": True,
                  "canonical_package": "sample-package"}

        def discover(self, ecosystem, package, *, revalidate):
            self.calls.append((ecosystem, package, revalidate))
            return self.result

    class Store:
        calls = []

        def reserve(self, ecosystem, package, request_id, requested_at):
            self.calls.append((ecosystem, package))
            return {"id": "durable-reservation-id", "acknowledged": True}

    discovery, store = Discovery(), Store()
    handler_type = create_handler(store, discovery)
    status, body = post_fixture(handler_type, {"ecosystem": "npm", "package": "sample-package"})
    assert (status, body) == (202, {"status": "reserved", "reservation_id": "durable-reservation-id"})
    assert discovery.calls == [("npm", "sample-package", True)]
    assert store.calls == [("npm", "sample-package")]

    discovery.result = {"outcome": "unverified", "verification_complete": True}
    status, body = post_fixture(handler_type, {"ecosystem": "npm", "package": "sample-package"})
    assert (status, body) == (422, {"status": "not_reserved", "outcome": "unverified"})
    assert len(store.calls) == 1

    status, body = post_fixture(handler_type, {"ecosystem": "npm", "package": "https://evil.test/x"})
    assert (status, body) == (400, {"status": "invalid_request"})
    assert len(discovery.calls) == 2


def processor_module():
    path = Path(__file__).parents[1] / "tools/process_reservations.py"
    spec = importlib.util.spec_from_file_location("process_reservations_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_periodic_processor_rechecks_and_marks_processed_only_after_publication(monkeypatch, tmp_path):
    processor = processor_module()
    events = []

    class Store:
        def list_pending(self, limit):
            events.append(("list", limit))
            return [{"key": "npm:sample-package"}]

        def claim(self, key, worker, claimed_at, *, lease_seconds):
            assert lease_seconds == 600
            events.append(("claim", key, worker))
            return {"key": key, "ecosystem": "npm", "package": "sample-package",
                    "lease": {"id": "lease-1"}}

        def mark_processed(self, key, lease_id, processed_at, **evidence):
            events.append(("processed", key, lease_id, evidence))

    class Discovery:
        def discover(self, ecosystem, package, *, revalidate):
            events.append(("verify", ecosystem, package, revalidate))
            return {"outcome": "verified_entry_point", "version": "1.2.3"}

    monkeypatch.setenv("GH_TOKEN", "fixture-token")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setattr(processor, "GitHubReservationStore", lambda _token: Store())
    monkeypatch.setattr(processor, "PackageDiscovery", Discovery)
    monkeypatch.setattr(processor, "git", lambda *_args: None)
    monkeypatch.setattr(processor, "persist_observation", lambda _root, _observation: (
        events.append(("persist", _observation["version"]))
        or {"claim": {"event_digest": "digest-1"}}))
    monkeypatch.setattr(processor, "publish", lambda _root: events.append(("publish",)) or True)

    assert processor.run(tmp_path, limit=3) == 1
    assert events[0] == ("list", 3)
    assert events[2] == ("verify", "npm", "sample-package", True)
    assert events[3:5] == [("persist", "1.2.3"), ("publish",)]
    assert events[5] == ("processed", "npm:sample-package", "lease-1",
                         {"observed_version": "1.2.3", "evidence_digest": "digest-1",
                          "outcome": "verified_entry_point"})


def test_periodic_processor_keeps_failed_publication_retryable(monkeypatch, tmp_path):
    processor = processor_module()
    retry = []

    class Store:
        def list_pending(self, _limit):
            return [{"key": "npm:sample-package"}]

        def claim(self, key, _worker, _claimed_at, *, lease_seconds):
            assert lease_seconds == 600
            return {"key": key, "ecosystem": "npm", "package": "sample-package",
                    "lease": {"id": "lease-1"}}

        def mark_processed(self, *_args, **_kwargs):
            raise AssertionError("publication failure cannot mark a reservation processed")

        def mark_retryable(self, key, lease_id, reason, retry_after):
            retry.append((key, lease_id, reason, retry_after))

    class Discovery:
        def discover(self, *_args, **_kwargs):
            return {"outcome": "verified_entry_point", "version": "1.2.3"}

    monkeypatch.setenv("GH_TOKEN", "fixture-token")
    monkeypatch.setattr(processor, "GitHubReservationStore", lambda _token: Store())
    monkeypatch.setattr(processor, "PackageDiscovery", Discovery)
    monkeypatch.setattr(processor, "git", lambda *_args: None)
    monkeypatch.setattr(processor, "persist_observation", lambda *_args: (
        {"claim": {"event_digest": "digest-1"}}))
    monkeypatch.setattr(processor, "publish", lambda _root: (_ for _ in ()).throw(RuntimeError("push failed")))

    with pytest.raises(RuntimeError, match="push failed"):
        processor.run(tmp_path)
    assert retry[0][:3] == ("npm:sample-package", "lease-1", "push failed")


def test_central_collector_is_scheduled_and_refreshes_only_after_change():
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/central-reservation-collector.yml").read_text()
    processor = (root / "tools/process_reservations.py").read_text()
    assert "schedule:" in workflow and "cron:" in workflow
    assert "python tools/process_reservations.py" in workflow
    assert "steps.collect.outputs.changed != '0'" in workflow
    assert 'output.write(f"changed={changed}\\n")' in processor


@pytest.mark.anyio
async def test_mcp_and_cli_report_same_live_observation_and_reservation(monkeypatch, tmp_path, capsys):
    publish(tmp_path, [], {}, "2026-10-21")
    verified = {
        "contract_version": "package-discovery-v1", "outcome": "verified_entry_point",
        "verification_complete": True, "ecosystem": "npm",
        "canonical_package": "sample-package", "version": "1.2.3",
        "commands": [{"command": "sample", "entry_point": "bin/sample.js"}],
    }

    class Discovery:
        def __init__(self, *_args, **_kwargs):
            pass

        def discover(self, ecosystem, package, version=None, command=None, target=None):
            assert (ecosystem, package, version, command, target or {}) == (
                "npm", "sample-package", None, None, {})
            return dict(verified)

    reservation = {"status": "reserved", "reservation_id": "durable-reservation-id"}
    monkeypatch.setattr(cli, "PackageDiscovery", Discovery)
    monkeypatch.setattr(cli, "reserve_discovery", lambda _result: dict(reservation))
    monkeypatch.setattr(mcp_server, "reserve_discovery", lambda _result: dict(reservation))

    cli.main(["discover-package", "npm", "sample-package"])
    cli_result = json.loads(capsys.readouterr().out)
    server = mcp_server.create_server(tmp_path, dataset_root=tmp_path, discovery_engine=Discovery())
    async with create_connected_server_and_client_session(server) as session:
        mcp_result = await session.call_tool("discover_published_package", {
            "ecosystem": "npm", "package": "sample-package"})

    assert cli_result == mcp_result.structuredContent
    assert cli_result["reservation"] == reservation
