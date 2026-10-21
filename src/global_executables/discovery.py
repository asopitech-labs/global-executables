"""Bounded, read-only discovery of commands published by one exact package."""
from __future__ import annotations

import base64
import configparser
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import __version__
from .remote import default_cache_dir

CONTRACT_VERSION = "package-discovery-v1"
LATEST_TTL_SECONDS = 300
EXACT_TTL_SECONDS = 86_400
NEGATIVE_TTL_SECONDS = 60
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXPANDED_BYTES = 100 * 1024 * 1024
USER_AGENT = f"global-executables/{__version__} (+https://github.com/asopitech-labs/global-executables)"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


Fetcher = Callable[[urllib.request.Request, float, int], Response]


class AcquisitionError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: str | None = None, source_url: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.source_url = source_url


class VerificationError(AcquisitionError):
    """The fetched evidence is unsafe, inconsistent, or does not verify the claim."""


def _fetch(request: urllib.request.Request, timeout: float, limit: int) -> Response:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise VerificationError(f"response exceeds {limit} bytes", source_url=request.full_url)
            return Response(
                getattr(response, "status", 200),
                {key.lower(): value for key, value in response.headers.items()},
                body,
                response.geturl(),
            )
    except urllib.error.HTTPError as error:
        return Response(
            error.code,
            {key.lower(): value for key, value in error.headers.items()},
            b"",
            error.geturl(),
        )
    except (OSError, TimeoutError) as error:
        raise AcquisitionError(str(error)) from error


def _safe_url(url: str, hosts: set[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise VerificationError(f"unsafe upstream URL: {url}", source_url=url)
    if (parsed.hostname or "").lower() not in hosts:
        raise VerificationError(f"upstream host is not allowed: {parsed.hostname}", source_url=url)


def _request(fetcher: Fetcher, url: str, hosts: set[str], timeout: float, limit: int,
             headers: dict[str, str] | None = None) -> Response:
    current = url
    for _ in range(4):
        _safe_url(current, hosts)
        request = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, **(headers or {})})
        response = fetcher(request, timeout, limit)
        _safe_url(response.url, hosts)
        if len(response.body) > limit:
            raise VerificationError(f"response exceeds {limit} bytes", source_url=response.url)
        if response.status not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            raise VerificationError("upstream redirect omits Location", source_url=current)
        current = urllib.parse.urljoin(current, location)
    raise VerificationError("too many upstream redirects", source_url=current)


def _utc(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts and bool(path.parts)


def _digest(body: bytes, algorithm: str, expected: str | None) -> str:
    actual = hashlib.new(algorithm, body).hexdigest()
    if expected and actual.lower() != expected.lower():
        raise VerificationError(f"artifact {algorithm} digest mismatch")
    return f"{algorithm}:{actual}"


def _normalized_pypi(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _npm_commands(body: bytes, metadata: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    distribution = metadata.get("dist", {})
    _digest(body, "sha1", distribution.get("shasum"))
    integrity = distribution.get("integrity")
    if isinstance(integrity, str) and integrity.startswith("sha512-"):
        actual = base64.b64encode(hashlib.sha512(body).digest()).decode()
        if actual != integrity.removeprefix("sha512-"):
            raise VerificationError("artifact sha512 integrity mismatch")
    digest = _digest(body, "sha256", None)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS or sum(item.size for item in members) > MAX_EXPANDED_BYTES:
            raise VerificationError("npm artifact exceeds archive expansion limits")
        names = {item.name for item in members if item.isfile() and _safe_member(item.name)}
        package_info = archive.getmember("package/package.json")
        package_member = archive.extractfile(package_info) if package_info.isfile() else None
        if package_member is None or not _safe_member(package_info.name):
            raise VerificationError("npm artifact has no package/package.json")
        manifest = json.load(package_member)
    except (tarfile.TarError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise VerificationError(f"invalid npm artifact: {error}") from error
    if manifest.get("name") != metadata.get("name") or manifest.get("version") != metadata.get("version"):
        raise VerificationError("npm artifact identity or version differs from registry metadata")
    declared = manifest.get("bin", {})
    if isinstance(declared, str):
        declared = {manifest.get("name", metadata.get("name")): declared}
    if not isinstance(declared, dict):
        raise VerificationError("npm bin declaration is not an object or string")
    commands = []
    for command, target in sorted(declared.items()):
        if not isinstance(command, str) or not isinstance(target, str):
            continue
        normalized = str(PurePosixPath("package") / target.removeprefix("./"))
        if not _safe_member(normalized) or normalized not in names:
            raise VerificationError(f"npm bin target is absent from artifact: {target}")
        commands.append({"command": command, "entry_point": target})
    return commands, digest


def _wheel_tags(filename: str) -> dict[str, str] | None:
    if not filename.endswith(".whl"):
        return None
    parts = filename[:-4].rsplit("-", 3)
    if len(parts) != 4:
        return None
    return {"python_tag": parts[1], "abi_tag": parts[2], "platform_tag": parts[3]}


def _compatible(filename: str, target: dict[str, Any]) -> bool:
    tags = _wheel_tags(filename)
    if tags is None:
        return False
    return all(not target.get(key) or target[key] in value.split(".") for key, value in tags.items())


def _pypi_commands(body: bytes, expected: str | None, package: str,
                   version: str) -> tuple[list[dict[str, str]], str]:
    digest = _digest(body, "sha256", expected)
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS or sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
                raise VerificationError("wheel exceeds archive expansion limits")
            names = {item.filename for item in infos if not item.is_dir() and _safe_member(item.filename)}
            metadata_files = sorted(name for name in names if name.endswith(".dist-info/METADATA"))
            if len(metadata_files) != 1:
                raise VerificationError("wheel must contain exactly one dist-info/METADATA")
            artifact_metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
            if (_normalized_pypi(artifact_metadata.get("Name", "")) != _normalized_pypi(package)
                    or artifact_metadata.get("Version") != version):
                raise VerificationError("wheel identity or version differs from registry metadata")
            candidates = sorted(name for name in names if name.endswith(".dist-info/entry_points.txt"))
            if not candidates:
                return [], digest
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read_string(archive.read(candidates[0]).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, configparser.Error) as error:
        raise VerificationError(f"invalid wheel artifact: {error}") from error
    commands = []
    for command, declaration in parser.items("console_scripts") if parser.has_section("console_scripts") else []:
        module = declaration.split("=", 1)[0].strip().split(":", 1)[0].strip()
        module_path = module.replace(".", "/")
        if f"{module_path}.py" not in names and f"{module_path}/__init__.py" not in names:
            raise VerificationError(f"console_scripts target is absent from wheel: {declaration}")
        commands.append({"command": command.strip(), "entry_point": declaration.strip()})
    return sorted(commands, key=lambda item: item["command"]), digest


class PackageDiscovery:
    """One shared synchronous engine used by the CLI and MCP tool."""

    def __init__(self, cache_dir: str | Path | None = None, *, offline: bool = False,
                 timeout: float = 20.0, fetcher: Fetcher | None = None,
                 clock: Callable[[], float] = time.time):
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir() / "package-discovery-v1"
        self.offline = offline
        self.timeout = timeout
        self.fetcher = fetcher or _fetch
        self.clock = clock
        self._lock = threading.RLock()

    def _path(self, query: dict[str, Any]) -> Path:
        key = hashlib.sha256(json.dumps(query, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _load(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def discover(self, ecosystem: str, package: str, version: str | None = None,
                 command: str | None = None, target: dict[str, Any] | None = None,
                 *, revalidate: bool = False) -> dict[str, Any]:
        ecosystem = ecosystem.strip().lower()
        package = package.strip()
        target = target or {}
        query = {"ecosystem": ecosystem, "package": package, "version": version,
                 "command": command, "target": target, "verifier": CONTRACT_VERSION}
        if not package or any(character in package for character in "\r\n\x00"):
            raise ValueError("package must be a non-empty registry package ID")
        if ecosystem not in {"npm", "pypi"}:
            return self._base(query, "unsupported", [], [f"unsupported ecosystem: {ecosystem}"])
        path = self._path(query)
        with self._lock:
            now = self.clock()
            cached = self._load(path)
            if cached and not revalidate and now < cached["expires_at"]:
                result = dict(cached["result"])
                result["cache"] = {"status": "fresh", "age_seconds": max(0, int(now - cached["stored_at"]))}
                return result
            if self.offline:
                result = self._base(query, "temporarily_unavailable", [], ["offline mode"])
                if cached:
                    result["historical_observation"] = cached["result"]
                return result
            try:
                result, validators = self._acquire(query, cached)
            except AcquisitionError as error:
                outcome = "unverified" if isinstance(error, VerificationError) else "temporarily_unavailable"
                result = self._base(query, outcome, [], [str(error)])
                result["retry_after"] = error.retry_after
                if error.source_url:
                    result["source_urls"] = [error.source_url]
                if cached:
                    result["cache"] = {"status": "failed_revalidation",
                                       "age_seconds": max(0, int(now - cached["stored_at"]))}
                    result["historical_observation"] = cached["result"]
                return result
            ttl = NEGATIVE_TTL_SECONDS if result["outcome"] == "not_published" else (
                EXACT_TTL_SECONDS if version else LATEST_TTL_SECONDS)
            self._write(path, {"stored_at": now, "expires_at": now + ttl, "result": result, **validators})
            result["cache"] = {"status": "revalidated" if cached else "miss", "age_seconds": 0}
            return result

    def _base(self, query: dict[str, Any], outcome: str, commands: list[dict[str, str]],
              unresolved: list[str]) -> dict[str, Any]:
        return {"contract_version": CONTRACT_VERSION, "outcome": outcome,
                "ecosystem": query["ecosystem"], "package": query["package"],
                "requested_version": query["version"], "requested_command": query["command"],
                "target": query["target"], "commands": commands, "unresolved": unresolved,
                "checked_at": _utc(self.clock()), "publication_status": "not_indexed",
                "verification_complete": False,
                "cache": {"status": "miss", "age_seconds": None}}

    def _acquire(self, query: dict[str, Any], cached: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        ecosystem, package, version = query["ecosystem"], query["package"], query["version"]
        quoted = urllib.parse.quote(package, safe="@")
        if ecosystem == "npm":
            selector = urllib.parse.quote(version or "latest", safe="")
            metadata_url = f"https://registry.npmjs.org/{quoted}/{selector}"
            metadata_hosts = artifact_hosts = {"registry.npmjs.org"}
        else:
            suffix = f"/{urllib.parse.quote(version, safe='')}" if version else ""
            metadata_url = f"https://pypi.org/pypi/{quoted}{suffix}/json"
            metadata_hosts, artifact_hosts = {"pypi.org"}, {"files.pythonhosted.org"}
        headers = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        response = _request(self.fetcher, metadata_url, metadata_hosts, self.timeout,
                            MAX_METADATA_BYTES, headers)
        if response.status == 304 and cached:
            result = dict(cached["result"])
            result["checked_at"] = _utc(self.clock())
            return result, {key: cached.get(key) for key in ("etag", "last_modified")}
        if response.status in {404, 410}:
            result = self._base(query, "not_published", [], [f"authoritative HTTP {response.status}"])
            result["metadata_url"] = metadata_url
            result["verification_complete"] = True
            return result, {}
        if response.status == 451:
            raise AcquisitionError("registry access is legally restricted", status=451,
                                   source_url=metadata_url)
        if response.status == 429 or response.status >= 500:
            raise AcquisitionError(f"temporary registry HTTP {response.status}", status=response.status,
                                   retry_after=response.headers.get("retry-after"), source_url=metadata_url)
        if response.status != 200:
            raise AcquisitionError(f"unexpected registry HTTP {response.status}", status=response.status,
                                   source_url=metadata_url)
        try:
            metadata = json.loads(response.body)
        except (ValueError, UnicodeDecodeError) as error:
            raise AcquisitionError(f"invalid registry metadata: {error}") from error
        if ecosystem == "npm":
            canonical = metadata.get("name")
            selected_version = metadata.get("version")
            artifact_url = metadata.get("dist", {}).get("tarball")
            if not canonical or not selected_version or not artifact_url:
                raise VerificationError("npm metadata omits package identity, version, or tarball",
                                        source_url=metadata_url)
            if canonical.lower() != package.lower() or (version and selected_version != version):
                raise VerificationError("npm metadata identity or version differs from the request",
                                        source_url=metadata_url)
            artifact = _request(self.fetcher, artifact_url, artifact_hosts, self.timeout, MAX_ARTIFACT_BYTES)
            if artifact.status != 200:
                error_type = VerificationError if artifact.status in {404, 410} else AcquisitionError
                raise error_type(f"artifact HTTP {artifact.status}", status=artifact.status,
                                 retry_after=artifact.headers.get("retry-after"), source_url=artifact_url)
            commands, digest = _npm_commands(artifact.body, metadata)
        else:
            canonical = metadata.get("info", {}).get("name")
            selected_version = metadata.get("info", {}).get("version")
            if not canonical or not selected_version:
                raise VerificationError("PyPI metadata omits package identity or version",
                                        source_url=metadata_url)
            if (_normalized_pypi(canonical) != _normalized_pypi(package)
                    or (version and selected_version != version)):
                raise VerificationError("PyPI metadata identity or version differs from the request",
                                        source_url=metadata_url)
            compatible = [item for item in metadata.get("urls", [])
                          if item.get("packagetype") == "bdist_wheel"
                          and _compatible(item.get("filename", ""), query["target"])]
            wheels = [item for item in compatible if not item.get("yanked")]
            if compatible and not wheels:
                result = self._base(query, "unverified", [], ["all target-compatible wheels are yanked"])
                result.update({"canonical_package": canonical, "version": selected_version,
                               "metadata_url": metadata_url, "yanked": True,
                               "verification_complete": True})
                return result, {"etag": response.headers.get("etag"),
                                "last_modified": response.headers.get("last-modified")}
            if not wheels:
                result = self._base(query, "unverified", [], ["no target-compatible wheel"])
                result.update({"canonical_package": canonical, "version": selected_version,
                               "metadata_url": metadata_url})
                return result, {"etag": response.headers.get("etag"),
                                "last_modified": response.headers.get("last-modified")}
            wheel = sorted(wheels, key=lambda item: item["filename"])[0]
            artifact_url = wheel["url"]
            artifact = _request(self.fetcher, artifact_url, artifact_hosts, self.timeout, MAX_ARTIFACT_BYTES)
            if artifact.status != 200:
                error_type = VerificationError if artifact.status in {404, 410} else AcquisitionError
                raise error_type(f"artifact HTTP {artifact.status}", status=artifact.status,
                                 retry_after=artifact.headers.get("retry-after"), source_url=artifact_url)
            commands, digest = _pypi_commands(
                artifact.body, wheel.get("digests", {}).get("sha256"), canonical, selected_version)
        requested = query["command"]
        matched = not requested or any(item["command"] == requested for item in commands)
        outcome = "verified_entry_point" if commands and matched else "unverified"
        unresolved = [] if outcome == "verified_entry_point" else ([
            f"requested command is not declared: {requested}"] if requested else ["release declares no installed commands"])
        result = self._base(query, outcome, commands, unresolved)
        result.update({"canonical_package": canonical, "version": selected_version,
                       "metadata_url": metadata_url, "artifact_url": artifact_url,
                       "artifact_digest": digest, "verification_method": (
                           "npm-package-json-bin-and-tar-member" if ecosystem == "npm"
                           else "pypi-wheel-console-scripts-and-module-member"),
                       "verification_complete": True})
        return result, {"etag": response.headers.get("etag"),
                        "last_modified": response.headers.get("last-modified")}
