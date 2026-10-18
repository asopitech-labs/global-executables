"""Zero-setup, read-only access to the published dictionary over HTTPS.

When no local checkout of the ``dictionary`` branch is available, the MCP server
reads only the files a query needs from the public raw-file URL and keeps them in a
local cache. Every cached file is revalidated with its ``ETag`` once per published
metadata generation, so unchanged files cost a ``304`` and no body. When the network
is unreachable, previously cached files are served as they are (offline fallback).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .search import Dataset

DEFAULT_DATASET_URL = "https://raw.githubusercontent.com/asopitech-labs/global-executables/dictionary"
METADATA_PATH = "data/metadata.json"
DEFAULT_REVALIDATE_SECONDS = 600.0
USER_AGENT = f"global-executables-mcp/{__version__} (+https://github.com/asopitech-labs/global-executables)"

log = logging.getLogger(__name__)


class DatasetUnavailableError(RuntimeError):
    """The published dataset is unreachable and no cached copy exists."""


def default_cache_dir() -> Path:
    """Return the per-user cache directory, honoring GLOBAL_EXECUTABLES_CACHE_DIR."""
    override = os.getenv("GLOBAL_EXECUTABLES_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "global-executables" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "global-executables"
    return Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "global-executables"


Fetcher = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]


def _urlopen(request: urllib.request.Request, timeout: float) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, b""


class HttpFileCache:
    """A tiny ETag-aware file cache keyed by dataset-relative paths."""

    def __init__(self, base_url: str, cache_dir: Path, *, offline: bool = False,
                 timeout: float = 20.0, fetcher: Fetcher | None = None):
        self.base_url = base_url.rstrip("/")
        self.root = Path(cache_dir) / hashlib.sha256(self.base_url.encode()).hexdigest()[:16]
        self.offline = offline
        self.timeout = timeout
        self._fetch = fetcher or _urlopen
        self._warned_offline = False

    def _paths(self, relative: str) -> tuple[Path, Path]:
        parts = relative.split("/")
        if relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"unsafe dataset path: {relative!r}")
        body = self.root.joinpath(*parts)
        return body, body.with_name(body.name + ".meta")

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _load(self, relative: str) -> tuple[dict[str, Any] | None, bytes | None]:
        body, meta = self._paths(relative)
        try:
            entry = json.loads(meta.read_bytes())
        except (OSError, ValueError):
            return None, None
        if entry.get("status") == 404:
            return entry, None
        try:
            return entry, body.read_bytes()
        except OSError:
            return None, None

    def _stale(self, relative: str, entry: dict[str, Any] | None, cached: bytes | None, reason: str) -> bytes | None:
        if entry is None:
            raise DatasetUnavailableError(
                f"cannot fetch {relative} from {self.base_url} ({reason}) and it is not cached; "
                "connect to the network once, or set GLOBAL_EXECUTABLES_DATASET_ROOT to a local "
                "checkout of the dictionary branch")
        if not self._warned_offline:
            log.warning("dataset unreachable (%s); serving cached files from %s", reason, self.root)
            self._warned_offline = True
        return cached

    def get(self, relative: str, generation: str | None, *, revalidate: bool = False,
            unconditional: bool = False) -> bytes | None:
        """Return a file's bytes, or None when the published dataset has no such file."""
        entry, cached = self._load(relative)
        if entry is not None and not revalidate and not unconditional and entry.get("generation") == generation:
            return cached
        if self.offline:
            return self._stale(relative, entry, cached, "offline mode")
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        if entry is not None and entry.get("etag") and not unconditional and (cached is not None or entry.get("status") == 404):
            headers["If-None-Match"] = entry["etag"]
        request = urllib.request.Request(f"{self.base_url}/{relative}", headers=headers)
        try:
            status, response_headers, payload = self._fetch(request, self.timeout)
        except (OSError, TimeoutError) as error:  # URLError is an OSError
            return self._stale(relative, entry, cached, str(error))
        body_path, meta_path = self._paths(relative)
        if status == 304 and entry is not None:
            entry["generation"] = generation
            self._write(meta_path, json.dumps(entry).encode())
            return cached
        if status == 404:
            body_path.unlink(missing_ok=True)
            self._write(meta_path, json.dumps({"status": 404, "etag": response_headers.get("etag"),
                                               "generation": generation}).encode())
            return None
        if status != 200:
            return self._stale(relative, entry, cached, f"HTTP {status}")
        if response_headers.get("content-encoding") == "gzip":
            payload = gzip.decompress(payload)
        self._write(body_path, payload)
        self._write(meta_path, json.dumps({"status": 200, "etag": response_headers.get("etag"),
                                           "generation": generation}).encode())
        return payload


class RemoteDataset(Dataset):
    """A Dataset whose files come from the published dictionary over HTTPS."""

    def __init__(self, base_url: str = DEFAULT_DATASET_URL, cache_dir: str | Path | None = None, *,
                 offline: bool = False, revalidate_seconds: float = DEFAULT_REVALIDATE_SECONDS,
                 timeout: float = 20.0, fetcher: Fetcher | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.cache = HttpFileCache(self.base_url, Path(cache_dir) if cache_dir else default_cache_dir(),
                                   offline=offline, timeout=timeout, fetcher=fetcher)
        super().__init__(self.cache.root)
        self.revalidate_seconds = revalidate_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._metadata_raw: bytes | None = None
        self._metadata: dict[str, Any] | None = None
        self._generation: str | None = None
        self._checked_at: float | None = None

    def _refresh(self) -> None:
        now = self._clock()
        if self._metadata is not None and self._checked_at is not None and now - self._checked_at < self.revalidate_seconds:
            return
        raw = self.cache.get(METADATA_PATH, None, revalidate=True)
        if raw is None:
            raise FileNotFoundError(f"dataset metadata not found at {self.base_url}/{METADATA_PATH}")
        if raw != self._metadata_raw:
            self._metadata = json.loads(raw)
            self._metadata_raw = raw
            self._generation = hashlib.sha256(raw).hexdigest()
        self._checked_at = now

    @property
    def generation(self) -> str:
        with self._lock:
            self._refresh()
            assert self._generation is not None
            return self._generation

    @property
    def metadata(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            assert self._metadata is not None
            return self._metadata

    def _read_bytes(self, relative: str) -> bytes | None:
        with self._lock:
            if relative == METADATA_PATH:
                self._refresh()
                return self._metadata_raw
            return self.cache.get(relative, self.generation)

    def _verified_index_bytes(self, relative: str, expected: str) -> bytes | None:
        with self._lock:
            raw = self.cache.get(f"data/{relative}", self.generation)
            if raw is not None and hashlib.sha256(raw).hexdigest() != expected and not self.cache.offline:
                # The cached copy predates the current metadata; fetch it in full once.
                raw = self.cache.get(f"data/{relative}", self.generation, unconditional=True)
            return raw
