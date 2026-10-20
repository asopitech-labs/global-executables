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
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .collectors import homebrew_metadata, package_files
from .model import write_jsonl


USER_AGENT = "global-executables-production/1.0 (+https://github.com/asopitech-labs/global-executables)"

SOURCE_URLS = {
    "debian": "https://deb.debian.org/debian/dists/stable/main/Contents-amd64.gz",
    "ubuntu": "https://archive.ubuntu.com/ubuntu/dists/noble/Contents-amd64.gz",
    "arch": "https://geo.mirror.pkgbuild.com/core/os/x86_64/core.files",
    "homebrew": "https://formulae.brew.sh/api/formula.json",
    "npm": "https://replicate.npmjs.com/_all_docs",
    "pypi": "https://pypi.org/simple/",
    "crates": "https://index.crates.io/config.json",
}


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
    else:
        text = _arch_text(body)
    rows = package_files(text, source, source_url)
    return rows, {"status": "success", "coverage_kind": "exhaustive", "records": len(rows), "source": source_url}


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


def crawl_source(source: str, output: Path, timeout: int = 300) -> dict[str, Any]:
    if source not in SOURCE_URLS:
        raise ProductionSourceError(f"unknown production source: {source}")
    source_url = SOURCE_URLS[source]
    if source not in {"debian", "ubuntu", "arch", "homebrew"}:
        raise ProductionSourceError(
            f"{source} requires a package-artifact inventory adapter; HTTP metadata alone is not executable evidence"
        )
    body, transfer = fetch(source_url, timeout)
    if source == "homebrew":
        rows, coverage = _crawl_homebrew(body, source_url)
    else:
        rows, coverage = _crawl_os(source, body, source_url)
    write_jsonl(sorted(rows, key=lambda row: (row["command"], row["package"])), output)
    coverage.update(transfer)
    return coverage


def crawl_sources(sources: list[str], output_dir: Path, report_path: Path, timeout: int = 300) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "success",
        "coverage_kind": "exhaustive" if set(sources) <= {"debian", "ubuntu", "arch", "homebrew"} else "partial",
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
