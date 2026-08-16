"""Resumable artifact crawlers for registry-backed executable evidence.

The crawler is deliberately budgeted.  A source becomes ``exhaustive`` only
after its catalog cursor reaches the end, every selected artifact is inspected,
and no failures remain.  A stopped or rate-limited run remains partial.
"""
from __future__ import annotations

import html
import json
import re
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from .collectors import crates_manifest, npm_metadata, record
from .model import write_jsonl


USER_AGENT = "global-executables-registry-crawl/1.0 (+https://github.com/asopitech-labs/global-executables)"
PROJECT_LINK = re.compile(r"<a\b[^>]*href=[\"'][^\"']+[\"'][^>]*>([^<]+)</a>", re.I)


class RegistryCrawlError(RuntimeError):
    pass


def fetch(url: str, timeout: int = 120) -> tuple[bytes, dict[str, Any]]:
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return body, {"url": url, "status_code": response.status, "downloaded_bytes": len(body),
                      "duration_seconds": round(time.monotonic() - started, 3)}


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


def _append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


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


def _pypi_projects(body: bytes) -> list[str]:
    names = {html.unescape(value).strip() for value in PROJECT_LINK.findall(body.decode("utf-8", "replace"))}
    return sorted(name for name in names if name)


def _crawl_pypi(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    project_file = Path(state.setdefault("projects_file", "data/production/pypi-projects.txt"))
    if not project_file.is_file():
        body, transfer = fetch("https://pypi.org/simple/", timeout)
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text("\n".join(_pypi_projects(body)) + "\n")
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    projects = [line.strip() for line in project_file.read_text().splitlines() if line.strip()]
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0; failures = state.setdefault("failures", {})
    rows: list[dict[str, Any]] = []
    budget_exhausted = False
    while cursor < len(projects) and processed < budget:
        project = projects[cursor]
        try:
            metadata_body, _ = fetch(f"https://pypi.org/pypi/{urllib.parse.quote(project)}/json", timeout)
            metadata = json.loads(metadata_body); info = metadata.get("info", {})
            candidates = [item for item in metadata.get("urls", []) if item.get("packagetype") == "bdist_wheel"]
            candidates.sort(key=lambda item: ("none-any" not in item.get("filename", ""), item.get("filename", "")))
            if not candidates:
                raise RegistryCrawlError("latest release has no wheel; sdist inspection required")
            url = candidates[0]["url"]
            artifact, transfer = fetch(url, timeout)
            downloaded += transfer["downloaded_bytes"]
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            rows.extend(_wheel_rows(artifact, info.get("name", project), info.get("version", "unknown"), info.get("home_page"), url))
            failures.pop(project, None)
        except Exception as error:  # keep the cursor moving; failures block exhaustive status
            failures[project] = str(error)
        cursor += 1; processed += 1
        if budget_exhausted:
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    return {"cursor": cursor, "catalog_size": len(projects), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "budget_exhausted": budget_exhausted,
            "complete": cursor >= len(projects) and not failures,
            "coverage_kind": "exhaustive" if cursor >= len(projects) and not failures else "partial"}


def _crawl_npm(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    since = state.get("since", 0); processed = 0; downloaded = 0; rows: list[dict[str, Any]] = []
    failures = state.setdefault("failures", {})
    budget_exhausted = False
    while processed < budget:
        query = urllib.parse.urlencode({"since": since, "limit": min(100, budget - processed)})
        body, _ = fetch(f"https://replicate.npmjs.com/_changes?{query}", timeout)
        page = json.loads(body); results = page.get("results", [])
        if not results:
            state["complete"] = True
            break
        for change in results:
            change_since = change.get("seq", since); name = change.get("id", "")
            if name.startswith("_"):
                since = change_since
                continue
            try:
                metadata_url = "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@")
                metadata_body, transfer = fetch(metadata_url, timeout)
                downloaded += transfer["downloaded_bytes"]
                if downloaded > byte_budget:
                    budget_exhausted = True
                    break
                rows.extend(npm_metadata(json.loads(metadata_body), metadata_url))
                failures.pop(name, None)
            except Exception as error:
                failures[name] = str(error)
            since = change_since
            processed += 1
            if processed >= budget:
                break
        if budget_exhausted:
            break
        if len(results) < min(100, budget):
            state["complete"] = True
            break
    state["since"] = since
    _append_rows(output, rows)
    complete = bool(state.get("complete")) and not failures
    return {"since": since, "processed": processed, "records": len(rows),
            "downloaded_bytes": downloaded, "failures": len(failures),
            "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_crates(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    page = int(state.get("page", 1)); processed = 0; downloaded = 0; rows: list[dict[str, Any]] = []
    failures = state.setdefault("failures", {})
    budget_exhausted = False
    while processed < budget:
        body, _ = fetch(f"https://crates.io/api/v1/crates?page={page}&per_page=100", timeout)
        crates = json.loads(body).get("crates", [])
        if not crates:
            state["complete"] = True
            break
        page_processed = 0
        for crate in crates:
            name = crate["name"]; version = crate.get("max_stable_version") or crate.get("newest_version")
            try:
                url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/download"
                artifact, transfer = fetch(url, timeout); downloaded += transfer["downloaded_bytes"]
                if downloaded > byte_budget:
                    budget_exhausted = True
                    break
                with tarfile.open(fileobj=BytesIO(artifact), mode="r:gz") as archive:
                    manifest_name = next(name for name in archive.getnames() if name.count("/") == 1 and name.endswith("/Cargo.toml"))
                    manifest = archive.extractfile(manifest_name).read().decode("utf-8", "replace")
                    before_rows = len(rows)
                    rows.extend(crates_manifest(manifest, name, version, crate.get("repository"), url))
                    if len(rows) == before_rows:
                        root = manifest_name.split("/", 1)[0]
                        if f"{root}/src/main.rs" in archive.getnames():
                            rows.append(record(name, "crates", name, version, crate.get("repository"), url,
                                               confidence="inferred", source_type="language_package",
                                               language="rust", registry="crates.io", latest_version=version))
                failures.pop(name, None)
            except Exception as error:
                failures[name] = str(error)
            processed += 1
            page_processed += 1
            if processed >= budget:
                break
        if page_processed == len(crates):
            page += 1
        if budget_exhausted:
            break
    state["page"] = page
    _append_rows(output, rows)
    complete = bool(state.get("complete")) and not failures
    return {"page": page, "processed": processed, "records": len(rows),
            "downloaded_bytes": downloaded, "failures": len(failures),
            "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_go(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    since = state.get("since", "2019-01-01T00:00:00Z")
    processed = 0; downloaded = 0; rows: list[dict[str, Any]] = []
    failures = state.setdefault("failures", {})
    budget_exhausted = False
    while processed < budget:
        query = urllib.parse.urlencode({"limit": min(1000, budget - processed), "since": since})
        body, _ = fetch(f"https://index.golang.org/index?{query}", timeout)
        entries = [json.loads(line) for line in body.decode("utf-8", "replace").splitlines() if line.strip()]
        if not entries:
            state["complete"] = True
            break
        for entry in entries:
            module = entry.get("Path", ""); version = entry.get("Version", ""); timestamp = entry.get("Timestamp", since)
            key = f"{module}@{version}"
            try:
                escaped_module = urllib.parse.quote(module, safe="/@")
                escaped_version = urllib.parse.quote(version, safe="")
                url = f"https://proxy.golang.org/{escaped_module}/@v/{escaped_version}.zip"
                artifact, transfer = fetch(url, timeout); downloaded += transfer["downloaded_bytes"]
                if downloaded > byte_budget:
                    budget_exhausted = True
                    break
                rows.extend(_go_rows(artifact, module, version, url))
                failures.pop(key, None)
            except Exception as error:
                failures[key] = str(error)
            since = timestamp
            processed += 1
            if processed >= budget:
                break
        if budget_exhausted:
            break
        if len(entries) < min(1000, budget):
            state["complete"] = True
            break
    state["since"] = since
    _append_rows(output, rows)
    complete = bool(state.get("complete")) and not failures
    return {"since": since, "processed": processed, "records": len(rows),
            "downloaded_bytes": downloaded, "failures": len(failures),
            "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def crawl_registry_sources(sources: list[str], state_path: Path, output_dir: Path, report_path: Path,
                           package_budget: int = 100, byte_budget: int = 500_000_000,
                           timeout: int = 120) -> dict[str, Any]:
    state = _load_json(state_path, {"version": 1, "sources": {}})
    output_dir.mkdir(parents=True, exist_ok=True); report: dict[str, Any] = {"status": "success", "sources": {}}
    runners: dict[str, Callable[..., dict[str, Any]]] = {
        "pypi": _crawl_pypi, "npm": _crawl_npm, "crates": _crawl_crates, "go": _crawl_go,
    }
    for source in sources:
        if source not in runners:
            raise RegistryCrawlError(f"unsupported registry source: {source}")
        source_state = state["sources"].setdefault(source, {})
        try:
            report["sources"][source] = runners[source](source_state, output_dir / f"{source}.jsonl", package_budget, byte_budget, timeout)
        except Exception as error:
            report["sources"][source] = {"status": "failed", "error": str(error), "coverage_kind": "partial"}
            report["status"] = "failed"
        if report["sources"].get(source, {}).get("failures", 0):
            report["status"] = "failed"
    report["coverage_kind"] = "exhaustive" if report["status"] == "success" and all(v.get("coverage_kind") == "exhaustive" for v in report["sources"].values()) else "partial"
    report["state"] = str(state_path); report["package_budget"] = package_budget; report["byte_budget"] = byte_budget
    _save_json(state_path, state)
    report_path.parent.mkdir(parents=True, exist_ok=True); report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return report
