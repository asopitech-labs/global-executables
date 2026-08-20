"""Resumable artifact crawlers for registry-backed executable evidence.

The crawler is deliberately budgeted.  A source becomes ``exhaustive`` only
after its catalog cursor reaches the end, every selected artifact is inspected,
and no failures remain.  A stopped or rate-limited run remains partial.
"""
from __future__ import annotations

import csv
import html
import json
import gzip
import re
import signal
import struct
import sys
import tarfile
import time
import tomllib
import urllib.parse
import urllib.error
import urllib.request
import zipfile
import zlib
from io import BytesIO, RawIOBase, TextIOWrapper
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .collectors import crates_manifest, npm_metadata, record
from .model import write_jsonl


USER_AGENT = "global-executables-registry-crawl/1.0 (+https://github.com/asopitech-labs/global-executables)"
PROJECT_LINK = re.compile(r"<a\b[^>]*href=[\"'][^\"']+[\"'][^>]*>([^<]+)</a>", re.I)
CRATES_DB_DUMP = "https://static.crates.io/db-dump.tar.gz"
# crates.io asks crawlers for at most one request per second and answers 429 well before
# a CI runner's natural pace.  Only the API host is paced; its CDN mirrors are not.
HOST_MIN_INTERVAL = {"crates.io": 1.0}
RETRY_AFTER_CAP = 60.0
# Crate conditions no later run can resolve; retrying them forever would hold the
# source below exhaustive.
PERMANENT_CRATE_CONDITIONS = ("crate has no non-yanked version:", "crate archive has no readable Cargo.toml:",
                              "module has no latest version:")
# Bumped when an npm parsing fix invalidates the coverage an earlier cursor claimed.
NPM_PARSER_GENERATION = 3
NPM_ALL_DOCS = "https://replicate.npmjs.com/_all_docs"
NPM_CATALOG_PAGE = 10000
GO_INDEX = "https://index.golang.org/index"
GO_INDEX_EPOCH = "2019-01-01T00:00:00Z"
GO_INDEX_PAGE = 2000
# Index pages are cheap — roughly 240KB per 2,000 entries — but a run still has to
# leave time to inspect modules.
GO_CATALOG_REQUESTS = 3000
# Below this, one download beats several range requests.  Above this many directories a
# module is better taken whole than probed, however cheap each probe is.
GO_SMALL_ZIP_BYTES = 262144
GO_DIRECTORY_PROBES = 512
# Progress is persisted this often inside a pass.  State used to be written once, after
# every source finished, so an interruption discarded the cursors of sources that had
# already completed along with the work in flight.
CHECKPOINT_INTERVAL = 200
_last_request: dict[str, float] = {}
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


class RegistryCrawlError(RuntimeError):
    pass


def _throttle(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname or ""
    interval = HOST_MIN_INTERVAL.get(host)
    if interval is None:
        return
    pause = interval - (time.monotonic() - _last_request.get(host, float("-inf")))
    if pause > 0:
        time.sleep(pause)
    _last_request[host] = time.monotonic()


def _retry_after_seconds(error: urllib.error.HTTPError, attempt: int) -> float:
    advertised = (error.headers or {}).get("Retry-After") if hasattr(error, "headers") else None
    try:
        return min(float(advertised), RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        return min(2.0 ** attempt, RETRY_AFTER_CAP)


def fetch(url: str, timeout: int = 120, attempts: int = 4) -> tuple[bytes, dict[str, Any]]:
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        _throttle(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                return body, {"url": url, "status_code": response.status, "downloaded_bytes": len(body),
                              "duration_seconds": round(time.monotonic() - started, 3)}
        except urllib.error.HTTPError as error:
            # Back off on rate limiting rather than losing the rest of the source's budget.
            if error.code != 429 or attempt == attempts:
                raise
            time.sleep(_retry_after_seconds(error, attempt))
    raise RegistryCrawlError(f"unreachable retry loop: {url}")


def fetch_range(url: str, start: int, end: int, timeout: int = 120) -> tuple[bytes, dict[str, Any]]:
    """Fetch one inclusive byte range, refusing a server that ignores the request."""
    started = time.monotonic()
    _throttle(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 206:
            raise RegistryCrawlError(f"host ignored the range request: {url}")
        body = response.read()
    return body, {"url": url, "status_code": 206, "downloaded_bytes": len(body),
                  "duration_seconds": round(time.monotonic() - started, 3)}


def content_length(url: str, timeout: int = 120) -> int:
    _throttle(url)
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
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
        self.entries = self._central_directory()

    def _range(self, start: int, end: int) -> bytes:
        body, transfer = fetch_range(self.url, max(0, start), min(end, self.size - 1), self.timeout)
        self.downloaded += transfer["downloaded_bytes"]
        return body

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
    if not path.is_file():
        return default
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RegistryCrawlError(f"invalid registry crawl state: {path}")
    return value


def _save_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


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


def write_catalog(path: Path, names: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = path.with_suffix(path.suffix + ".gz")
    with gzip.open(packed, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(names) + "\n")
    path.unlink(missing_ok=True)


def _append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _failure_state(state: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return retryable failures and preserve permanent 404s separately."""
    failures = state.setdefault("failures", {})
    unavailable = state.setdefault("unavailable", {})
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
        elif "HTTP Error 404" in text:
            unavailable[key] = text
            failures.pop(key, None)
    retry_projects[:] = [key for key in retry_projects if key not in unavailable]
    return failures, unavailable


def _record_failure(failures: dict[str, str], unavailable: dict[str, str], key: str, error: Exception) -> None:
    message = str(error)
    if isinstance(error, urllib.error.HTTPError) and error.code == 404:
        unavailable[key] = message
        failures.pop(key, None)
    elif message.startswith(PERMANENT_CRATE_CONDITIONS):
        unavailable[key] = message
        failures.pop(key, None)
    else:
        failures[key] = message


def _archive_files(body: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=BytesIO(body), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.isfile() and member.name not in files:
                    handle = archive.extractfile(member)
                    if handle is not None:
                        files[member.name] = handle.read()
        return files
    except tarfile.TarError:
        pass
    with zipfile.ZipFile(BytesIO(body)) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def _entry_point_rows(files: dict[str, bytes], package: str, version: str,
                      repository: str | None, source: str, artifact: str) -> list[dict[str, Any]]:
    commands: set[str] = set()

    def add_lines(value: str) -> None:
        for line in value.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "[")) and "=" in line:
                commands.add(line.split("=", 1)[0].strip())

    for name, body in files.items():
        if name.endswith(".dist-info/entry_points.txt") or name.endswith(".egg-info/entry_points.txt"):
            section = None
            for raw in body.decode("utf-8", "replace").splitlines():
                line = raw.strip()
                if line.startswith("["):
                    section = line
                elif section == "[console_scripts]" and "=" in line:
                    commands.add(line.split("=", 1)[0].strip())

    for name, body in files.items():
        text = body.decode("utf-8", "replace")
        if name.endswith("pyproject.toml"):
            try:
                document = tomllib.loads(text)
                project = document.get("project", {})
                commands.update(project.get("scripts", {}).keys())
                commands.update(project.get("entry-points", {}).get("console_scripts", {}).keys())
                poetry = document.get("tool", {}).get("poetry", {}).get("scripts", {})
                commands.update(poetry.keys())
            except (tomllib.TOMLDecodeError, AttributeError):
                pass
        elif name.endswith("setup.cfg"):
            in_console = False
            for raw in text.splitlines():
                line = raw.strip()
                if line.startswith("["):
                    in_console = line.lower() == "[options.entry_points]"
                elif in_console and "=" in line:
                    add_lines(line)
        elif name.endswith("setup.py"):
            for match in re.finditer(r"console_scripts[^\[\(]*[\[\(](.*?)[\]\)]", text, re.S):
                add_lines(match.group(1).replace(",", "\n"))

    return [record(command, "pypi", package, version, repository, artifact,
                   source_type="language_package", language="python", registry="pypi",
                   latest_version=version) for command in sorted(commands)]


def _sdist_rows(body: bytes, package: str, version: str, repository: str | None, source: str) -> list[dict[str, Any]]:
    return _entry_point_rows(_archive_files(body), package, version, repository, source, source)


def _console_scripts(text: str) -> list[str]:
    commands = []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
        elif section == "[console_scripts]" and "=" in line:
            commands.append(line.split("=", 1)[0].strip())
    return commands


def _console_script_rows(commands: list[str], package: str, version: str, repository: str | None,
                         source: str) -> list[dict[str, Any]]:
    return [record(command, "pypi", package, version, repository, source,
                   source_type="language_package", language="python", registry="pypi",
                   latest_version=version) for command in sorted(set(commands))]


def _wheel_commands_from(names: list[str], read: Callable[[str], bytes]) -> list[str]:
    commands: list[str] = []
    for name in names:
        if name.endswith(".dist-info/entry_points.txt"):
            commands.extend(_console_scripts(read(name).decode("utf-8", "replace")))
        # A wheel shipping a prebuilt binary declares it as a data script, not a
        # console script; ruff and friends were invisible while only entry points were read.
        elif ".data/scripts/" in name and not name.endswith("/"):
            commands.append(name.rsplit("/", 1)[-1])
    return commands


def _wheel_commands(url: str, timeout: int) -> tuple[list[str], int]:
    """Read a wheel's declared commands over HTTP ranges rather than downloading it.

    A wheel is a ZIP whose file list sits in the trailing central directory, and the
    commands live in one small member; the rest of a multi-megabyte wheel is payload
    the crawler never reads.  Falls back to the whole file when the host or the archive
    will not cooperate.
    """
    try:
        archive = RemoteZip(url, timeout)
        return _wheel_commands_from(archive.names, archive.read), archive.downloaded
    except (RegistryCrawlError, urllib.error.HTTPError, OSError, struct.error, zlib.error, KeyError):
        body, transfer = fetch(url, timeout)
        with zipfile.ZipFile(BytesIO(body)) as whole:
            return _wheel_commands_from(whole.namelist(), whole.read), transfer["downloaded_bytes"]


def _wheel_rows(body: bytes, package: str, version: str, repository: str | None, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(BytesIO(body)) as archive:
        entries = [name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")]
        for entry in entries:
            section = None
            for raw in archive.read(entry).decode("utf-8", "replace").splitlines():
                line = raw.strip()
                if line.startswith("["):
                    section = line
                elif section == "[console_scripts]" and "=" in line:
                    command = line.split("=", 1)[0].strip()
                    rows.append(record(command, "pypi", package, version, repository, source,
                                       source_type="language_package", language="python", registry="pypi",
                                       latest_version=version))
    return rows


def _go_rows(body: bytes, module: str, version: str, source: str) -> list[dict[str, Any]]:
    """Extract one executable name for each Go directory containing package main."""
    root = f"{module}@{version}"
    main_directories: set[str] = set()
    with zipfile.ZipFile(BytesIO(body)) as archive:
        for name in archive.namelist():
            if not name.startswith(root + "/") or not name.endswith(".go") or name.endswith("_test.go"):
                continue
            if "/vendor/" in f"/{name}" or "/testdata/" in f"/{name}":
                continue
            text = archive.read(name).decode("utf-8", "replace")
            if re.search(r"(?m)^\s*package\s+main\b", text):
                main_directories.add(name.rsplit("/", 1)[0])
    rows: list[dict[str, Any]] = []
    for directory in sorted(main_directories):
        command = directory.rsplit("/", 1)[-1] if directory != root else module.rsplit("/", 1)[-1]
        rows.append(record(command, "go", module, version, None, source,
                           source_type="language_package", language="go", registry="go",
                           latest_version=version))
    return rows


def _ruby_gem_rows(body: bytes, package: str, version: str, repository: str | None,
                   source: str) -> list[dict[str, Any]]:
    """Extract RubyGems' declared executables from the gemspec metadata."""
    with tarfile.open(fileobj=BytesIO(body), mode="r:*") as archive:
        metadata_name = next(name for name in archive.getnames() if name == "metadata.gz")
        handle = archive.extractfile(metadata_name)
        if handle is None:
            return []
        metadata = gzip.decompress(handle.read()).decode("utf-8", "replace")
    return _gem_rows(metadata, package, version, repository, source)


def _gem_rows(metadata: str, package: str, version: str, repository: str | None,
              source: str) -> list[dict[str, Any]]:
    commands: set[str] = set()
    lines = metadata.splitlines()
    for index, raw in enumerate(lines):
        if not re.match(r"^executables:\s*(?:\[\s*\]|)$", raw.strip()):
            continue
        remainder = raw.split(":", 1)[1].strip()
        if remainder and remainder != "[]":
            commands.update(value.strip(" \\\"'") for value in remainder.strip("[]").split(",") if value.strip())
        for child in lines[index + 1:]:
            stripped = child.strip()
            if stripped.startswith("-"):
                value = stripped[1:].strip().strip("\\\"'")
                if value:
                    commands.add(value)
            elif stripped and not child.startswith((" ", "\t")):
                break
        break
    return [record(command, "rubygems", package, version, repository, source,
                   source_type="language_package", language="ruby", registry="rubygems",
                   latest_version=version) for command in sorted(commands)]


GEM_HEAD_BYTES = 65536


def _gem_metadata(url: str, timeout: int) -> tuple[bytes, int]:
    """Fetch just enough of a .gem to reach its metadata.gz member.

    A .gem is an uncompressed tar whose gemspec sits near the front, so the declared
    executables are readable from the first few blocks instead of the whole gem.
    Falls back to the whole file when the member is not in the head.
    """
    try:
        size = content_length(url, timeout)
        if size > GEM_HEAD_BYTES:
            head, transfer = fetch_range(url, 0, GEM_HEAD_BYTES - 1, timeout)
            padded = head + b"\0" * 1024
            with tarfile.open(fileobj=BytesIO(padded), mode="r|") as archive:
                for member in archive:
                    if member.name == "metadata.gz":
                        handle = archive.extractfile(member)
                        if handle is not None:
                            return handle.read(), transfer["downloaded_bytes"]
                    break  # metadata.gz is the first member when the layout is conventional
    except (RegistryCrawlError, urllib.error.HTTPError, OSError, tarfile.TarError, EOFError):
        pass
    body, transfer = fetch(url, timeout)
    with tarfile.open(fileobj=BytesIO(body), mode="r:*") as archive:
        handle = archive.extractfile("metadata.gz")
        return (handle.read() if handle is not None else b""), transfer["downloaded_bytes"]


def _pypi_projects(body: bytes) -> list[str]:
    names = {html.unescape(value).strip() for value in PROJECT_LINK.findall(body.decode("utf-8", "replace"))}
    return sorted(name for name in names if name)


def _rubygems_names(body: bytes) -> list[str]:
    names = set()
    for line in body.decode("utf-8", "replace").splitlines():
        name = line.strip()
        if name and name != "---" and not name.endswith(":") and " " not in name:
            names.add(name)
    return sorted(names)


def _packagist_packages(body: bytes) -> list[str]:
    value = json.loads(body)
    names = value.get("packageNames", []) if isinstance(value, dict) else []
    return sorted(name.strip() for name in names if isinstance(name, str) and name.strip())


def _packagist_rows(value: dict[str, Any], package: str, source: str) -> list[dict[str, Any]]:
    """Extract Composer ``bin`` declarations from the newest Packagist version."""
    versions = value.get("packages", {}).get(package, [])
    if not isinstance(versions, list) or not versions:
        return []
    metadata = versions[0]
    bins = metadata.get("bin", []) if isinstance(metadata, dict) else []
    if isinstance(bins, str):
        bins = [bins]
    if not isinstance(bins, list):
        return []
    version = metadata.get("version", "unknown")
    repository = metadata.get("source") or metadata.get("homepage")
    if isinstance(repository, dict):
        repository = repository.get("url")
    commands = {PurePosixPath(entry).name for entry in bins if isinstance(entry, str) and entry.strip()}
    return [record(command, "packagist", package, version, repository, source,
                   source_type="language_package", language="php", registry="packagist",
                   latest_version=version)
            for command in sorted(commands) if command]


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


def _crawl_pypi(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    project_file = Path(state.setdefault("projects_file", "data/production/pypi-projects.txt"))
    if not read_catalog(project_file):
        body, transfer = fetch("https://pypi.org/simple/", timeout)
        project_file.parent.mkdir(parents=True, exist_ok=True)
        write_catalog(project_file, _pypi_projects(body))
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    projects = read_catalog(project_file)
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0
    failures, unavailable = _failure_state(state)
    retry_projects = state.setdefault("retry_projects", [])
    retry_projects[:] = [project for project in retry_projects if project not in unavailable]
    rows: list[dict[str, Any]] = []
    budget_exhausted = False
    retry_quota = max(1, budget // 2)
    retry_used = 0
    while (retry_projects or cursor < len(projects)) and processed < budget:
        retrying = bool(retry_projects) and (retry_used < retry_quota or cursor >= len(projects))
        project = retry_projects.pop(0) if retrying else projects[cursor]
        try:
            metadata_body, _ = fetch(f"https://pypi.org/pypi/{urllib.parse.quote(project)}/json", timeout)
            metadata = json.loads(metadata_body); info = metadata.get("info", {})
            candidates = [item for item in metadata.get("urls", []) if item.get("packagetype") in {"bdist_wheel", "sdist"}]
            candidates.sort(key=lambda item: (item.get("packagetype") != "bdist_wheel",
                                              "none-any" not in item.get("filename", ""), item.get("filename", "")))
            if not candidates:
                unavailable[project] = "latest release has no wheel or sdist"
                failures.pop(project, None)
            else:
                package = info.get("name", project); version = info.get("version", "unknown")
                selected = False; last_error: Exception | None = None
                for candidate in candidates:
                    url = candidate["url"]
                    try:
                        if candidate.get("packagetype") == "bdist_wheel":
                            commands, spent = _wheel_commands(url, timeout)
                            downloaded += spent
                            if downloaded > byte_budget:
                                budget_exhausted = True
                                break
                            rows.extend(_console_script_rows(commands, package, version, info.get("home_page"), url))
                        else:
                            artifact, transfer = fetch(url, timeout)
                            downloaded += transfer["downloaded_bytes"]
                            if downloaded > byte_budget:
                                budget_exhausted = True
                                break
                            rows.extend(_sdist_rows(artifact, package, version, info.get("home_page"), url))
                        selected = True
                        break
                    except Exception as error:
                        last_error = error
                if budget_exhausted:
                    if retrying:
                        retry_projects.insert(0, project)
                    break
                if not selected:
                    raise last_error or RegistryCrawlError("no usable wheel or sdist")
                failures.pop(project, None)
        except Exception as error:  # keep the cursor moving; failures block exhaustive status
            _record_failure(failures, unavailable, project, error)
        if not retrying:
            cursor += 1
        else:
            retry_used += 1
        processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            checkpoint(rows, cursor=cursor)
        if budget_exhausted or interrupted():
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    return {"cursor": cursor, "catalog_size": len(projects), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "budget_exhausted": budget_exhausted,
            "unavailable": len(unavailable), "retry_pending": len(retry_projects),
            "complete": cursor >= len(projects) and not failures and not retry_projects,
            "coverage_kind": "exhaustive" if cursor >= len(projects) and not failures and not retry_projects else "partial"}


def _npm_release_url(name: str) -> str:
    """Address the latest release, not the packument.

    registry.npmjs.org answers a bare package name with every version it ever
    published, and `bin` lives inside `versions[...]` — never at the top level, which
    is where the parser looked.  The `/latest` document is the shape the parser
    expects and is a few kilobytes rather than megabytes.
    """
    return "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@") + "/latest"


def _crawl_npm(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
               checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    """Inspect every npm package once, from the registry's own package list.

    The changes feed carries every revision — over 126 million of them against 4.3
    million packages — so walking it to reach each package once costs thirty times
    what enumerating the packages does.
    """
    if int(state.get("parser_generation", 1)) < NPM_PARSER_GENERATION:
        state.update({"parser_generation": NPM_PARSER_GENERATION, "cursor": 0})
        for retired in ("since", "complete", "retry_packages"):
            state.pop(retired, None)
    catalog_file = Path(state.setdefault("packages_file", "data/production/npm-packages.txt"))
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    if not read_catalog(catalog_file):
        names: list[str] = []
        start_key = ""
        while True:
            query = {"limit": NPM_CATALOG_PAGE}
            if start_key:
                query["startkey"] = json.dumps(start_key)
            body, transfer = fetch(f"{NPM_ALL_DOCS}?{urllib.parse.urlencode(query)}", timeout)
            downloaded += transfer["downloaded_bytes"]
            rows = json.loads(body).get("rows", [])
            if start_key:
                rows = [row for row in rows if row.get("id") != start_key]
            if not rows:
                break
            names.extend(row["id"] for row in rows if row.get("id") and not row["id"].startswith("_"))
            start_key = rows[-1]["id"]
            if interrupted():
                break
        write_catalog(catalog_file, names)
    packages = read_catalog(catalog_file)
    cursor = int(state.get("cursor", 0)); processed = 0
    failures, unavailable = _failure_state(state)
    rows_out: list[dict[str, Any]] = []
    budget_exhausted = False
    while cursor < len(packages) and processed < budget:
        name = packages[cursor]
        try:
            metadata_url = _npm_release_url(name)
            metadata_body, transfer = fetch(metadata_url, timeout)
            downloaded += transfer["downloaded_bytes"]
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            rows_out.extend(npm_metadata(json.loads(metadata_body), metadata_url))
            failures.pop(name, None)
        except Exception as error:
            _record_failure(failures, unavailable, name, error)
        cursor += 1; processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            checkpoint(rows_out, cursor=cursor)
        if interrupted():
            break
    state["cursor"] = cursor
    _append_rows(output, rows_out)
    complete = cursor >= len(packages) and not failures
    return {"cursor": cursor, "catalog_size": len(packages), "processed": processed,
            "records": len(rows_out), "downloaded_bytes": downloaded, "failures": len(failures),
            "unavailable": len(unavailable), "budget_exhausted": budget_exhausted,
            "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}


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
    failures, unavailable = _failure_state(state)
    # The dump is republished daily and the observations replace rather than extend, so
    # re-reading an unchanged one costs 1.7GB to rewrite the same file.
    _throttle(CRATES_DB_DUMP)
    head = urllib.request.Request(CRATES_DB_DUMP, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(head, timeout=timeout) as response:
        published = response.headers.get("Last-Modified", "")
        size = int(response.headers.get("Content-Length") or 0)
    published_header = published
    if published and published == state.get("dump_last_modified") and output.is_file():
        collected = sum(1 for line in output.open(encoding="utf-8") if line.strip())
        complete = not failures
        return {"dump_timestamp": state.get("dump_timestamp"), "dump_last_modified": published,
                "catalog_size": state.get("catalog_size", 0), "cursor": state.get("catalog_size", 0),
                "processed": 0, "records": collected, "downloaded_bytes": 0, "dump_bytes": size,
                "failures": len(failures), "unavailable": len(unavailable),
                "budget_exhausted": False, "complete": complete, "unchanged": True,
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
    _throttle(CRATES_DB_DUMP)
    request = urllib.request.Request(CRATES_DB_DUMP, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
                      encoding="utf-8")
    state["dump_timestamp"] = published
    state["dump_last_modified"] = last_modified
    state["catalog_size"] = len(crates)
    state["complete"] = True
    complete = not failures
    return {"dump_timestamp": published, "catalog_size": len(crates), "cursor": len(crates),
            "crates_with_binaries": with_binaries, "processed": len(crates), "records": len(rows),
            "downloaded_bytes": downloaded, "dump_bytes": size,
            "failures": len(failures), "unavailable": len(unavailable),
            "budget_exhausted": False, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}



def _load_catalog_cursor(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _save_catalog_cursor(path: Path, since: str, complete: bool) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps({"since": since, "complete": complete}) + "\n")
    temporary.replace(path)


def _go_latest_version(module: str, timeout: int) -> str:
    escaped = urllib.parse.quote(module, safe="/@")
    body, _ = fetch(f"https://proxy.golang.org/{escaped}/@latest", timeout)
    version = json.loads(body).get("Version")
    if not version:
        raise RegistryCrawlError(f"module has no latest version: {module}")
    return version


def _go_module_rows(url: str, module: str, version: str, timeout: int) -> tuple[list[dict[str, Any]], int]:
    """Read a module's command names from the parts of the archive that can hold them.

    Every ``.go`` file in a directory must declare the same package, so one file per
    directory decides whether that directory is a command.  Small archives, and ones
    with more directories than probes would be worth, are still taken whole.
    """
    try:
        archive = RemoteZip(url, timeout)
        if archive.size > GO_SMALL_ZIP_BYTES:
            root = f"{module}@{version}/"
            directories: dict[str, str] = {}
            for name in archive.names:
                if not name.startswith(root) or not name.endswith(".go") or name.endswith("_test.go"):
                    continue
                relative = name[len(root):]
                if "vendor/" in f"/{relative}" or "testdata/" in f"/{relative}":
                    continue
                directories.setdefault(relative.rsplit("/", 1)[0] if "/" in relative else "", name)
            # The central directory already states each member's compressed size, so the
            # probe cost is known before spending it.
            probe_bytes = sum(archive.entries[member][1] for member in directories.values())
            if len(directories) <= GO_DIRECTORY_PROBES and probe_bytes * 2 < archive.size:
                commands: list[str] = []
                for directory, member in sorted(directories.items()):
                    text = archive.read(member).decode("utf-8", "replace")
                    if re.search(r"(?m)^\s*package\s+main\b", text):
                        commands.append(directory.rsplit("/", 1)[-1] if directory else module.rsplit("/", 1)[-1])
                return [record(command, "go", module, version, None, url,
                               source_type="language_package", language="go", registry="go",
                               latest_version=version) for command in sorted(set(commands))], archive.downloaded
    except (RegistryCrawlError, urllib.error.HTTPError, OSError, struct.error, zlib.error, KeyError):
        pass
    body, transfer = fetch(url, timeout)
    return _go_rows(body, module, version, url), transfer["downloaded_bytes"]


def _crawl_go(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
              checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    """Inspect each Go module once, at its current version.

    The module index lists every version of every module — tens of millions of entries,
    overwhelmingly republished copies of modules already inspected.  Reading the index
    is cheap, so the catalog phase distils the feed down to distinct module paths and
    the inspection phase spends the artifact budget on one archive per module.
    """
    state.pop("since", None)  # the old cursor tracked downloads, not catalog coverage
    catalog_file = Path(state.setdefault("modules_file", "data/production/go-modules.txt"))
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    modules = ([line.strip() for line in catalog_file.read_text().splitlines() if line.strip()]
               if catalog_file.is_file() else [])
    seen = set(modules)
    cursor_file = catalog_file.with_suffix(".cursor.json")
    catalog_cursor = _load_catalog_cursor(cursor_file)
    since = catalog_cursor.get("since") or state.get("catalog_since", GO_INDEX_EPOCH)
    catalog_complete = bool(catalog_cursor.get("complete") or state.get("catalog_complete"))
    failures, unavailable = _failure_state(state)
    retry_modules = state.setdefault("retry_modules", [])
    retry_modules[:] = [name for name in retry_modules if name not in unavailable]
    for name in failures:
        if name not in retry_modules:
            retry_modules.append(name)
    retry_budget = len(retry_modules)
    downloaded = 0
    discovered = 0
    index_requests = 0

    with catalog_file.open("a", encoding="utf-8") as handle:
        while not catalog_complete and index_requests < GO_CATALOG_REQUESTS:
            query = urllib.parse.urlencode({"limit": GO_INDEX_PAGE, "since": since})
            body, transfer = fetch(f"{GO_INDEX}?{query}", timeout)
            downloaded += transfer["downloaded_bytes"]
            index_requests += 1
            entries = [json.loads(line) for line in body.decode("utf-8", "replace").splitlines() if line.strip()]
            if not entries:
                catalog_complete = True
                break
            for entry in entries:
                path = entry.get("Path", "")
                since = entry.get("Timestamp", since)
                if path and path not in seen:
                    seen.add(path)
                    modules.append(path)
                    handle.write(path + "\n")
                    discovered += 1
            if len(entries) < GO_INDEX_PAGE:
                catalog_complete = True
            # The catalog phase is long enough to be interrupted, and the run-level state
            # is only written when the whole pass returns.  Checkpoint beside the catalog
            # so an interrupted sweep resumes instead of re-walking the index.
            handle.flush()
            _save_catalog_cursor(cursor_file, since, catalog_complete)
            if interrupted():
                break
    state["catalog_since"] = since
    state["catalog_complete"] = catalog_complete

    cursor = int(state.get("cursor", 0))
    processed = 0
    rows: list[dict[str, Any]] = []
    budget_exhausted = False
    while (retry_budget or cursor < len(modules)) and processed < budget:
        retrying = retry_budget > 0
        if retrying:
            retry_budget -= 1
        module = retry_modules.pop(0) if retrying else modules[cursor]
        try:
            version = _go_latest_version(module, timeout)
            escaped = urllib.parse.quote(module, safe="/@")
            url = f"https://proxy.golang.org/{escaped}/@v/{urllib.parse.quote(version, safe='')}.zip"
            module_rows, spent = _go_module_rows(url, module, version, timeout)
            downloaded += spent
            if downloaded > byte_budget:
                budget_exhausted = True
                if retrying:
                    retry_modules.insert(0, module)
                break
            rows.extend(module_rows)
            failures.pop(module, None)
        except Exception as error:
            _record_failure(failures, unavailable, module, error)
            if module in failures and module not in retry_modules:
                retry_modules.append(module)  # queued for the next run, not this one
        if not retrying:
            cursor += 1
        processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            checkpoint(rows, cursor=cursor)
        if interrupted():
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    complete = catalog_complete and cursor >= len(modules) and not failures and not retry_modules
    return {"cursor": cursor, "catalog_size": len(modules), "catalog_complete": catalog_complete,
            "catalog_since": since, "discovered": discovered, "index_requests": index_requests,
            "processed": processed, "records": len(rows), "downloaded_bytes": downloaded,
            "failures": len(failures), "unavailable": len(unavailable),
            "retry_pending": len(retry_modules), "budget_exhausted": budget_exhausted,
            "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_rubygems(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                    checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    catalog_file = Path(state.setdefault("names_file", "data/production/rubygems-names.txt"))
    if not read_catalog(catalog_file):
        body, transfer = fetch("https://rubygems.org/names", timeout)
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        write_catalog(catalog_file, _rubygems_names(body))
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    gems = read_catalog(catalog_file)
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0
    failures, unavailable = _failure_state(state)
    rows: list[dict[str, Any]] = []; budget_exhausted = False
    while cursor < len(gems) and processed < budget:
        package = gems[cursor]
        try:
            metadata_url = "https://rubygems.org/api/v1/gems/" + urllib.parse.quote(package, safe="") + ".json"
            metadata_body, _ = fetch(metadata_url, timeout)
            metadata = json.loads(metadata_body)
            version = metadata.get("version", "unknown")
            artifact_url = metadata.get("gem_uri") or f"https://rubygems.org/gems/{urllib.parse.quote(package, safe='')}-{urllib.parse.quote(version, safe='')}.gem"
            compressed, spent = _gem_metadata(artifact_url, timeout); downloaded += spent
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            repository = metadata.get("source_code_uri") or metadata.get("homepage_uri")
            gemspec = gzip.decompress(compressed).decode("utf-8", "replace") if compressed else ""
            rows.extend(_gem_rows(gemspec, metadata.get("name", package), version, repository, artifact_url))
            failures.pop(package, None)
        except Exception as error:
            _record_failure(failures, unavailable, package, error)
        cursor += 1; processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            checkpoint(rows, cursor=cursor)
        if interrupted():
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    complete = cursor >= len(gems) and not failures
    return {"cursor": cursor, "catalog_size": len(gems), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "unavailable": len(unavailable), "budget_exhausted": budget_exhausted,
            "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}


NUGET_SEARCH = "https://azuresearch-usnc.nuget.org/query"
NUGET_FLAT = "https://api.nuget.org/v3-flatcontainer"
NUGET_PAGE = 1000
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
    except (RegistryCrawlError, urllib.error.HTTPError, OSError, struct.error, zlib.error, KeyError):
        body, transfer = fetch(url, timeout)
        with zipfile.ZipFile(BytesIO(body)) as whole:
            members = [name for name in whole.namelist() if name.rsplit("/", 1)[-1].lower() == "dotnettoolsettings.xml"]
            text = whole.read(members[0]).decode("utf-8", "replace") if members else ""
        return TOOL_COMMAND.findall(text), transfer["downloaded_bytes"]


def _crawl_nuget(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                 checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    """Inspect NuGet's .NET tool packages, the only NuGet packages that ship commands."""
    catalog_file = Path(state.setdefault("tools_file", "data/production/nuget-tools.txt"))
    catalog_file.parent.mkdir(parents=True, exist_ok=True)
    if not read_catalog(catalog_file):
        identifiers: list[str] = []
        skip = 0
        advertised = 0
        while True:
            query = urllib.parse.urlencode({"q": "", "packageType": "DotnetTool",
                                            "take": NUGET_PAGE, "skip": skip, "prerelease": "false"})
            body, _ = fetch(f"{NUGET_SEARCH}?{query}", timeout)
            value = json.loads(body)
            advertised = max(advertised, int(value.get("totalHits") or 0))
            page = value.get("data", [])
            if not page:
                break
            identifiers.extend(item["id"] for item in page if item.get("id"))
            skip += len(page)
        write_catalog(catalog_file, sorted(set(identifiers)))
        # The search endpoint stops paging well before totalHits, so the catalog it
        # yields is a sample, not the tool list; the source must never claim otherwise.
        state["catalog_advertised"] = advertised
        state["catalog_truncated"] = len(set(identifiers)) < advertised
    tools = read_catalog(catalog_file)
    if state.get("catalog_advertised") is None and tools:
        # A catalog built before this check existed carries no verdict, and without one
        # the source would present a sample of the tool list as the whole of it.
        query = urllib.parse.urlencode({"q": "", "packageType": "DotnetTool", "take": 1,
                                        "skip": 0, "prerelease": "false"})
        try:
            body, _ = fetch(f"{NUGET_SEARCH}?{query}", timeout)
            advertised = int(json.loads(body).get("totalHits") or 0)
        except Exception:
            advertised = 0
        state["catalog_advertised"] = advertised
        state["catalog_truncated"] = bool(advertised) and len(tools) < advertised
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0
    failures, unavailable = _failure_state(state)
    rows: list[dict[str, Any]] = []; budget_exhausted = False
    while cursor < len(tools) and processed < budget:
        package = tools[cursor]
        lowered = urllib.parse.quote(package.lower(), safe="")
        try:
            body, _ = fetch(f"{NUGET_FLAT}/{lowered}/index.json", timeout)
            versions = json.loads(body).get("versions", [])
            if not versions:
                raise RegistryCrawlError(f"tool has no published version: {package}")
            version = versions[-1]
            url = f"{NUGET_FLAT}/{lowered}/{urllib.parse.quote(version, safe='')}/{lowered}.{urllib.parse.quote(version, safe='')}.nupkg"
            commands, spent = _nuget_tool_commands(url, timeout)
            downloaded += spent
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            rows.extend(record(command, "nuget", package, version, None, url,
                               source_type="language_package", language="dotnet",
                               registry="nuget", latest_version=version)
                        for command in sorted(set(commands)))
            failures.pop(package, None)
        except Exception as error:
            _record_failure(failures, unavailable, package, error)
        cursor += 1; processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            checkpoint(rows, cursor=cursor)
        if interrupted():
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    truncated = bool(state.get("catalog_truncated"))
    complete = cursor >= len(tools) and not failures and not truncated
    report = {"cursor": cursor, "catalog_size": len(tools), "processed": processed,
              "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
              "unavailable": len(unavailable), "budget_exhausted": budget_exhausted,
              "catalog_truncated": truncated, "catalog_advertised": state.get("catalog_advertised"),
              "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}
    if truncated:
        report["note"] = "NuGet search paging stops short of totalHits; this is a sample of .NET tools"
    return report


def _crawl_packagist(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int,
                     checkpoint: Callable[..., None] = _no_checkpoint) -> dict[str, Any]:
    catalog_file = Path(state.setdefault("packages_file", "data/production/packagist-packages.txt"))
    if not read_catalog(catalog_file):
        body, transfer = fetch("https://packagist.org/packages/list.json", timeout)
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        write_catalog(catalog_file, _packagist_packages(body))
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    packages = read_catalog(catalog_file)
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0
    failures, unavailable = _failure_state(state)
    retry_packages = state.setdefault("retry_packagist", [])
    for package in failures:
        if package not in retry_packages:
            retry_packages.append(package)
    retry_budget = len(retry_packages)  # one attempt per queued package per run
    rows: list[dict[str, Any]] = []; budget_exhausted = False
    while (retry_budget or cursor < len(packages)) and processed < budget:
        retrying = retry_budget > 0
        if retrying:
            retry_budget -= 1
        package = retry_packages.pop(0) if retrying else packages[cursor]
        try:
            metadata_url = "https://repo.packagist.org/p2/" + urllib.parse.quote(package, safe="/") + ".json"
            metadata_body, transfer = fetch(metadata_url, timeout)
            downloaded += transfer["downloaded_bytes"]
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            rows.extend(_packagist_rows(json.loads(metadata_body), package, metadata_url))
            failures.pop(package, None)
        except Exception as error:
            _record_failure(failures, unavailable, package, error)
            if retrying and package not in retry_packages:
                retry_packages.append(package)
        if not retrying:
            cursor += 1
        processed += 1
        if processed % CHECKPOINT_INTERVAL == 0:
            checkpoint(rows, cursor=cursor)
        if budget_exhausted or interrupted():
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    complete = cursor >= len(packages) and not failures and not retry_packages
    return {"cursor": cursor, "catalog_size": len(packages), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "unavailable": len(unavailable), "retry_pending": len(retry_packages),
            "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


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
        "pypi": _crawl_pypi, "npm": _crawl_npm, "crates": _crawl_crates, "go": _crawl_go,
        "rubygems": _crawl_rubygems, "packagist": _crawl_packagist, "nuget": _crawl_nuget,
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

        try:
            report["sources"][source] = runners[source](source_state, observations, budget,
                                                        byte_budget, timeout, checkpoint)
            report["sources"][source]["package_budget"] = budget
            _refuse_empty_exhaustive(report["sources"][source], observations)
        except Exception as error:
            report["sources"][source] = {"status": "failed", "error": str(error), "coverage_kind": "partial"}
            report["status"] = "failed"
        _save_json(state_path, state)  # a later source must not cost this one its cursor
        result = report["sources"].get(source, {})
        if result.get("failures", 0) or result.get("error"):
            report["status"] = "failed"
        if interrupted():
            report["interrupted"] = True
            break
    report["coverage_kind"] = "exhaustive" if report["status"] == "success" and all(v.get("coverage_kind") == "exhaustive" for v in report["sources"].values()) else "partial"
    report["state"] = str(state_path); report["package_budget"] = package_budget; report["byte_budget"] = byte_budget
    _save_json(state_path, state)
    report_path.parent.mkdir(parents=True, exist_ok=True); report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report
