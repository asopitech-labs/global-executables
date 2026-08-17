"""Resumable artifact crawlers for registry-backed executable evidence.

The crawler is deliberately budgeted.  A source becomes ``exhaustive`` only
after its catalog cursor reaches the end, every selected artifact is inspected,
and no failures remain.  A stopped or rate-limited run remains partial.
"""
from __future__ import annotations

import html
import json
import gzip
import re
import tarfile
import time
import tomllib
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
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
        elif "HTTP Error 404" in text:
            unavailable[key] = text
            failures.pop(key, None)
    return failures, unavailable


def _record_failure(failures: dict[str, str], unavailable: dict[str, str], key: str, error: Exception) -> None:
    message = str(error)
    if isinstance(error, urllib.error.HTTPError) and error.code == 404:
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


def _crate_index_path(name: str) -> str:
    lowered = name.lower()
    if len(lowered) == 1:
        return f"1/{lowered}"
    if len(lowered) == 2:
        return f"2/{lowered}"
    if len(lowered) == 3:
        return f"3/{lowered[0]}/{lowered}"
    return f"{lowered[:2]}/{lowered[2:4]}/{lowered}"


def _crate_latest_version(name: str, timeout: int) -> str:
    body, _ = fetch(f"https://index.crates.io/{_crate_index_path(name)}", timeout)
    for line in reversed(body.decode("utf-8", "replace").splitlines()):
        if line.strip():
            value = json.loads(line)
            if not value.get("yanked"):
                return value["vers"]
    raise RegistryCrawlError(f"crate has no non-yanked version: {name}")


def _crate_download(name: str, version: str, timeout: int) -> tuple[bytes, dict[str, Any], str]:
    api_url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/download"
    try:
        artifact, transfer = fetch(api_url, timeout)
        return artifact, transfer, api_url
    except urllib.error.HTTPError as error:
        if error.code not in {403, 429, 500, 502, 503, 504}:
            raise
        url = f"https://static.crates.io/crates/{urllib.parse.quote(name)}/{urllib.parse.quote(name)}-{urllib.parse.quote(version)}.crate"
        artifact, transfer = fetch(url, timeout)
        return artifact, transfer, url


def _crawl_pypi(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    project_file = Path(state.setdefault("projects_file", "data/production/pypi-projects.txt"))
    if not project_file.is_file():
        body, transfer = fetch("https://pypi.org/simple/", timeout)
        project_file.parent.mkdir(parents=True, exist_ok=True)
        project_file.write_text("\n".join(_pypi_projects(body)) + "\n")
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    projects = [line.strip() for line in project_file.read_text().splitlines() if line.strip()]
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0
    failures, unavailable = _failure_state(state)
    retry_projects = state.setdefault("retry_projects", [])
    rows: list[dict[str, Any]] = []
    budget_exhausted = False
    while (retry_projects or cursor < len(projects)) and processed < budget:
        retrying = bool(retry_projects)
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
                continue
            url = candidates[0]["url"]
            artifact, transfer = fetch(url, timeout)
            downloaded += transfer["downloaded_bytes"]
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            package = info.get("name", project); version = info.get("version", "unknown")
            if candidates[0].get("packagetype") == "bdist_wheel":
                rows.extend(_wheel_rows(artifact, package, version, info.get("home_page"), url))
            else:
                rows.extend(_sdist_rows(artifact, package, version, info.get("home_page"), url))
            failures.pop(project, None)
        except Exception as error:  # keep the cursor moving; failures block exhaustive status
            _record_failure(failures, unavailable, project, error)
        if not retrying:
            cursor += 1
        processed += 1
        if budget_exhausted:
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    return {"cursor": cursor, "catalog_size": len(projects), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "budget_exhausted": budget_exhausted,
            "unavailable": len(unavailable), "retry_pending": len(retry_projects),
            "complete": cursor >= len(projects) and not failures and not retry_projects,
            "coverage_kind": "exhaustive" if cursor >= len(projects) and not failures and not retry_projects else "partial"}


def _crawl_npm(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    since = state.get("since", 0); processed = 0; downloaded = 0; rows: list[dict[str, Any]] = []
    failures, unavailable = _failure_state(state)
    retry_packages = state.setdefault("retry_packages", [])
    for package in failures:
        if package not in retry_packages:
            retry_packages.append(package)
    retry_budget = len(retry_packages)
    budget_exhausted = False
    while processed < budget:
        if retry_budget:
            retry_budget -= 1
            name = retry_packages.pop(0)
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
                _record_failure(failures, unavailable, name, error)
                retry_packages.append(name)
            processed += 1
            if budget_exhausted:
                break
            continue
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
                _record_failure(failures, unavailable, name, error)
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
    complete = bool(state.get("complete")) and not failures and not retry_packages
    return {"since": since, "processed": processed, "records": len(rows),
            "downloaded_bytes": downloaded, "failures": len(failures), "unavailable": len(unavailable),
            "retry_pending": len(retry_packages), "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_crates(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    page = int(state.get("page", 1)); processed = 0; downloaded = 0; rows: list[dict[str, Any]] = []
    failures, unavailable = _failure_state(state)
    retry_crates = state.setdefault("retry_crates", [])
    for crate_name in failures:
        if crate_name not in retry_crates:
            retry_crates.append(crate_name)
    retry_budget = len(retry_crates)
    budget_exhausted = False
    while processed < budget:
        if retry_budget:
            retry_budget -= 1
            name = retry_crates.pop(0)
            try:
                version = _crate_latest_version(name, timeout)
                artifact, transfer, url = _crate_download(name, version, timeout)
                downloaded += transfer["downloaded_bytes"]
                if downloaded > byte_budget:
                    budget_exhausted = True
                    break
                with tarfile.open(fileobj=BytesIO(artifact), mode="r:gz") as archive:
                    manifest_name = next(member for member in archive.getnames()
                                         if member.count("/") == 1 and member.endswith("/Cargo.toml"))
                    manifest = archive.extractfile(manifest_name).read().decode("utf-8", "replace")
                    rows.extend(crates_manifest(manifest, name, version, None, url))
                failures.pop(name, None)
            except Exception as error:
                _record_failure(failures, unavailable, name, error)
                retry_crates.append(name)
            processed += 1
            if budget_exhausted:
                break
            continue
        body, _ = fetch(f"https://crates.io/api/v1/crates?page={page}&per_page=100", timeout)
        crates = json.loads(body).get("crates", [])
        if not crates:
            state["complete"] = True
            break
        page_processed = 0
        for crate in crates:
            name = crate["name"]; version = crate.get("max_stable_version") or crate.get("newest_version")
            try:
                artifact, transfer, url = _crate_download(name, version, timeout)
                downloaded += transfer["downloaded_bytes"]
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
                _record_failure(failures, unavailable, name, error)
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
    complete = bool(state.get("complete")) and not failures and not retry_crates
    return {"page": page, "processed": processed, "records": len(rows),
            "downloaded_bytes": downloaded, "failures": len(failures), "unavailable": len(unavailable),
            "retry_pending": len(retry_crates), "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_go(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    since = state.get("since", "2019-01-01T00:00:00Z")
    processed = 0; downloaded = 0; rows: list[dict[str, Any]] = []
    failures, unavailable = _failure_state(state)
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
                _record_failure(failures, unavailable, key, error)
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
            "downloaded_bytes": downloaded, "failures": len(failures), "unavailable": len(unavailable),
            "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_rubygems(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    catalog_file = Path(state.setdefault("names_file", "data/production/rubygems-names.txt"))
    if not catalog_file.is_file():
        body, transfer = fetch("https://rubygems.org/names", timeout)
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_text("\n".join(_rubygems_names(body)) + "\n")
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    gems = [line.strip() for line in catalog_file.read_text().splitlines() if line.strip()]
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
            artifact, transfer = fetch(artifact_url, timeout); downloaded += transfer["downloaded_bytes"]
            if downloaded > byte_budget:
                budget_exhausted = True
                break
            repository = metadata.get("source_code_uri") or metadata.get("homepage_uri")
            rows.extend(_ruby_gem_rows(artifact, metadata.get("name", package), version, repository, artifact_url))
            failures.pop(package, None)
        except Exception as error:
            _record_failure(failures, unavailable, package, error)
        cursor += 1; processed += 1
    state["cursor"] = cursor
    _append_rows(output, rows)
    complete = cursor >= len(gems) and not failures
    return {"cursor": cursor, "catalog_size": len(gems), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "unavailable": len(unavailable), "budget_exhausted": budget_exhausted,
            "complete": complete, "coverage_kind": "exhaustive" if complete else "partial"}


def _crawl_packagist(state: dict[str, Any], output: Path, budget: int, byte_budget: int, timeout: int) -> dict[str, Any]:
    catalog_file = Path(state.setdefault("packages_file", "data/production/packagist-packages.txt"))
    if not catalog_file.is_file():
        body, transfer = fetch("https://packagist.org/packages/list.json", timeout)
        catalog_file.parent.mkdir(parents=True, exist_ok=True)
        catalog_file.write_text("\n".join(_packagist_packages(body)) + "\n")
        state["catalog_bytes"] = transfer["downloaded_bytes"]
    packages = [line.strip() for line in catalog_file.read_text().splitlines() if line.strip()]
    cursor = int(state.get("cursor", 0)); processed = 0; downloaded = 0
    failures, unavailable = _failure_state(state)
    retry_packages = state.setdefault("retry_packagist", [])
    for package in failures:
        if package not in retry_packages:
            retry_packages.append(package)
    rows: list[dict[str, Any]] = []; budget_exhausted = False
    while (retry_packages or cursor < len(packages)) and processed < budget:
        retrying = bool(retry_packages)
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
        if budget_exhausted:
            break
    state["cursor"] = cursor
    _append_rows(output, rows)
    complete = cursor >= len(packages) and not failures and not retry_packages
    return {"cursor": cursor, "catalog_size": len(packages), "processed": processed,
            "records": len(rows), "downloaded_bytes": downloaded, "failures": len(failures),
            "unavailable": len(unavailable), "retry_pending": len(retry_packages),
            "budget_exhausted": budget_exhausted, "complete": complete,
            "coverage_kind": "exhaustive" if complete else "partial"}


def crawl_registry_sources(sources: list[str], state_path: Path, output_dir: Path, report_path: Path,
                           package_budget: int = 100, byte_budget: int = 500_000_000,
                           timeout: int = 120) -> dict[str, Any]:
    state = _load_json(state_path, {"version": 1, "sources": {}})
    output_dir.mkdir(parents=True, exist_ok=True); report: dict[str, Any] = {"status": "success", "sources": {}}
    runners: dict[str, Callable[..., dict[str, Any]]] = {
        "pypi": _crawl_pypi, "npm": _crawl_npm, "crates": _crawl_crates, "go": _crawl_go,
        "rubygems": _crawl_rubygems, "packagist": _crawl_packagist,
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
