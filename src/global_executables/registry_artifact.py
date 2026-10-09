"""Resumable artifact crawlers for registry-backed executable evidence.

The crawler is deliberately budgeted.  A source becomes ``exhaustive`` only
after its catalog cursor reaches the end, every selected artifact is inspected,
and no failures remain.  A stopped or rate-limited run remains partial.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import json
import gzip
import os
import re
import signal
import socket
import struct
import sys
import tarfile
import time
import threading
import urllib.parse
import urllib.error
import urllib.request
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO, RawIOBase, TextIOWrapper
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import change_feeds, refresh_policy
from .collectors import conan_manifest_commands, crates_manifest, declared_command, record
from .model import read_jsonl
from .registry_state import load_state, save_state


USER_AGENT = "global-executables-registry-crawl/1.0 (+https://github.com/asopitech-labs/global-executables)"
CRATES_DB_DUMP = "https://static.crates.io/db-dump.tar.gz"
# crates.io asks crawlers for at most one request per second and answers 429 well before
# a CI runner's natural pace.  Only the API host is paced; its CDN mirrors are not.
HOST_MIN_INTERVAL = {"crates.io": 1.0}
# ConanCenter binaries are rebuilt without a recipe commit, which no feed announces, so
# its rotation may skip a package for less long than a feed-covered source's.
BACKOFF_MAX_DAYS_CONAN = 30
RETRY_AFTER_CAP = 60.0
# Every request gets a bounded retry budget.  The item queue below is the durable retry
# mechanism; this budget only absorbs a short request-level fault.
NETWORK_BACKOFF_CAP = 8.0
REQUEST_ATTEMPTS = 3
# Long enough that a pass stops resolving the same handful of hosts, short enough
# that a registry moving its addresses costs one interval rather than the run.
DNS_CACHE_SECONDS = 300.0
# Crate conditions no later run can resolve; retrying them forever would hold the
# source below exhaustive.
PERMANENT_CRATE_CONDITIONS = ("crate has no non-yanked version:", "crate archive has no readable Cargo.toml:",
                              "module has no latest version:", "gem is yanked")
# Answers that mean the package is gone for good.  404 is the usual one; 410 and 451 are
# how a registry reports a withdrawal or a legal takedown, and 405 is what npm returns for
# the package literally named "-", whose path collides with the registry's own API space.
PERMANENT_HTTP_CODES = (404, 405, 410, 451)
# These responses describe a temporary upstream condition, not a package verdict.
RETRYABLE_HTTP_CODES = (429, 500, 502, 503, 504)
# A failure that is neither a clean "gone" answer nor a network blip still has to stop
# somewhere, or one unreadable artifact holds a source below exhaustive forever.  Network
# errors are deliberately exempt: they self-heal, and a DNS outage spanning a few passes
# would otherwise bury packages that are perfectly fine.
FAILURE_ATTEMPT_LIMIT = 3
TRANSIENT_RETRY_LIMIT = 6
TRANSIENT_RETRY_BASE = 3600.0
TRANSIENT_RETRY_CAP = 86400.0
# Progress is persisted this often inside a pass.  State used to be written once, after
# every source finished, so an interruption discarded the cursors of sources that had
# already completed along with the work in flight.
CHECKPOINT_INTERVAL = 32
# Counting packages assumes packages are quick.  A Go module costs a request per source
# directory, so 200 of them can outlast the pass itself: one pass spent twenty-one
# minutes inspecting and was killed having written nothing, leaving the next pass to
# redo all of it.  Persist on a count or a clock, whichever comes first.
CHECKPOINT_SECONDS = 30.0
# One token bucket per paced host (see `refresh_policy.TokenBucket`): a reservation is
# claimed at once, so concurrent callers queue instead of racing past the floor.
_last_request: dict[str, refresh_policy.TokenBucket] = {}
_last_checkpoint = 0.0
_interrupted = False


def interrupted() -> bool:
    return _interrupted


def install_interrupt_handlers() -> None:
    """Turn a stop signal into a clean stop at the next checkpoint.

    A container stop or a cancelled job otherwise kills the process between
    checkpoints, which is exactly when the unsaved work is largest.
    """
    def handle(signum, frame):  # noqa: ARG001
        global _interrupted
        _interrupted = True

    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(number, handle)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass


def _no_checkpoint(buffer: list[dict[str, Any]] | None = None, **updates: Any) -> None:
    return None


def _start_checkpoint_clock() -> None:
    global _last_checkpoint
    _last_checkpoint = time.monotonic()


def _due_for_checkpoint(processed: int) -> bool:
    """True when the pass has done enough packages, or waited long enough, to persist."""
    global _last_checkpoint
    now = time.monotonic()
    if not _last_checkpoint:  # a crawler called directly starts its clock here, not at zero
        _last_checkpoint = now
    if processed % CHECKPOINT_INTERVAL == 0 or now - _last_checkpoint >= CHECKPOINT_SECONDS:
        _last_checkpoint = now
        return True
    return False


class _Unchanged(Exception):
    """Control flow: the package's recorded version is current, nothing to read."""


class RegistryCrawlError(RuntimeError):
    pass


class RegistryRequestError(RegistryCrawlError):
    """A bounded HTTP attempt set with enough context to diagnose its failure."""

    def __init__(self, url: str, category: str, attempts: int, elapsed: float,
                 detail: str, status_code: int | None = None, operation: str = "http") -> None:
        self.url = url
        self.category = category
        self.attempts = attempts
        self.elapsed = elapsed
        self.detail = detail
        self.status_code = status_code
        self.operation = operation
        super().__init__(
            f"{operation} {category} after {attempts} attempts in {elapsed:.3f}s at {url}: {detail}"
        )


def install_dns_cache() -> None:
    """Resolve each registry host once every few minutes rather than once per request.

    Every request opens its own connection, so a pass makes one name lookup per package.
    The resolver in a container is the first thing to give way under that: sixteen
    modules in flight answered "Name or service not known" 1,474 times and ran slower
    than eight did.  A crawl talks to a handful of hosts whose addresses do not move, so
    the answers are worth keeping.
    """
    if getattr(socket.getaddrinfo, "_ge_cached", False):
        return
    resolve = socket.getaddrinfo
    cache: dict[tuple[Any, ...], tuple[float, list[Any]]] = {}
    guard = threading.Lock()

    def cached(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - socket's own names
        key = (host, port, family, type, proto, flags)
        now = time.monotonic()
        with guard:
            entry = cache.get(key)
            if entry is not None and now - entry[0] < DNS_CACHE_SECONDS:
                return entry[1]
        answer = resolve(host, port, family, type, proto, flags)
        with guard:
            cache[key] = (now, answer)
        return answer

    cached._ge_cached = True  # type: ignore[attr-defined]
    socket.getaddrinfo = cached  # type: ignore[assignment]


def _throttle(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    interval = HOST_MIN_INTERVAL.get(host)
    if interval is None:
        return
    bucket = _last_request.get(host)
    if bucket is None:
        bucket = _last_request[host] = refresh_policy.TokenBucket(
            1.0 / interval, 1, clock=lambda: time.monotonic(), sleep=lambda seconds: time.sleep(seconds))
    bucket.wait()


def _retry_after_seconds(error: urllib.error.HTTPError, attempt: int) -> float:
    advertised = (error.headers or {}).get("Retry-After") if hasattr(error, "headers") else None
    try:
        return min(float(advertised), RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        return min(2.0 ** attempt, RETRY_AFTER_CAP)


def _request_category(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"http_{error.code}"
    if isinstance(error, http.client.IncompleteRead):
        return "network"
    reason = getattr(error, "reason", None)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        return "network"
    if isinstance(error, OSError):
        return "network"
    return "protocol"


def _request_is_retryable(error: Exception) -> bool:
    if isinstance(error, RegistryRequestError):
        return error.category in {"timeout", "network"} or (
            error.status_code is not None and error.status_code in RETRYABLE_HTTP_CODES
        )
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_CODES
    return isinstance(error, OSError)


def _request_bytes(url: str, timeout: int, headers: dict[str, str] | None = None,
                   method: str = "GET", attempts: int = REQUEST_ATTEMPTS) -> tuple[bytes, dict[str, Any]]:
    started = time.monotonic()
    limit = max(1, attempts)
    for attempt in range(1, limit + 1):
        _throttle(url)
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        request = urllib.request.Request(url, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = b"" if method == "HEAD" else response.read()
                response_headers = getattr(response, "headers", {})
                return body, {"url": url, "status_code": getattr(response, "status", 200),
                              "headers": dict(response_headers.items()),
                              "downloaded_bytes": len(body),
                              "duration_seconds": round(time.monotonic() - started, 3)}
        except urllib.error.HTTPError as error:
            # Back off on rate limiting and transient upstream failures rather than
            # turning a temporary registry outage into a package verdict.
            if error.code not in RETRYABLE_HTTP_CODES or attempt == limit:
                raise RegistryRequestError(url, _request_category(error), attempt,
                                           time.monotonic() - started, str(error), error.code) from error
            time.sleep(_retry_after_seconds(error, attempt))
        except (OSError, http.client.HTTPException) as error:
            if attempt == limit:
                raise RegistryRequestError(url, _request_category(error), attempt,
                                           time.monotonic() - started, str(error)) from error
            time.sleep(min(2.0 ** attempt, NETWORK_BACKOFF_CAP))
    raise RegistryCrawlError(f"unreachable retry loop: {url}")


def fetch(url: str, timeout: int = 120, attempts: int = REQUEST_ATTEMPTS) -> tuple[bytes, dict[str, Any]]:
    return _request_bytes(url, timeout, attempts=attempts)


def _open_stream(url: str, timeout: int, attempts: int = REQUEST_ATTEMPTS,
                 operation: str = "stream") -> Any:
    started = time.monotonic()
    limit = max(1, attempts)
    for attempt in range(1, limit + 1):
        _throttle(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_CODES or attempt == limit:
                raise RegistryRequestError(url, _request_category(error), attempt,
                                           time.monotonic() - started, str(error), error.code,
                                           operation=operation) from error
            time.sleep(_retry_after_seconds(error, attempt))
        except (OSError, http.client.HTTPException) as error:
            if attempt == limit:
                raise RegistryRequestError(url, _request_category(error), attempt,
                                           time.monotonic() - started, str(error),
                                           operation=operation) from error
            time.sleep(min(2.0 ** attempt, NETWORK_BACKOFF_CAP))
    raise RegistryCrawlError(f"unreachable stream loop: {url}")


def fetch_range(url: str, start: int, end: int, timeout: int = 120) -> tuple[bytes, dict[str, Any]]:
    """Fetch one inclusive byte range, refusing a server that ignores the request."""
    body, transfer = _request_bytes(url, timeout, {"Range": f"bytes={start}-{end}"})
    if transfer["status_code"] != 206:
        raise RegistryCrawlError(f"host ignored the range request: {url}")
    return body, transfer


def content_length(url: str, timeout: int = 120) -> int:
    _, transfer = _request_bytes(url, timeout, method="HEAD")
    length = transfer["headers"].get("Content-Length")
    if length is None:
        raise RegistryCrawlError(f"artifact does not advertise a length: {url}")
    return int(length)


def _zip64_values(extra: bytes, uncompressed: int, compressed: int, offset: int) -> tuple[int, int, int]:
    position = 0
    while position + 4 <= len(extra):
        tag, size = struct.unpack("<HH", extra[position:position + 4])
        body = extra[position + 4:position + 4 + size]
        if tag == 0x0001:
            cursor = 0
            for name, value in (("uncompressed", uncompressed), ("compressed", compressed), ("offset", offset)):
                if value == 0xFFFFFFFF and cursor + 8 <= len(body):
                    replacement = struct.unpack("<Q", body[cursor:cursor + 8])[0]
                    cursor += 8
                    if name == "uncompressed":
                        uncompressed = replacement
                    elif name == "compressed":
                        compressed = replacement
                    else:
                        offset = replacement
            break
        position += 4 + size
    return uncompressed, compressed, offset


class _RangeReader:
    """One connection reused for every range read of a single archive.

    Each range used to open its own TCP and TLS connection.  Deciding which directories
    of ``knative.dev/eventing`` are commands takes 884 of them, which cost five minutes
    to move 0.8MB: the bytes were never the price, the handshakes were.
    """

    def __init__(self, url: str, timeout: int) -> None:
        self.url = url
        self.timeout = timeout
        self._connection: http.client.HTTPConnection | None = None
        self._path = ""

    def _connect(self) -> tuple[http.client.HTTPConnection, str]:
        if self._connection is None:
            split = urllib.parse.urlsplit(self.url)
            factory = http.client.HTTPSConnection if split.scheme == "https" else http.client.HTTPConnection
            self._connection = factory(split.netloc, timeout=self.timeout)
            self._path = urllib.parse.urlunsplit(("", "", split.path or "/", split.query, ""))
        return self._connection, self._path

    def read(self, start: int, end: int) -> bytes:
        started = time.monotonic()
        for attempt in (1, 2, 3):  # a pooled connection can be closed by the peer
            connection, path = self._connect()
            try:
                _throttle(self.url)
                connection.request("GET", path, headers={"User-Agent": USER_AGENT,
                                                         "Range": f"bytes={start}-{end}"})
                response = connection.getresponse()
                body = response.read()  # drained in full, or the connection cannot be reused
            except (http.client.HTTPException, OSError) as error:
                self.close()
                if attempt == 3:
                    raise RegistryRequestError(self.url, _request_category(error), attempt,
                                               time.monotonic() - started, str(error),
                                               operation="nuget.range") from error
                continue
            if response.status in (301, 302, 303, 307, 308) and attempt < 3:
                location = response.getheader("Location")
                if location:
                    self.url = urllib.parse.urljoin(self.url, location)
                    self.close()
                    continue
            if response.status != 206:
                raise RegistryCrawlError(f"host ignored the range request: {self.url}")
            return body
        raise RegistryCrawlError(f"range request never settled: {self.url}")

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - closing must not mask the real error
                pass
            self._connection = None

    def __del__(self) -> None:
        self.close()


class RemoteZip:
    """Read a ZIP over HTTP ranges instead of downloading the whole artifact.

    A wheel or a Go module archive is almost entirely payload the crawler never reads.
    The file list lives in the trailing central directory and any single member can be
    inflated from its own byte range, so the evidence costs kilobytes rather than the
    whole download.
    """

    def __init__(self, url: str, timeout: int = 120) -> None:
        self.url = url
        self.timeout = timeout
        self.downloaded = 0
        self.size = content_length(url, timeout)
        self._reader = _RangeReader(url, timeout)
        self.entries = self._central_directory()

    def _range(self, start: int, end: int) -> bytes:
        body = self._reader.read(max(0, start), min(end, self.size - 1))
        self.downloaded += len(body)
        return body

    def close(self) -> None:
        self._reader.close()

    def _central_directory(self) -> dict[str, tuple[int, int, int]]:
        window = min(65557, self.size)
        tail = self._range(self.size - window, self.size - 1)
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0:
            raise RegistryCrawlError(f"no zip end-of-directory record: {self.url}")
        count, size, offset = struct.unpack("<HII", tail[marker + 10:marker + 20])
        if count == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            locator = tail.rfind(b"PK\x06\x07")
            if locator < 0:
                raise RegistryCrawlError(f"no zip64 locator: {self.url}")
            record_offset = struct.unpack("<Q", tail[locator + 8:locator + 16])[0]
            record = self._range(record_offset, record_offset + 55)
            if record[:4] != b"PK\x06\x06":
                raise RegistryCrawlError(f"no zip64 end-of-directory record: {self.url}")
            count, size, offset = struct.unpack("<QQQ", record[32:56])
        if offset >= self.size - window:
            data = tail[offset - (self.size - window):][:size]
        else:
            data = self._range(offset, offset + size - 1)
        entries: dict[str, tuple[int, int, int]] = {}
        position = 0
        while position + 46 <= len(data) and data[position:position + 4] == b"PK\x01\x02":
            method = struct.unpack("<H", data[position + 10:position + 12])[0]
            compressed, uncompressed = struct.unpack("<II", data[position + 20:position + 28])
            name_length, extra_length, comment_length = struct.unpack("<HHH", data[position + 28:position + 34])
            local = struct.unpack("<I", data[position + 42:position + 46])[0]
            name = data[position + 46:position + 46 + name_length].decode("utf-8", "replace")
            extra = data[position + 46 + name_length:position + 46 + name_length + extra_length]
            _, compressed, local = _zip64_values(extra, uncompressed, compressed, local)
            entries[name] = (method, compressed, local)
            position += 46 + name_length + extra_length + comment_length
        if not entries:
            raise RegistryCrawlError(f"zip central directory is unreadable: {self.url}")
        return entries

    @property
    def names(self) -> list[str]:
        return list(self.entries)

    def read(self, name: str) -> bytes:
        method, compressed, local = self.entries[name]
        header = self._range(local, local + 29)
        if header[:4] != b"PK\x03\x04":
            raise RegistryCrawlError(f"zip member header is unreadable: {name}")
        name_length, extra_length = struct.unpack("<HH", header[26:30])
        start = local + 30 + name_length + extra_length
        payload = self._range(start, start + compressed - 1) if compressed else b""
        if method == 0:
            return payload
        if method == 8:
            return zlib.decompress(payload, -15)
        raise RegistryCrawlError(f"unsupported zip compression {method}: {name}")


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read the crawl state in either on-disk layout (see registry_state)."""
    value = load_state(path, default)
    if value.get("version") != 1:
        raise RegistryCrawlError(f"invalid registry crawl state: {path}")
    return value


def _save_json(path: Path, value: dict[str, Any]) -> None:
    save_state(path, value)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_catalog(path: Path) -> list[str]:
    """Read a name catalog, preferring the compressed copy.

    npm's catalog alone is 85MB of package names, past what GitHub will accept
    without complaint, and these files compress to roughly a quarter.
    """
    packed = path.with_suffix(path.suffix + ".gz")
    if packed.is_file():
        with gzip.open(packed, "rt", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]
    if path.is_file():
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return []


def _catalog_digest(names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _pin_catalog(state: dict[str, Any], names: list[str]) -> None:
    digest = _catalog_digest(names)
    previous = state.get("catalog_digest")
    if previous and previous != digest:
        raise RegistryCrawlError("catalog changed without resetting its resumable cursor")
    state["catalog_digest"] = digest


def write_catalog(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = path.with_suffix(path.suffix + ".gz")
    temporary = packed.with_name(f".{packed.name}.tmp")
    try:
        with gzip.open(temporary, "wt", encoding="utf-8") as handle:
            handle.write("\n".join(names) + "\n")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(packed)
        _fsync_directory(packed.parent)
    finally:
        temporary.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def _append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _replace_package_rows(path: Path, packages: set[str], rows: list[dict[str, Any]]) -> None:
    if not packages:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] \
        if path.is_file() else []
    merged = [row for row in existing if row.get("package") not in packages]
    merged.extend(rows)
    merged.sort(key=lambda row: (row.get("command", ""), row.get("package", ""), row.get("source", "")))
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in merged))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _permanently_gone(text: str) -> bool:
    return any(f"HTTP Error {code}" in text for code in PERMANENT_HTTP_CODES)


def _network_blip(error: Exception) -> bool:
    """True for errors that say nothing about the package, only the upstream path."""
    if isinstance(error, RegistryRequestError):
        return _request_is_retryable(error)
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_CODES
    return isinstance(error, OSError)


def _failure_state(state: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, int]]:
    """Return retryable failures, permanent verdicts, and how often each has been tried."""
    failures = state.setdefault("failures", {})
    unavailable = state.setdefault("unavailable", {})
    attempts = state.setdefault("failure_attempts", {})
    retry_projects = state.setdefault("retry_projects", [])
    for key, message in list(failures.items()):
        text = str(message)
        if text == "latest release has no wheel; sdist inspection required":
            if key not in retry_projects:
                retry_projects.append(key)
            failures.pop(key, None)
        elif text == "latest release has no wheel or sdist":
            unavailable[key] = text
            failures.pop(key, None)
        elif text.startswith(PERMANENT_CRATE_CONDITIONS):
            unavailable[key] = text
            failures.pop(key, None)
        elif _permanently_gone(text):
            unavailable[key] = text
            failures.pop(key, None)
    retry_projects[:] = [key for key in retry_projects if key not in unavailable]
    for key in list(attempts):  # a key that cleared or was decided keeps no tally
        if key not in failures:
            attempts.pop(key, None)
    return failures, unavailable, attempts


def _record_failure(failures: dict[str, str], unavailable: dict[str, str], key: str, error: Exception,
                    attempts: dict[str, int] | None = None,
                    details: dict[str, dict[str, Any]] | None = None) -> None:
    message = str(error)
    gone = ((isinstance(error, urllib.error.HTTPError) and error.code in PERMANENT_HTTP_CODES)
            or (isinstance(error, RegistryRequestError)
                and error.status_code in PERMANENT_HTTP_CODES))
    if details is not None:
        details[key] = _error_details(error)
    if gone or message.startswith(PERMANENT_CRATE_CONDITIONS):
        unavailable[key] = message
        failures.pop(key, None)
        if attempts is not None:
            attempts.pop(key, None)
        return
    if attempts is not None and not _network_blip(error):
        tried = attempts.get(key, 0) + 1
        if tried >= FAILURE_ATTEMPT_LIMIT:
            # Recorded as a negative answer rather than dropped: the source may now claim
            # exhaustive, and the reason it could not read this one stays on the record.
            unavailable[key] = f"gave up after {tried} attempts: {message}"
            failures.pop(key, None)
            attempts.pop(key, None)
            return
        attempts[key] = tried
    failures[key] = message


def _error_details(error: Exception) -> dict[str, Any]:
    if isinstance(error, RegistryRequestError):
        return {"category": error.category, "url": error.url, "attempts": error.attempts,
                "elapsed_seconds": round(error.elapsed, 3), "status_code": error.status_code,
                "operation": error.operation, "message": error.detail}
    return {"category": _request_category(error), "message": str(error)}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _retry_due(metadata: dict[str, Any], key: str, now: datetime | None = None) -> bool:
    value = metadata.get(key, {})
    if not isinstance(value, dict):
        return True
    scheduled = value.get("next_attempt_at")
    if not isinstance(scheduled, str) or not scheduled:
        return True
    try:
        parsed = datetime.fromisoformat(scheduled)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (now or _utc_now()) >= parsed
    except (TypeError, ValueError):
        return True


def _schedule_transient_retry(state: dict[str, Any], key: str, error: Exception) -> bool:
    if not _network_blip(error):
        return True
    metadata = state.setdefault("retry_metadata", {})
    previous = metadata.get(key, {})
    attempts = int(previous.get("attempts", 0)) + 1 if isinstance(previous, dict) else 1
    if attempts >= TRANSIENT_RETRY_LIMIT:
        details = _error_details(error)
        state.setdefault("blocked", {})[key] = {
            "attempts": attempts, "last_error": str(error), "last_attempt_at": _utc_now().isoformat(),
            **details,
        }
        metadata.pop(key, None)
        return False
    delay = min(TRANSIENT_RETRY_BASE * (2 ** (attempts - 1)), TRANSIENT_RETRY_CAP)
    details = _error_details(error)
    metadata[key] = {"attempts": attempts, "next_attempt_at": (_utc_now() + timedelta(seconds=delay)).isoformat(),
                     "last_attempt_at": _utc_now().isoformat(), "last_error": str(error), **details}
    return True


def _retry_candidates(state: dict[str, Any], queue_name: str,
                      failures: dict[str, str], unavailable: dict[str, str]) -> tuple[list[str], int]:
    queue = state.setdefault(queue_name, [])
    blocked = state.setdefault("blocked", {})
    queue[:] = list(dict.fromkeys(name for name in queue if name not in unavailable and name not in blocked))
    for name in failures:
        if name not in queue:
            queue.append(name)
    metadata = state.setdefault("retry_metadata", {})
    now = _utc_now()
    due = [name for name in queue if _retry_due(metadata, name, now)]
    return due, len(queue) - len(due)


def _next_refresh_index(items: list[str], start: int, excluded: set[str]) -> int | None:
    if not items:
        return None
    for offset in range(len(items)):
        index = (start + offset) % len(items)
        if items[index] not in excluded:
            return index
    return None


def _retry_waiting_count(state: dict[str, Any], queue_name: str) -> int:
    metadata = state.setdefault("retry_metadata", {})
    return sum(1 for key in state.get(queue_name, []) if not _retry_due(metadata, key))


def _drop_retry_key(state: dict[str, Any], key: str) -> None:
    for queue_name in ("retry_tools", "retry_recipes"):
        queue = state.get(queue_name)
        if isinstance(queue, list):
            queue[:] = [item for item in queue if item != key]


def _failure_diagnostics(state: dict[str, Any], failures: dict[str, str], queue_name: str) -> dict[str, Any]:
    details = state.setdefault("failure_details", {})
    result = {key: details[key] for key in set(failures) | set(state.get(queue_name, [])) if key in details}
    result.update({key: value for key, value in state.get("blocked", {}).items()})
    return result


def _clear_failure(state: dict[str, Any], failures: dict[str, str], attempts: dict[str, int], key: str) -> None:
    failures.pop(key, None)
    attempts.pop(key, None)
    state.setdefault("failure_details", {}).pop(key, None)
    state.setdefault("retry_metadata", {}).pop(key, None)
    state.setdefault("blocked", {}).pop(key, None)
    _drop_retry_key(state, key)


def _postgres_array(value: str) -> list[str]:
    """Parse a Postgres ``text[]`` literal the way the database dump writes it."""
    value = value.strip()
    if not value.startswith("{") or not value.endswith("}"):
        return []
    body = value[1:-1]
    if not body:
        return []
    items: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return [item.strip() for item in items if item.strip()]


class _CountingReader:
    """Count the compressed bytes a streaming tar actually pulls off the socket."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.stream.read(size)
        self.count += len(chunk)
        return chunk


class _UnseekableMember(RawIOBase):
    """Present a streamed tar member as a plain readable file.

    Members of a ``r|gz`` tar cannot answer ``seekable()``, which TextIOWrapper asks for.
    """

    def __init__(self, handle: Any) -> None:
        self.handle = handle

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, buffer) -> int:  # type: ignore[override]
        chunk = self.handle.read(len(buffer))
        buffer[:len(chunk)] = chunk
        return len(chunk)


def _dump_rows(archive: tarfile.TarFile, member: tarfile.TarInfo):
    handle = archive.extractfile(member)
    if handle is None:
        return
    yield from csv.DictReader(TextIOWrapper(_UnseekableMember(handle), encoding="utf-8", newline=""))


# crates.io republishes its dump daily: soft TTL one day, hard TTL two weeks.
CRATES_DUMP_SOFT_DAYS = 2
CRATES_DUMP_HARD_DAYS = 14


def _dump_ttl(last_modified: str, now: datetime) -> str:
    """Soft/hard TTL verdict (`refresh_policy.classify`) of the dump a run is reporting."""
    try:
        published = parsedate_to_datetime(last_modified)
    except (TypeError, ValueError):
        return "unknown"
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return refresh_policy.classify((now - published).days, CRATES_DUMP_SOFT_DAYS, CRATES_DUMP_HARD_DAYS)


def _crawl_crates(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                  checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    """Read every crate's declared binaries from the crates.io database dump.

    crates.io publishes ``bin_names`` per version, so an executable name never
    required downloading a ``.crate`` at all.  The dump carries the whole registry
    in one request and is the bulk access route crates.io points crawlers to when
    they hit the API's pagination limit.
    """
    for legacy in ("page", "seek", "page_offset", "skip", "cursor", "retry_crates"):
        state.pop(legacy, None)
    # The dump is a whole-registry snapshot, so a verdict the retired per-crate path
    # left behind is not evidence about it.  One stale IncompleteRead was holding a
    # complete crates.io at partial with no way to ever clear.
    state.pop("failures", None)
    state.pop("unavailable", None)
    failures, unavailable, attempts = _failure_state(state)
    # The dump is republished daily and the observations replace rather than extend, so
    # re-reading an unchanged one costs 1.7GB to rewrite the same file.
    _, head_transfer = _request_bytes(CRATES_DB_DUMP, timeout, method="HEAD")
    published = head_transfer["headers"].get("Last-Modified", "")
    size = int(head_transfer["headers"].get("Content-Length") or 0)
    published_header = published
    if published and published == state.get("dump_last_modified") and output.is_file():
        collected = sum(1 for line in output.open(encoding="utf-8") if line.strip())
        complete = not failures
        return {"dump_timestamp": state.get("dump_timestamp"), "dump_last_modified": published,
                "catalog_size": state.get("catalog_size", 0), "cursor": state.get("catalog_size", 0),
                "processed": 0, "records": collected, "downloaded_bytes": 0, "dump_bytes": size,
                "failures": len(failures), "unavailable": len(unavailable),
                "budget_exhausted": False, "complete": complete, "unchanged": True,
                "ttl": _dump_ttl(published, _utc_now()),
                "coverage_kind": "exhaustive" if complete else "partial"}
    if size > byte_budget:
        return {"records": 0, "processed": 0, "downloaded_bytes": 0, "dump_bytes": size,
                "failures": len(failures), "unavailable": len(unavailable),
                "budget_exhausted": True, "complete": False, "coverage_kind": "partial"}

    last_modified = published_header
    crates: dict[str, tuple[str, str | None]] = {}
    defaults: dict[str, str] = {}
    binaries: dict[str, tuple[str, list[str]]] = {}
    published = ""
    csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))
    started = time.monotonic()
    response = _open_stream(CRATES_DB_DUMP, timeout, operation="crates.dump")
    try:
        with response:
            counter = _CountingReader(response)
            with tarfile.open(fileobj=counter, mode="r|gz") as archive:  # type: ignore[arg-type]
                for member in archive:
                    name = PurePosixPath(member.name).name
                    if name == "metadata.json":
                        handle = archive.extractfile(member)
                        if handle is not None:
                            published = json.loads(handle.read()).get("timestamp", "")
                    elif name == "crates.csv":
                        for row in _dump_rows(archive, member):
                            crates[row["id"]] = (row["name"], row.get("repository") or None)
                    elif name == "default_versions.csv":
                        for row in _dump_rows(archive, member):
                            defaults[row["crate_id"]] = row["version_id"]
                    elif name == "versions.csv":
                        for row in _dump_rows(archive, member):
                            if row.get("yanked") in ("t", "true", "True"):
                                continue
                            commands = _postgres_array(row.get("bin_names") or "")
                            if commands:
                                binaries[row["id"]] = (row["num"], commands)
            downloaded = counter.count
    except (OSError, http.client.HTTPException) as error:
        raise RegistryRequestError(CRATES_DB_DUMP, _request_category(error), 1,
                                   time.monotonic() - started, str(error),
                                   operation="crates.dump") from error

    rows: list[dict[str, Any]] = []
    with_binaries = 0
    for crate_id, (name, repository) in sorted(crates.items(), key=lambda item: item[1][0]):
        entry = binaries.get(defaults.get(crate_id, ""))
        if entry is None:
            continue
        with_binaries += 1
        version, commands = entry
        for command in sorted(set(commands)):
            rows.append(record(command, "crates", name, version, repository, CRATES_DB_DUMP,
                               source_type="language_package", language="rust",
                               registry="crates.io", latest_version=version))
    if not crates:
        raise RegistryCrawlError("crates.io database dump carried no crates table")

    # The dump is a whole-registry snapshot, so the observations replace rather than
    # extend what an earlier snapshot wrote.
    _write_text_atomic(output, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    collected = len(rows)
    state["dump_timestamp"] = published
    state["dump_last_modified"] = last_modified
    state["catalog_size"] = len(crates)
    state["complete"] = True
    complete = not failures
    return {"dump_timestamp": published, "catalog_size": len(crates), "cursor": len(crates),
            "crates_with_binaries": with_binaries, "processed": len(crates), "records": collected,
            "downloaded_bytes": downloaded, "dump_bytes": size,
            "failures": len(failures), "unavailable": len(unavailable),
            "budget_exhausted": False, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}

NUGET_SEARCH = "https://azuresearch-usnc.nuget.org/query"
NUGET_FLAT = "https://api.nuget.org/v3-flatcontainer"
NUGET_PAGE = 1000
# One query stops paging at 4,000 of the 9,190 tools it advertises, but the cap is per
# query: partitioning the term space reaches every one of them.
NUGET_TERMS = ("", *"abcdefghijklmnopqrstuvwxyz", *"0123456789")
TOOL_COMMAND = re.compile(r"<Command\b[^>]*\bName\s*=\s*\"([^\"]+)\"", re.I)


def _nuget_tool_commands(url: str, timeout: int) -> tuple[list[str], int]:
    """Read a .NET tool's declared commands from DotnetToolSettings.xml.

    A tool package states its commands in that one small file, and a .nupkg is a ZIP,
    so the declaration costs a range read rather than the whole package.
    """
    try:
        archive = RemoteZip(url, timeout)
        members = [name for name in archive.names if name.rsplit("/", 1)[-1].lower() == "dotnettoolsettings.xml"]
        text = archive.read(members[0]).decode("utf-8", "replace") if members else ""
        return TOOL_COMMAND.findall(text), archive.downloaded
    except RegistryRequestError:
        raise
    except (RegistryCrawlError, urllib.error.HTTPError, OSError, struct.error, zlib.error, KeyError):
        body, transfer = _fetch_stage(url, timeout, "nuget.package")
        with zipfile.ZipFile(BytesIO(body)) as whole:
            members = [name for name in whole.namelist() if name.rsplit("/", 1)[-1].lower() == "dotnettoolsettings.xml"]
            text = whole.read(members[0]).decode("utf-8", "replace") if members else ""
        return TOOL_COMMAND.findall(text), transfer["downloaded_bytes"]


def _fetch_stage(url: str, timeout: int, operation: str) -> tuple[bytes, dict[str, Any]]:
    try:
        return fetch(url, timeout)
    except RegistryRequestError as error:
        if error.operation == operation:
            raise
        raise RegistryRequestError(error.url, error.category, error.attempts, error.elapsed,
                                   error.detail, error.status_code, operation=operation) from error


def _prepare_checks(state: dict[str, Any], output: Path, key_of: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """Return the source's `checked` map, seeded from stored rows on first use.

    State written by older extraction logic cannot vouch for the stored rows, so a lower
    `extraction_revision` drops the checks and every package is read once more.
    """
    stored = state.get("extraction_revision")
    outdated = isinstance(stored, int) and stored < refresh_policy.EXTRACTION_REVISION
    if outdated:
        state.pop(refresh_policy.CHECKED_FIELD, None)
    state["extraction_revision"] = refresh_policy.EXTRACTION_REVISION
    checked = state.setdefault(refresh_policy.CHECKED_FIELD, {})
    # Rows written by older logic must not seed checks that would then vouch for them.
    if not checked and not outdated and output.is_file():
        refresh_policy.seed_from_rows(checked, read_jsonl(output), key_of)
    return checked


def _crawl_nuget(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                 checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    """Inspect NuGet's .NET tool packages, the only NuGet packages that ship commands."""
    catalog_file = Path(state.setdefault("tools_file", "data/production/nuget-tools.txt"))
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    existing_tools = read_catalog(catalog_file)
    catalog_grew = False
    state["catalog_grew"] = False
    if not existing_tools or state.get("catalog_truncated"):
        identifiers: set[str] = set(existing_tools)
        previous_size = len(identifiers)
        advertised = int(state.get("catalog_advertised") or 0)
        catalog_fetch_failed = False
        for term in NUGET_TERMS:
            skip = 0
            while True:
                query = urllib.parse.urlencode({"q": term, "packageType": "DotnetTool",
                                                "take": NUGET_PAGE, "skip": skip, "prerelease": "true"})
                try:
                    body, _ = _fetch_stage(f"{NUGET_SEARCH}?{query}", timeout, "nuget.catalog")
                except Exception:
                    # One truncated response must not discard every term already collected.
                    catalog_fetch_failed = True
                    break
                value = json.loads(body)
                advertised = max(advertised, int(value.get("totalHits") or 0))
                page = value.get("data", [])
                if not page:
                    break
                identifiers.update(item["id"] for item in page if item.get("id"))
                skip += len(page)
            if interrupted():
                break
        state["catalog_advertised"] = advertised
        state["catalog_truncated"] = (catalog_fetch_failed or
                                       (bool(advertised) and len(identifiers) < advertised) or
                                       interrupted())
        catalog_grew = len(identifiers) > previous_size
        state["catalog_grew"] = catalog_grew
        if catalog_grew:
            # A changed ordered catalogue invalidates its cursor. Replaying is safe:
            # observation replacement is keyed by package.
            state.pop("catalog_digest", None)
            state["cursor"] = 0
            state["refresh_cursor"] = 0
        ordered_identifiers = sorted(identifiers)
        _pin_catalog(state, ordered_identifiers)
        write_catalog(catalog_file, ordered_identifiers)
        # Building the catalogue is the expensive part of the pass; persist its verdict
        # before inspecting anything, so a later failure cannot discard it.
        checkpoint()
    tools = read_catalog(catalog_file)
    _pin_catalog(state, tools)
    if state.get("catalog_advertised") is None and tools:
        # A catalog built before this check existed carries no verdict, and without one
        # the source would present a sample of the tool list as the whole of it.
        query = urllib.parse.urlencode({"q": "", "packageType": "DotnetTool", "take": 1,
                                        "skip": 0, "prerelease": "false"})
        try:
            body, _ = _fetch_stage(f"{NUGET_SEARCH}?{query}", timeout, "nuget.catalog")
            advertised = int(json.loads(body).get("totalHits") or 0)
        except Exception:
            advertised = 0
        state["catalog_advertised"] = advertised
        state["catalog_truncated"] = bool(advertised) and len(tools) < advertised
    checked = _prepare_checks(state, output, lambda row: row.get("package"))
    day = refresh_policy.today(_utc_now())
    max_days = refresh_policy.BACKOFF_MAX_DAYS_PLAIN  # NuGet has no polled feed (docs/OPERATIONS.md)
    cursor = int(state.get("cursor", 0)); refresh_cursor = int(state.get("refresh_cursor", 0))
    if refresh_cursor >= len(tools):
        refresh_cursor = 0
    refresh_enabled = cursor >= len(tools)
    processed = 0; refreshed = 0; downloaded = 0; unchanged = 0; skipped = 0
    failures, unavailable, attempts = _failure_state(state)
    # Reaching the end of the catalogue is not the end of the work: a tool that failed
    # on a DNS blip has no other way back, and six of them held a finished NuGet at
    # partial with nothing left to walk.
    retry_tools = state.setdefault("retry_tools", [])
    retry_candidates, retry_waiting = _retry_candidates(state, "retry_tools", failures, unavailable)
    refresh_excluded = set(retry_tools)
    refresh_remaining = sum(name not in refresh_excluded for name in tools) if refresh_enabled else 0
    refresh_index = _next_refresh_index(tools, refresh_cursor, refresh_excluded)
    rows: list[dict[str, Any]] = []; replacement_rows: list[dict[str, Any]] = []
    replaced_packages: set[str] = set(); collected = 0; budget_exhausted = False
    while (retry_candidates or cursor < len(tools) or
           (refresh_enabled and refresh_remaining > 0)) and processed < budget:
        retrying = bool(retry_candidates)
        refreshing = not retrying and cursor >= len(tools)
        if refreshing:
            if refresh_index is None:
                break
            package = tools[refresh_index]
            if not refresh_policy.is_due(checked, package, day, max_days, int(state.get("due_floor", 0))):
                # Not due: advance the rotation without a request or a unit of budget.
                skipped += 1
                refresh_remaining -= 1
                refresh_cursor = (refresh_index + 1) % len(tools)
                refresh_index = _next_refresh_index(tools, refresh_cursor, refresh_excluded)
                continue
        else:
            package = retry_candidates.pop(0) if retrying else tools[cursor]
        lowered = urllib.parse.quote(package.lower(), safe="")
        try:
            body, _ = _fetch_stage(f"{NUGET_FLAT}/{lowered}/index.json", timeout, "nuget.index")
            versions = json.loads(body).get("versions", [])
            if not versions:
                raise RegistryCrawlError(f"tool has no published version: {package}")
            version = versions[-1]
            if refresh_policy.known_version(checked, package) == version:
                # The tool's latest version is the one already recorded: its stored rows
                # stay and the package download (megabytes) is skipped.
                unchanged += 1
                refresh_policy.record_check(checked, package, version, day)
                _clear_failure(state, failures, attempts, package)
            else:
                url = f"{NUGET_FLAT}/{lowered}/{urllib.parse.quote(version, safe='')}/{lowered}.{urllib.parse.quote(version, safe='')}.nupkg"
                commands, spent = _nuget_tool_commands(url, timeout)
                downloaded += spent
                if downloaded > byte_budget:
                    budget_exhausted = True
                    break
                package_rows = [record(command, "nuget", package, version, None, url,
                                       source_type="language_package", language="dotnet",
                                       registry="nuget", latest_version=version)
                                for command in sorted(set(commands))]
                rows.extend(package_rows)
                replacement_rows.extend(package_rows)
                replaced_packages.add(package)
                refresh_policy.record_check(checked, package, version, day)
                _clear_failure(state, failures, attempts, package)
        except Exception as error:
            _record_failure(failures, unavailable, package, error, attempts,
                            state.setdefault("failure_details", {}))
            if package in unavailable:
                refresh_policy.forget(checked, package)
            if package in failures and not _schedule_transient_retry(state, package, error):
                failures.pop(package, None)
            if package not in failures:
                _drop_retry_key(state, package)
            if package in failures and package not in retry_tools:
                retry_tools.append(package)  # queued for the next run, not this one
        if refreshing:
            refreshed += 1
            refresh_remaining -= 1
            refresh_cursor = (refresh_index + 1) % len(tools)
            refresh_index = _next_refresh_index(tools, refresh_cursor, refresh_excluded)
        elif not retrying:
            cursor += 1
        processed += 1
        if _due_for_checkpoint(processed) or interrupted():
            collected += len(rows)
            _replace_package_rows(output, replaced_packages, replacement_rows)
            rows.clear(); replacement_rows.clear(); replaced_packages.clear()
            checkpoint(cursor=cursor, refresh_cursor=refresh_cursor)
        if interrupted():
            break
    state["cursor"] = cursor
    state["catalog_size"] = len(tools)
    state["catalog_complete"] = not bool(state.get("catalog_truncated"))
    state["refresh_cursor"] = refresh_cursor
    collected += len(rows)
    _replace_package_rows(output, replaced_packages, replacement_rows)
    truncated = bool(state.get("catalog_truncated"))
    retry_waiting = _retry_waiting_count(state, "retry_tools")
    complete = (cursor >= len(tools) and not failures and not retry_tools
                and not state.get("blocked") and not truncated)
    report = {"cursor": cursor, "refresh_cursor": refresh_cursor, "refreshed": refreshed,
              "unchanged": unchanged, "skipped_not_due": skipped, "checked": len(checked),
              "ttl": refresh_policy.ttl_summary(checked, day, max_days),
              "catalog_size": len(tools), "processed": processed,
              "records": collected, "downloaded_bytes": downloaded, "failures": len(failures),
              "unavailable": len(unavailable), "budget_exhausted": budget_exhausted,
              "catalog_truncated": truncated, "catalog_advertised": state.get("catalog_advertised"),
              "catalog_grew": catalog_grew,
              "retry_pending": len(retry_tools), "retry_waiting": retry_waiting,
              "blocked": len(state.get("blocked", {})),
              "failure_details": _failure_diagnostics(state, failures, "retry_tools"),
              "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}
    report["status"] = "success" if complete else "partial"
    if truncated:
        report["note"] = "NuGet search paging stops short of totalHits; this is a sample of .NET tools"
    return report


CONAN_INDEX = "https://codeload.github.com/conan-io/conan-center-index/tar.gz/refs/heads/master"
CONAN_REMOTE = "https://center2.conan.io/v2/conans"
# A command set barely differs across build configurations, so one is inspected, and
# the manifest URL records which.  Linux first because its binaries carry no suffix.
CONAN_PREFERRED_OS = ("Linux", "Macos", "Windows")
# config.yml quotes a version with double quotes, single quotes (`fff`: `'1.1'`), or
# not at all; the quote is YAML syntax and never part of the reference.
CONAN_CONFIG_VERSION = re.compile(r"""^\s{2}(["']?)([^"'\s:]+)\1:\s*$""", re.M)
# When the newest declared version has no built package (it is not yet published, so the
# remote answers 404, or nobody has built it), older published versions are tried, at
# most this many, so a recipe such as gcc/16.1.0 still yields gcc 15.2.0's commands.
CONAN_FALLBACK_VERSIONS = 3
# conan-center-index moves daily.  The walk reads the catalogue once and then refreshes
# the recipes it lists, so without a re-read new recipes and new versions never reach
# the index.  Once the walk has reached the end of the catalogue, a catalogue older than
# this is read again and rolled over (see `_roll_conan_catalog`).
CONAN_CATALOG_MAX_AGE = timedelta(days=7)
# Per-recipe bookkeeping keyed by reference.  A reference that leaves the catalogue
# leaves these too, or a superseded version would hold the source short of complete.
CONAN_REFERENCE_STATE = ("failures", "unavailable", "uninspected", "failure_attempts",
                         "failure_details", "retry_metadata", "blocked")


def _version_key(value: str) -> tuple[tuple[int, Any], ...]:
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in re.split(r"[._-]", value) if part)


def _conan_catalog(timeout: int) -> tuple[list[str], int]:
    """Read every ConanCenter recipe and its newest version from the index snapshot.

    The index is one repository tarball, so the whole declared population costs a
    single request and the catalogue is finite rather than paged.
    """
    body, transfer = _fetch_stage(CONAN_INDEX, timeout, "conan.catalog")
    versions: dict[str, list[str]] = {}
    with tarfile.open(fileobj=BytesIO(body), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith("/config.yml"):
                continue
            parts = member.name.split("/")
            if len(parts) != 4 or parts[1] != "recipes":
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            declared = [match.group(2) for match in
                        CONAN_CONFIG_VERSION.finditer(handle.read().decode("utf-8", "replace"))]
            if declared:
                versions[parts[2]] = declared
    if not versions:
        raise RegistryCrawlError("conan-center-index snapshot carried no recipe versions")
    return ([f"{name}/{max(found, key=_version_key)}" for name, found in sorted(versions.items())],
            transfer["downloaded_bytes"])


def _not_found(error: Exception) -> bool:
    return ((isinstance(error, urllib.error.HTTPError) and error.code == 404)
            or (isinstance(error, RegistryRequestError) and error.status_code == 404))


def _conan_published_versions(name: str, timeout: int) -> tuple[list[str], int]:
    """List the versions of one recipe the remote publishes, newest first."""
    body, transfer = _fetch_stage(f"{CONAN_REMOTE}/search?q={urllib.parse.quote(name, safe='')}",
                                  timeout, "conan.version_search")
    versions = set()
    for found in json.loads(body).get("results") or []:
        base, _, user_channel = str(found).partition("@")
        found_name, _, found_version = base.partition("/")
        if found_name == name and found_version and user_channel in ("", "_/_"):
            versions.add(found_version)
    return sorted(versions, key=_version_key, reverse=True), transfer["downloaded_bytes"]


def _conan_package_commands(reference: str, timeout: int) -> tuple[list[str], str, int, str]:
    """Inspect one ConanCenter recipe for the commands it installs.

    Returns the commands, the manifest URL ("" when no built package was found), the
    bytes downloaded, and the version actually inspected.  The newest declared version
    is tried first; when the remote does not publish it (404) or has no built package
    for it, up to `CONAN_FALLBACK_VERSIONS` older published versions are tried.  A 404
    that no fallback resolves is raised, so the recipe is recorded as unavailable.
    """
    name, _, version = reference.partition("/")
    if not name or not version:
        raise RegistryCrawlError(f"malformed recipe reference: {reference}")
    missing: Exception | None = None
    try:
        commands, manifest_url, downloaded = _conan_version_commands(name, version, timeout)
    except Exception as error:
        if not _not_found(error):
            raise
        commands, manifest_url, downloaded, missing = [], "", 0, error
    if manifest_url:
        return commands, manifest_url, downloaded, version
    try:
        published, spent = _conan_published_versions(name, timeout)
        downloaded += spent
    except Exception as error:
        if not _not_found(error):
            raise
        published = []
    for candidate in [found for found in published if found != version][:CONAN_FALLBACK_VERSIONS]:
        try:
            commands, manifest_url, spent = _conan_version_commands(name, candidate, timeout)
        except Exception as error:
            if not _not_found(error):
                raise
            continue
        downloaded += spent
        if manifest_url:
            return commands, manifest_url, downloaded, candidate
    if missing is not None:
        raise missing
    return [], "", downloaded, version


# The newest recipe revision each full inspection saw, read back by the crawl to record a
# check without a second request.
_conan_revisions: dict[tuple[str, str], str] = {}


def _conan_recipe_revision(name: str, version: str, timeout: int) -> tuple[str, int]:
    """The newest published recipe revision of one version: the first request of an inspection."""
    quoted = f"{urllib.parse.quote(name, safe='')}/{urllib.parse.quote(version, safe='')}"
    body, transfer = _fetch_stage(f"{CONAN_REMOTE}/{quoted}/_/_/revisions", timeout, "conan.revisions")
    revisions = json.loads(body).get("revisions") or []
    if not revisions:
        raise RegistryCrawlError(f"recipe has no published revision: {name}/{version}")
    return max(revisions, key=lambda item: str(item.get("time", "")))["revision"], transfer["downloaded_bytes"]


def _conan_version_commands(name: str, version: str, timeout: int) -> tuple[list[str], str, int]:
    """Inspect one ConanCenter binary package for the commands it installs.

    A conan recipe never declares its executables, so the evidence is the built
    package's own file list.  `conanmanifest.txt` carries that list as a small text
    file, which is far less than the package archive it describes.
    """
    quoted = f"{urllib.parse.quote(name, safe='')}/{urllib.parse.quote(version, safe='')}"
    revisions_url = f"{CONAN_REMOTE}/{quoted}/_/_/revisions"
    body, transfer = _fetch_stage(revisions_url, timeout, "conan.revisions")
    downloaded = transfer["downloaded_bytes"]
    revisions = json.loads(body).get("revisions") or []
    if not revisions:
        raise RegistryCrawlError(f"recipe has no published revision: {reference}")
    recipe_revision = max(revisions, key=lambda item: str(item.get("time", "")))["revision"]
    _conan_revisions[(name, version)] = recipe_revision
    revision_url = f"{revisions_url}/{recipe_revision}"
    body, transfer = _fetch_stage(f"{revision_url}/search", timeout, "conan.package_search")
    downloaded += transfer["downloaded_bytes"]
    packages = json.loads(body)
    if not isinstance(packages, dict) or not packages:
        # A recipe nobody has built yet states nothing about installed files.
        return [], "", downloaded
    def preference(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        declared = str((item[1].get("settings") or {}).get("os", ""))
        rank = CONAN_PREFERRED_OS.index(declared) if declared in CONAN_PREFERRED_OS else len(CONAN_PREFERRED_OS)
        return rank, item[0]
    package_id = min(packages.items(), key=preference)[0]
    body, transfer = _fetch_stage(f"{revision_url}/packages/{package_id}/revisions", timeout,
                                  "conan.package_revisions")
    downloaded += transfer["downloaded_bytes"]
    package_revisions = json.loads(body).get("revisions") or []
    if not package_revisions:
        return [], "", downloaded
    package_revision = max(package_revisions, key=lambda item: str(item.get("time", "")))["revision"]
    manifest_url = (f"{revision_url}/packages/{package_id}/revisions/{package_revision}"
                    "/files/conanmanifest.txt")
    body, transfer = _fetch_stage(manifest_url, timeout, "conan.manifest")
    downloaded += transfer["downloaded_bytes"]
    return conan_manifest_commands(body.decode("utf-8", "replace")), manifest_url, downloaded


def _conan_catalog_stale(state: dict[str, Any], now: datetime) -> bool:
    fetched = state.get("catalog_fetched_at")
    if not isinstance(fetched, str) or not fetched:
        return True  # a catalogue of unknown age is re-read once rather than trusted forever
    try:
        parsed = datetime.fromisoformat(fetched)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return now - parsed >= CONAN_CATALOG_MAX_AGE


def _roll_conan_catalog(state: dict[str, Any], previous: list[str], current: list[str]) -> dict[str, int]:
    """Adopt a re-read recipe catalogue without restarting the walk.

    Only called once the walk has reached the end of `previous`, so every reference the
    two catalogues share has been inspected and stays on the refresh rotation.  A new
    recipe or a new version is queued in `catalog_pending` and inspected ahead of the
    refresh rotation; the cursor moves to the end of the new catalogue so the walk is
    still complete and the continuation keyed on the cursor does not restart it.
    Bookkeeping for references that left the catalogue is dropped, while their
    published rows stay until a newer version of the same package replaces them.
    """
    keep = set(current)
    known = set(previous)
    pending = [reference for reference in state.get("catalog_pending", []) if reference in keep]
    queued = set(pending)
    added = [reference for reference in current if reference not in known and reference not in queued]
    state["catalog_pending"] = pending + added
    for key in CONAN_REFERENCE_STATE:
        value = state.get(key)
        if isinstance(value, dict):
            for reference in [reference for reference in value if reference not in keep]:
                value.pop(reference, None)
    retry = state.get("retry_recipes")
    if isinstance(retry, list):
        retry[:] = [reference for reference in retry if reference in keep]
    state["catalog_digest"] = _catalog_digest(current)
    state["cursor"] = len(current)
    state["catalog_size"] = len(current)
    if int(state.get("refresh_cursor", 0)) >= len(current):
        state["refresh_cursor"] = 0
    return {"added": len(added), "removed": len(known - keep), "size": len(current)}


# The feed's HTTP transport; tests replace it so no test reaches api.github.com.
_conan_feed_request = _request_bytes


def _conan_feed(state: dict[str, Any], recipes: list[str], timeout: int,
                checkpoint: Callable[..., None], day: int) -> dict[str, Any]:
    """Queue the recipes conan-center-index changed since the stored commit.

    The cursor and the queue (`catalog_pending`) live in the same state document and are
    written by one checkpoint, so the cursor never gets ahead of the queued work.  A
    failed poll leaves both untouched: the next run replays it.
    """
    report: dict[str, Any] = {"events": 0, "enqueued": 0, "requests": 0, "downloaded_bytes": 0,
                              "resync": False, "error": ""}
    try:
        page = change_feeds.conan_poll(_conan_feed_request, str(state.get("feed_cursor") or ""), timeout)
    except Exception as error:
        report["error"] = str(error)
        return report
    report.update(requests=page.requests, downloaded_bytes=page.downloaded_bytes,
                  events=len(page.names), resync=page.resync)
    pending = state.setdefault("catalog_pending", [])
    queued = set(pending)
    by_name = {reference.partition("/")[0]: reference for reference in recipes}
    for name in sorted(page.names):
        reference = by_name.get(name)
        if reference and reference not in queued:
            pending.append(reference)
            queued.add(reference)
            report["enqueued"] += 1
    if page.catalog_changed:
        # A recipe or version list changed: read the catalogue again instead of waiting a week.
        state["catalog_fetched_at"] = ""
    if page.resync:
        state["due_floor"] = day
    state["feed_cursor"] = page.cursor
    checkpoint()
    return report


def _crawl_conan(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                 checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    """Inspect ConanCenter's built packages for the commands they install."""
    catalog_file = Path(state.setdefault("recipes_file", "data/production/conan-recipes.txt"))
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    catalog_refresh: dict[str, Any] | None = None
    recipes = read_catalog(catalog_file)
    checked = _prepare_checks(state, output, lambda row: None)  # a reference has no row to seed from
    day = refresh_policy.today(_utc_now())
    feed_report = _conan_feed(state, recipes, timeout, checkpoint, day) if recipes else {}
    if not recipes:
        references, spent = _conan_catalog(timeout)
        downloaded += spent
        _pin_catalog(state, references)
        write_catalog(catalog_file, references)
        state["catalog_complete"] = True
        state["catalog_fetched_at"] = _utc_now().isoformat()
        checkpoint()  # the catalogue is the expensive part; a later failure must not lose it
        recipes = references
    elif state.get("catalog_digest") not in (None, _catalog_digest(recipes)):
        # The catalogue on file is not the one the cursor counted, so the cursor's
        # positions mean nothing in it: walk the catalogue on file from the start.
        state.update(catalog_digest=_catalog_digest(recipes), cursor=0, refresh_cursor=0,
                     catalog_pending=[])
        catalog_refresh = {"status": "reset", "reason": "catalogue on file differs from the walked one"}
    elif int(state.get("cursor", 0)) >= len(recipes) and _conan_catalog_stale(state, _utc_now()):
        try:
            references, spent = _conan_catalog(timeout)
        except (RegistryCrawlError, OSError, tarfile.TarError) as error:
            # A failed re-read keeps the catalogue already walked; the next run tries again.
            catalog_refresh = {"status": "failed", "error": str(error)}
        else:
            downloaded += spent
            catalog_refresh = {"status": "refreshed", **_roll_conan_catalog(state, recipes, references)}
            write_catalog(catalog_file, references)
            state["catalog_fetched_at"] = _utc_now().isoformat()
            checkpoint()
            recipes = references
    _pin_catalog(state, recipes)
    cursor = int(state.get("cursor", 0))
    refresh_cursor = int(state.get("refresh_cursor", 0))
    if refresh_cursor >= len(recipes):
        refresh_cursor = 0
    refresh_enabled = cursor >= len(recipes)
    processed = refreshed = collected = unchanged = skipped = 0
    budget_exhausted = False
    failures, unavailable, attempts = _failure_state(state)
    # A recipe nobody has built publishes no file list, so it is neither a failure nor
    # evidence of absence.  Tracking it keeps the source honestly short of exhaustive.
    uninspected = state.setdefault("uninspected", {})
    retry_recipes = state.setdefault("retry_recipes", [])
    retry_candidates, retry_waiting = _retry_candidates(state, "retry_recipes", failures, unavailable)
    pending = state.setdefault("catalog_pending", [])
    refresh_excluded = set(retry_recipes) | set(pending)
    refresh_remaining = sum(reference not in refresh_excluded for reference in recipes) if refresh_enabled else 0
    refresh_index = _next_refresh_index(recipes, refresh_cursor, refresh_excluded)
    rows: list[dict[str, Any]] = []
    replacement_rows: list[dict[str, Any]] = []
    replaced_packages: set[str] = set()
    while (retry_candidates or pending or cursor < len(recipes) or
           (refresh_enabled and refresh_remaining > 0)) and processed < budget:
        retrying = bool(retry_candidates)
        from_pending = not retrying and bool(pending)
        refreshing = not retrying and not from_pending and cursor >= len(recipes)
        if refreshing:
            if refresh_index is None:
                break
            reference = recipes[refresh_index]
            if not refresh_policy.is_due(checked, reference, day, BACKOFF_MAX_DAYS_CONAN,
                                         int(state.get("due_floor", 0))):
                skipped += 1
                refresh_remaining -= 1
                refresh_cursor = (refresh_index + 1) % len(recipes)
                refresh_index = _next_refresh_index(recipes, refresh_cursor, refresh_excluded)
                continue
        elif from_pending:
            reference = pending.pop(0)
        else:
            reference = retry_candidates.pop(0) if retrying else recipes[cursor]
        package, _, version = reference.partition("/")
        try:
            known = refresh_policy.known_version(checked, reference)
            if known and "@" in known and known.partition("@")[0] == version:
                # The recipe revision is the first request of an inspection.  When it is
                # the one recorded, the search, package revisions and manifest are skipped.
                revision, spent = _conan_recipe_revision(package, version, timeout)
                downloaded += spent
                if known == f"{version}@{revision}":
                    unchanged += 1
                    refresh_policy.record_check(checked, reference, known, day)
                    unavailable.pop(reference, None)
                    _clear_failure(state, failures, attempts, reference)
                    raise _Unchanged
            _conan_revisions.pop((package, version), None)
            commands, manifest_url, spent, inspected_version = _conan_package_commands(reference, timeout)
            downloaded += spent
            unavailable.pop(reference, None)
            if manifest_url:
                uninspected.pop(reference, None)
            else:
                uninspected[reference] = "no built package published for this recipe"
            # `version` is what was inspected; `latest_version` is what the recipe declares.
            package_rows = [record(command, "conan", package, inspected_version,
                                   "https://github.com/conan-io/conan-center-index",
                                   manifest_url or CONAN_REMOTE, "filesystem",
                                   source_type="language_package", language="c++",
                                   package_system="conan", registry="conancenter",
                                   latest_version=version)
                            for command in commands]
            rows.extend(package_rows)
            replacement_rows.extend(package_rows)
            replaced_packages.add(package)
            revision = _conan_revisions.get((package, inspected_version))
            if manifest_url and revision:
                refresh_policy.record_check(checked, reference, f"{inspected_version}@{revision}", day)
            else:
                refresh_policy.forget(checked, reference)
            _clear_failure(state, failures, attempts, reference)
        except _Unchanged:
            pass
        except Exception as error:
            _record_failure(failures, unavailable, reference, error, attempts,
                            state.setdefault("failure_details", {}))
            if reference in failures and not _schedule_transient_retry(state, reference, error):
                failures.pop(reference, None)
            if reference not in failures:
                _drop_retry_key(state, reference)
            if reference in failures and reference not in retry_recipes:
                retry_recipes.append(reference)
        if refreshing:
            refreshed += 1
            refresh_remaining -= 1
            refresh_cursor = (refresh_index + 1) % len(recipes)
            refresh_index = _next_refresh_index(recipes, refresh_cursor, refresh_excluded)
        elif not retrying and not from_pending:
            cursor += 1
        processed += 1
        if downloaded > byte_budget:
            budget_exhausted = True
            break
        if _due_for_checkpoint(processed) or interrupted():
            collected += len(rows)
            _replace_package_rows(output, replaced_packages, replacement_rows)
            rows.clear(); replacement_rows.clear(); replaced_packages.clear()
            checkpoint(cursor=cursor, refresh_cursor=refresh_cursor)
        if interrupted():
            break
    state["cursor"] = cursor
    state["catalog_size"] = len(recipes)
    state["refresh_cursor"] = refresh_cursor
    collected += len(rows)
    _replace_package_rows(output, replaced_packages, replacement_rows)
    retry_waiting = _retry_waiting_count(state, "retry_recipes")
    # A recipe the remote does not publish was never inspected either, so it holds the
    # source short of complete exactly like one nobody has built.
    complete = (cursor >= len(recipes) and not failures and not retry_recipes and not pending
                and not state.get("blocked") and not uninspected and not unavailable)
    result = {"cursor": cursor, "refresh_cursor": refresh_cursor, "refreshed": refreshed,
              "catalog_size": len(recipes), "processed": processed, "records": collected,
              "downloaded_bytes": downloaded, "failures": len(failures),
              "unavailable": len(unavailable), "budget_exhausted": budget_exhausted,
              "retry_pending": len(retry_recipes), "retry_waiting": retry_waiting,
              "blocked": len(state.get("blocked", {})), "uninspected": len(uninspected),
              "catalog_pending": len(pending), "catalog_fetched_at": state.get("catalog_fetched_at"),
              "unchanged": unchanged, "skipped_not_due": skipped, "checked": len(checked),
              "ttl": refresh_policy.ttl_summary(checked, day, BACKOFF_MAX_DAYS_CONAN),
              "feed": feed_report,
              "failure_details": _failure_diagnostics(state, failures, "retry_recipes"),
              "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}
    if catalog_refresh:
        result["catalog_refresh"] = catalog_refresh
    result["status"] = "success" if complete else "partial"
    return result


def _refuse_empty_exhaustive(result: dict[str, Any], observations: Path) -> None:
    """A registry that has yielded nothing has not been surveyed, whatever its cursor says.

    npm reported `exhaustive` for its whole history while its parser read the wrong
    object and recorded no commands at all.  Completeness is the claim that licenses a
    negative answer, so it has to be backed by evidence on file, not by a cursor.
    """
    collected = sum(1 for line in observations.open(encoding="utf-8") if line.strip()) if observations.is_file() else 0
    result["observations"] = collected
    if result.get("coverage_kind") == "exhaustive" and collected == 0:
        result.update({"coverage_kind": "partial", "complete": False,
                       "error": "claimed exhaustive with no observations on file"})


def crawl_registry_sources(sources: list[str], state_path: Path, output_dir: Path, report_path: Path,
                           package_budget: int = 100, byte_budget: int = 500_000_000,
                           timeout: int = 120, source_budgets: dict[str, int] | None = None) -> dict[str, Any]:
    state = _load_json(state_path, {"version": 1, "sources": {}})
    output_dir.mkdir(parents=True, exist_ok=True); report: dict[str, Any] = {"status": "success", "sources": {}}
    runners: dict[str, Callable[..., dict[str, Any]]] = {
        "crates": _crawl_crates, "nuget": _crawl_nuget, "conan": _crawl_conan,
    }
    source_budgets = source_budgets or {}
    for source in sources:
        if source not in runners:
            raise RegistryCrawlError(f"unsupported registry source: {source}")
        source_state = state["sources"].setdefault(source, {})
        budget = int(source_budgets.get(source, package_budget))
        observations = output_dir / f"{source}.jsonl"
        def checkpoint(buffer: list[dict[str, Any]] | None = None, _state: dict[str, Any] = source_state,
                       _observations: Path = observations, **updates: Any) -> None:
            _state.update(updates)
            if buffer:
                _append_rows(_observations, buffer)
                buffer.clear()
            _save_json(state_path, state)

        source_retry = source_state.get("source_retry")
        if isinstance(source_retry, dict) and not _retry_due({"source": source_retry}, "source"):
            report["sources"][source] = {
                "status": "partial", "coverage_kind": "partial", "package_budget": budget,
                "retry_pending": 1, "retry_waiting": 1,
                "failure_details": {"source": source_state.get("source_failure", {})},
            }
            report["status"] = "partial"
            _save_json(state_path, state)
            if interrupted():
                report["interrupted"] = True
                break
            continue

        try:
            install_dns_cache()  # a lookup per package is what the resolver gives way under
            _start_checkpoint_clock()  # each source gets its own two minutes, not the last one's
            report["sources"][source] = runners[source](source_state, observations, budget,
                                                        byte_budget, timeout, checkpoint)
            report["sources"][source]["package_budget"] = budget
            _refuse_empty_exhaustive(report["sources"][source], observations)
            source_state.pop("source_retry", None)
            source_state.pop("source_failure", None)
            source_state.pop("source_blocked", None)
        except Exception as error:
            details = _error_details(error)
            source_state["source_failure"] = details
            if isinstance(error, RegistryRequestError) and _request_is_retryable(error):
                previous = source_state.get("source_retry", {})
                attempts = int(previous.get("attempts", 0)) + 1 if isinstance(previous, dict) else 1
                if attempts >= TRANSIENT_RETRY_LIMIT:
                    source_state["source_blocked"] = {"attempts": attempts, **details}
                    source_state.pop("source_retry", None)
                    report["sources"][source] = {
                        "status": "failed", "error": str(error), "coverage_kind": "partial",
                        "blocked": 1, "failure_details": {"source": details},
                    }
                    report["status"] = "failed"
                else:
                    delay = min(TRANSIENT_RETRY_BASE * (2 ** (attempts - 1)), TRANSIENT_RETRY_CAP)
                    source_state["source_retry"] = {
                        "attempts": attempts, "next_attempt_at": (_utc_now() + timedelta(seconds=delay)).isoformat(),
                        **details,
                    }
                    report["sources"][source] = {
                        "status": "partial", "coverage_kind": "partial",
                        "retry_pending": 1, "retry_waiting": 0,
                        "failure_details": {"source": details},
                    }
                    report["status"] = "partial"
            else:
                report["sources"][source] = {"status": "failed", "error": str(error),
                                               "coverage_kind": "partial",
                                               "failure_details": {"source": details}}
                report["status"] = "failed"
        _save_json(state_path, state)  # a later source must not cost this one its cursor
        result = report["sources"].get(source, {})
        if result.get("error") or result.get("status") == "failed":
            report["status"] = "failed"
        elif (result.get("failures", 0) or result.get("retry_pending", 0) or
              result.get("blocked", 0) or result.get("coverage_kind") != "exhaustive"):
            report["status"] = "partial"
        if interrupted():
            report["interrupted"] = True
            break
    report["coverage_kind"] = ("exhaustive" if report["status"] == "success" and report["sources"] and
                                all(v.get("coverage_kind") == "exhaustive" for v in report["sources"].values())
                                else "partial")
    report["state"] = str(state_path); report["package_budget"] = package_budget; report["byte_budget"] = byte_budget
    _save_json(state_path, state)
    _write_text_atomic(report_path, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report
