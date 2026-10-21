"""Submit verified live discoveries to the central durable reservation service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def reserve_discovery(observation: dict[str, Any], endpoint: str | None = None) -> dict[str, Any]:
    """Return an explicit acknowledgement; never call this for an unverified result."""
    if observation.get("outcome") != "verified_entry_point" or not observation.get("verification_complete"):
        return {"status": "not_eligible"}
    endpoint = endpoint if endpoint is not None else os.environ.get("GLOBAL_EXECUTABLES_RESERVATION_URL")
    if not endpoint:
        return {"status": "unavailable", "reason": "central reservation endpoint is not configured"}
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        return {"status": "unavailable", "reason": "central reservation endpoint must be HTTPS"}
    body = json.dumps({"ecosystem": observation["ecosystem"],
                       "package": observation["canonical_package"]}, separators=(",", ":")).encode()
    request = urllib.request.Request(endpoint, body,
                                     {"Content-Type": "application/json", "Accept": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 202:
                return {"status": "unavailable", "reason": f"central service HTTP {response.status}"}
            data = json.load(response)
            if data.get("status") != "reserved" or not isinstance(data.get("reservation_id"), str):
                return {"status": "unavailable", "reason": "invalid central acknowledgement"}
            return {"status": "reserved", "reservation_id": data["reservation_id"]}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        return {"status": "unavailable", "reason": str(error)}
