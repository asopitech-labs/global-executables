"""Central, credential-holding intake for verified package reservations.

Deploy behind an HTTPS reverse proxy. The service independently verifies every
package before its GitHub-backed queue acknowledges the request.
"""

from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .acquisition_reservation import GitHubReservationStore, ReservationError, ReservationUnavailable
from .discovery import PackageDiscovery


def create_handler(store: GitHubReservationStore, discovery: PackageDiscovery):
    class ReservationHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/reservations":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 1 or length > 2048:
                self.send_error(413)
                return
            try:
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or set(request) != {"ecosystem", "package"}:
                    raise ValueError("expected exact ecosystem and package fields")
                ecosystem, package = request["ecosystem"], request["package"]
                if not isinstance(ecosystem, str) or not isinstance(package, str):
                    raise ValueError("ecosystem and package must be strings")
                # Store validation runs before any network access.
                from .acquisition_reservation import package_key
                ecosystem, package, _ = package_key(ecosystem, package)
            except (ValueError, ReservationError):
                self._reply(400, {"status": "invalid_request"})
                return
            try:
                observation = discovery.discover(ecosystem, package, revalidate=True)
            except (OSError, ValueError, TimeoutError):
                self._reply(503, {"status": "verification_unavailable"})
                return
            if observation.get("outcome") != "verified_entry_point" or not observation.get("verification_complete"):
                self._reply(422 if observation.get("outcome") in {"not_published", "unverified"} else 503,
                            {"status": "not_reserved", "outcome": observation.get("outcome")})
                return
            try:
                evidence = json.dumps({"ecosystem": ecosystem,
                                       "package": observation["canonical_package"],
                                       "version": observation.get("version"),
                                       "artifact_digest": observation.get("artifact_digest")},
                                      sort_keys=True, separators=(",", ":")).encode()
                request_id = hashlib.sha256(evidence).hexdigest()
                reservation = store.reserve(
                    ecosystem, observation["canonical_package"], request_id,
                    datetime.now(timezone.utc).isoformat())
            except (ReservationUnavailable, ReservationError):
                self._reply(503, {"status": "reservation_unavailable"})
                return
            self._reply(202, {"status": "reserved", "reservation_id": reservation["id"]})

        def _reply(self, status: int, data: dict):
            body = json.dumps(data, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ReservationHandler


def main() -> None:
    token = os.environ["GLOBAL_EXECUTABLES_GITHUB_TOKEN"]
    host = os.environ.get("GLOBAL_EXECUTABLES_RESERVATION_HOST", "127.0.0.1")
    port = int(os.environ.get("GLOBAL_EXECUTABLES_RESERVATION_PORT", "8765"))
    ThreadingHTTPServer((host, port), create_handler(GitHubReservationStore(token), PackageDiscovery())).serve_forever()


if __name__ == "__main__":
    main()
