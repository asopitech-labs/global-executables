"""Production-source acquisition with explicit, fail-closed coverage reporting.

The collector downloads upstream indexes, normalizes only executable evidence,
and writes resumable intermediate JSONL files.  It never upgrades a source to
exhaustive merely because an endpoint returned HTTP 200: registry sources that
require package-artifact inspection remain failed until their adapter has
completed that inspection.
"""
from __future__ import annotations

import gzip
import io
import json
import sqlite3
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import zstandard

from .collectors import (homebrew_metadata, package_files, record, scoop_manifests,
                         winget_commands, windows_command)
from .model import write_jsonl


USER_AGENT = "global-executables-production/1.0 (+https://github.com/asopitech-labs/global-executables)"

# A distribution is not one index.  Arch keeps almost everything outside `core`, and
# Debian's stable/main is a fraction of what the archive ships.
SOURCE_INDEXES = {
    "debian": [
        "https://deb.debian.org/debian/dists/stable/main/Contents-amd64.gz",
        "https://deb.debian.org/debian/dists/stable/contrib/Contents-amd64.gz",
        "https://deb.debian.org/debian/dists/stable/non-free/Contents-amd64.gz",
        "https://deb.debian.org/debian/dists/stable/non-free-firmware/Contents-amd64.gz",
        "https://deb.debian.org/debian/dists/sid/main/Contents-amd64.gz",
    ],
    "ubuntu": ["https://archive.ubuntu.com/ubuntu/dists/noble/Contents-amd64.gz"],
    "arch": [
        "https://geo.mirror.pkgbuild.com/core/os/x86_64/core.files",
        "https://geo.mirror.pkgbuild.com/extra/os/x86_64/extra.files",
        "https://geo.mirror.pkgbuild.com/multilib/os/x86_64/multilib.files",
    ],
    # MSYS2 ships Windows binaries through pacman databases, so the Arch reader applies.
    "msys2": [
        "https://repo.msys2.org/msys/x86_64/msys.files",
        "https://repo.msys2.org/mingw/mingw64/mingw64.files",
        "https://repo.msys2.org/mingw/ucrt64/ucrt64.files",
        "https://repo.msys2.org/mingw/clang64/clang64.files",
    ],
    "homebrew": ["https://formulae.brew.sh/api/formula.json"],
    # The official bucket list points at three third-party repositories, so the
    # owner cannot be assumed, and two of them default to `main` rather than `master`.
    "scoop": [f"https://codeload.github.com/{repository}/tar.gz/refs/heads/{branch}"
              for repository, branch in (
                  ("ScoopInstaller/Main", "master"), ("ScoopInstaller/Extras", "master"),
                  ("ScoopInstaller/Versions", "master"), ("ScoopInstaller/Nirsoft", "master"),
                  ("ScoopInstaller/PHP", "master"), ("ScoopInstaller/Java", "master"),
                  ("ScoopInstaller/Nonportable", "master"), ("niheaven/scoop-sysinternals", "main"),
                  ("matthewjberger/scoop-nerd-fonts", "master"), ("Calinou/scoop-games", "master"))],
    "winget": ["https://cdn.winget.microsoft.com/cache/source.msix"],
    # Official Windows images, read as archives rather than run.  Pinned by digest at
    # crawl time so the shipped command set of each release stays reproducible.
    "windows": [f"{repository}:{tag}" for repository, tag in (
        ("windows/servercore", "ltsc2025-amd64"), ("windows/servercore", "ltsc2022-amd64"),
        ("windows/nanoserver", "ltsc2025-amd64"), ("windows/nanoserver", "ltsc2022-amd64"))],
    "npm": ["https://replicate.npmjs.com/_all_docs"],
    "pypi": ["https://pypi.org/simple/"],
    "crates": ["https://index.crates.io/config.json"],
}
SOURCE_URLS = {source: urls[0] for source, urls in SOURCE_INDEXES.items()}
FILE_INDEX_SOURCES = {"debian", "ubuntu", "arch", "msys2"}
COLLECTED_SOURCES = FILE_INDEX_SOURCES | {"homebrew", "scoop", "winget", "windows"}
PACMAN_IDENTITY = {"arch": ("arch", "archlinux"), "msys2": ("windows", "msys2")}


class ProductionSourceError(RuntimeError):
    """A source could not be collected to its declared completeness."""


def fetch(url: str, timeout: int = 300) -> tuple[bytes, dict[str, Any]]:
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return body, {
                "url": url,
                "final_url": response.url,
                "status_code": response.status,
                "downloaded_bytes": len(body),
                "duration_seconds": round(time.monotonic() - started, 3),
            }
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProductionSourceError(f"download failed: {error}") from error


def _arch_text(body: bytes) -> str:
    if body[:4] == b"\x28\xb5\x2f\xfd":  # MSYS2 publishes its pacman databases zstd-compressed
        body = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)).read()
    blocks: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
        for member in archive:
            if not member.isfile() or not member.name.endswith("/files"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            package = member.name.rsplit("/", 1)[0]
            files = handle.read().decode("utf-8", "replace").rstrip("\n")
            blocks.append(f"%NAME%\n{package}\n%FILES%\n{files}")
    return "\n\n".join(blocks)


def _crawl_os(source: str, body: bytes, source_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if source in {"debian", "ubuntu"}:
        text = gzip.decompress(body).decode("utf-8", "replace")
        rows = package_files(text, source, source_url)
    else:
        family, distribution = PACMAN_IDENTITY[source]
        rows = package_files(_arch_text(body), source, source_url,
                             family=family, distribution=distribution)
    return rows, {"status": "success", "coverage_kind": "exhaustive", "records": len(rows), "source": source_url}


MCR = "https://mcr.microsoft.com/v2"
MANIFEST_ACCEPT = ("application/vnd.docker.distribution.manifest.v2+json,"
                   "application/vnd.oci.image.manifest.v1+json")
# Windows layers store the filesystem under Files/.  These are the directories on the
# default PATH, which is what makes a file a command a user can type: System32 alone
# misses WMIC under Wbem, powershell, and the bundled OpenSSH client.
WINDOWS_EXEC_DIRS = ("files/windows/system32/", "files/windows/", "files/windows/syswow64/",
                     "files/windows/system32/wbem/",
                     "files/windows/system32/windowspowershell/v1.0/",
                     "files/windows/system32/openssh/")
WINDOWS_EXEC_SUFFIXES = (".exe", ".com", ".bat", ".cmd")


def _windows_image_rows(repository: str, tag: str, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """List the commands an official Windows image ships, pinned to its digest.

    The base command set is not packaged by anything, so it cannot be read from a
    registry — but a container layer is an ordinary tar, so the image can be inspected
    without running Windows.  The tag moves and the digest does not, so the digest is
    what the evidence cites.
    """
    request = urllib.request.Request(f"{MCR}/{repository}/manifests/{tag}",
                                     headers={"User-Agent": USER_AGENT, "Accept": MANIFEST_ACCEPT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        manifest = json.load(response)
        digest = response.headers.get("Docker-Content-Digest", "")
    reference = f"{repository}@{digest}" if digest else f"{repository}:{tag}"
    commands: dict[str, str] = {}
    downloaded = 0
    for layer in manifest.get("layers", []):
        blob = urllib.request.Request(f"{MCR}/{repository}/blobs/{layer['digest']}",
                                      headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(blob, timeout=timeout) as stream:
            with tarfile.open(fileobj=gzip.GzipFile(fileobj=stream), mode="r|") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    normalised = member.name.replace("\\", "/")
                    path = normalised.lower()
                    if not path.startswith("files/"):
                        continue  # the image also carries a nested UtilityVM tree
                    # A command sits directly in one of these directories, not in a subtree.
                    if path.rsplit("/", 1)[0] + "/" not in WINDOWS_EXEC_DIRS:
                        continue
                    if not path.endswith(WINDOWS_EXEC_SUFFIXES):
                        continue
                    filename = normalised.rsplit("/", 1)[-1]
                    # The image ships ARP.EXE beside attrib.exe; NTFS is case-insensitive,
                    # so the command a user types folds to one name and must collide with
                    # the `arp` every Linux index already carries.
                    commands.setdefault(windows_command(filename).lower(), filename)
        downloaded += layer.get("size", 0)
    rows = [record(command, "windows", repository.rsplit("/", 1)[-1], tag, None, reference,
                   confidence="filesystem", source_type="os_package", package_system="windows-image",
                   distribution_family="windows", distribution=repository.rsplit("/", 1)[-1],
                   image_digest=digest or None, shipped_as=filename)
            for command, filename in sorted(commands.items())]
    return rows, {"status": "success", "coverage_kind": "exhaustive", "records": len(rows),
                  "source": reference, "image_digest": digest, "downloaded_bytes": downloaded,
                  "url": f"{MCR}/{repository}/manifests/{tag}", "final_url": reference,
                  "status_code": 200, "duration_seconds": 0.0}


def _crawl_scoop(body: bytes, source_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a Scoop bucket's manifests from its repository tarball."""
    manifests: list[tuple[str, dict[str, Any]]] = []
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for member in archive:
            if "/bucket/" not in member.name or not member.name.endswith(".json"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            try:
                manifests.append((member.name.rsplit("/", 1)[-1][:-5],
                                  json.loads(handle.read().decode("utf-8", "replace"))))
            except json.JSONDecodeError:
                continue
    if not manifests:
        raise ProductionSourceError(f"scoop bucket carried no manifests: {source_url}")
    rows = scoop_manifests(manifests, source_url)
    return rows, {"status": "success", "coverage_kind": "exhaustive", "records": len(rows),
                  "packages": len(manifests), "source": source_url}


def _crawl_winget(body: bytes, source_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read winget's declared commands from the published source index."""
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith("index.db")]
        if not members:
            raise ProductionSourceError(f"winget source carries no index: {source_url}")
        payload = archive.read(members[0])
    with tempfile.NamedTemporaryFile(suffix=".db") as handle:
        handle.write(payload)
        handle.flush()
        connection = sqlite3.connect(handle.name)
        try:
            pairs = list(connection.execute(
                "select commands.command, ids.id from commands_map"
                " join commands on commands.rowid = commands_map.command"
                " join manifest on manifest.rowid = commands_map.manifest"
                " join ids on ids.rowid = manifest.id"))
        finally:
            connection.close()
    rows = winget_commands(pairs, source_url)
    return rows, {"status": "success", "coverage_kind": "partial", "records": len(rows),
                  "packages": len({package for _, package in pairs}), "source": source_url,
                  "note": "winget declares Commands only for manifests that opt in"}


def _crawl_homebrew(body: bytes, source_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value = json.loads(body)
    rows = homebrew_metadata(value, source_url)
    return rows, {
        "status": "success",
        "coverage_kind": "exhaustive",
        "records": len(rows),
        "packages": len(value) if isinstance(value, list) else None,
        "source": source_url,
    }


def _crawl_index(source: str, body: bytes, source_url: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if source == "homebrew":
        return _crawl_homebrew(body, source_url)
    if source == "scoop":
        return _crawl_scoop(body, source_url)
    if source == "winget":
        return _crawl_winget(body, source_url)
    return _crawl_os(source, body, source_url)


def crawl_source(source: str, output: Path, timeout: int = 300) -> dict[str, Any]:
    if source not in SOURCE_INDEXES:
        raise ProductionSourceError(f"unknown production source: {source}")
    if source not in COLLECTED_SOURCES:
        raise ProductionSourceError(
            f"{source} requires a package-artifact inventory adapter; HTTP metadata alone is not executable evidence"
        )
    rows: list[dict[str, Any]] = []
    indexes: list[dict[str, Any]] = []
    downloaded = 0
    coverage_kind = "exhaustive"
    for source_url in SOURCE_INDEXES[source]:
        if source == "windows":
            repository, _, tag = source_url.rpartition(":")
            index_rows, index_coverage = _windows_image_rows(repository, tag, timeout)
            transfer = {"downloaded_bytes": index_coverage["downloaded_bytes"]}
        else:
            body, transfer = fetch(source_url, timeout)
            index_rows, index_coverage = _crawl_index(source, body, source_url)
        rows.extend(index_rows)
        downloaded += transfer["downloaded_bytes"]
        if index_coverage.get("coverage_kind") != "exhaustive":
            coverage_kind = index_coverage["coverage_kind"]
        indexes.append({**index_coverage, **transfer})
    write_jsonl(sorted(rows, key=lambda row: (row["command"], row["package"])), output)
    return {"status": "success", "coverage_kind": coverage_kind, "records": len(rows),
            "indexes": indexes, "index_count": len(indexes), "downloaded_bytes": downloaded,
            "source": SOURCE_INDEXES[source][0],
            "url": SOURCE_INDEXES[source][0], "final_url": indexes[-1].get("final_url"),
            "status_code": indexes[-1].get("status_code"),
            "duration_seconds": round(sum(i.get("duration_seconds", 0) for i in indexes), 3)}


def crawl_sources(sources: list[str], output_dir: Path, report_path: Path, timeout: int = 300) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "success",
        "coverage_kind": "exhaustive" if set(sources) <= (COLLECTED_SOURCES - {"winget"}) else "partial",
        "declared_sources": sorted(SOURCE_URLS),
        "requested_sources": sorted(sources),
        "sources": {},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        output = output_dir / f"{source}.jsonl"
        try:
            report["sources"][source] = crawl_source(source, output, timeout)
        except ProductionSourceError as error:
            report["sources"][source] = {
                "status": "failed",
                "coverage_kind": "unknown",
                "records": 0,
                "source": SOURCE_URLS.get(source, source),
                "error": str(error),
            }
            report["status"] = "failed"
    report["failed"] = sorted(source for source, value in report["sources"].items() if value["status"] != "success")
    report["uncollected"] = sorted(set(SOURCE_URLS) - set(report["sources"]))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report
