"""Change feeds the Python crawlers poll (ConanCenter).

A feed only decides *what to look at sooner*.  The rotation over the whole catalogue
stays the backstop, so a feed that misses, truncates or lies costs latency, not rows.

The Go crawler reads the PyPI, npm, Packagist and Go-index feeds
(``internal/registryinspect/feeds.go``, ``internal/gocrawl/catalog_refresh.go``).
NuGet's catalog feed is polled by ``nuget_poll`` below.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

GITHUB_API = "https://api.github.com"
CONAN_REPOSITORY = "conan-io/conan-center-index"
# The compare endpoint returns at most 250 commits and 300 files; a larger difference
# is truncated, so it is reported as a resync rather than trusted.
COMPARE_COMMIT_LIMIT = 250
COMPARE_FILE_LIMIT = 300
_RECIPE_PATH = re.compile(r"^recipes/([^/]+)/(.+)$")

Request = Callable[..., tuple[bytes, dict[str, Any]]]


@dataclass
class FeedPage:
    cursor: str
    names: set[str] = field(default_factory=set)
    # A recipe directory or version list changed: the catalogue should be read again.
    catalog_changed: bool = False
    resync: bool = False
    # NuGet: tools the catalog reports as deleted (the package or one of its versions).
    deleted: set[str] = field(default_factory=set)
    requests: int = 0
    downloaded_bytes: int = 0


def _github_headers(accept: str) -> dict[str, str]:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def conan_poll(request: Request, cursor: str, timeout: int) -> FeedPage:
    """Poll conan-center-index commits since ``cursor`` (a commit SHA).

    ``request(url, timeout, headers=...)`` is `registry_artifact._request_bytes`.  An
    empty cursor only reports the current head: the rotation owns the history before it.
    """
    page = FeedPage(cursor=cursor)

    def head() -> str:
        body, transfer = request(f"{GITHUB_API}/repos/{CONAN_REPOSITORY}/commits/master", timeout,
                                 headers=_github_headers("application/vnd.github.sha"))
        page.requests += 1
        page.downloaded_bytes += transfer["downloaded_bytes"]
        return body.decode("ascii", "replace").strip()

    if not cursor:
        page.cursor = head()
        return page
    body, transfer = request(f"{GITHUB_API}/repos/{CONAN_REPOSITORY}/compare/{cursor}...master?per_page=100",
                             timeout, headers=_github_headers("application/vnd.github+json"))
    page.requests += 1
    page.downloaded_bytes += transfer["downloaded_bytes"]
    comparison = json.loads(body)
    status = comparison.get("status")
    commits = comparison.get("commits") or []
    files = comparison.get("files") or []
    if status == "identical" or (status == "ahead" and not commits):
        return page
    if (status != "ahead" or int(comparison.get("total_commits") or len(commits)) > COMPARE_COMMIT_LIMIT
            or len(files) >= COMPARE_FILE_LIMIT):
        # Force-pushed, rewound or too large to list: forget the history, re-check all.
        page.resync = True
        page.cursor = head()
        return page
    for item in files:
        match = _RECIPE_PATH.match(str(item.get("filename", "")))
        if not match:
            continue
        page.names.add(match.group(1))
        if match.group(2) == "config.yml" or item.get("status") in ("added", "removed", "renamed"):
            page.catalog_changed = True
    page.cursor = str(commits[-1]["sha"])
    return page


NUGET_CATALOG = "https://api.nuget.org/v3/catalog0/index.json"
# More catalog pages than this since the cursor means the crawler was away for days:
# replaying them costs more than re-checking everything, so it resynchronises instead.
NUGET_PAGE_LIMIT = 24
# A catalog commit is acted on only after the flat container has had time to serve it.
NUGET_READY_SECONDS = 300


def nuget_poll(request: Request, cursor: str, timeout: int, tools: dict[str, str],
               now: datetime | None = None) -> FeedPage:
    """Poll the NuGet V3 catalog for commits newer than ``cursor``.

    The cursor is a catalog ``commitTimeStamp`` (they sort as text).  ``tools`` maps a
    lower-cased id to the crawler's own spelling; only those ids are reported.  An empty
    cursor only records the catalog head.  Pages are read oldest first and the cursor is
    the timestamp of the last page read in full, so a failure part-way replays that page.
    """
    now = now or datetime.now(timezone.utc)
    ready = (now - timedelta(seconds=NUGET_READY_SECONDS)).strftime("%Y-%m-%dT%H:%M:%S")
    page = FeedPage(cursor=cursor)

    def get(url: str) -> Any:
        body, transfer = request(url, timeout)
        page.requests += 1
        page.downloaded_bytes += transfer["downloaded_bytes"]
        return json.loads(body)

    index = get(NUGET_CATALOG)
    head = str(index.get("commitTimeStamp") or "")
    if not cursor:
        page.cursor = head
        return page
    newer = sorted((item for item in index.get("items", []) if str(item.get("commitTimeStamp", "")) > cursor),
                   key=lambda item: item["commitTimeStamp"])
    if len(newer) > NUGET_PAGE_LIMIT:
        page.resync = True
        page.cursor = head
        return page
    for item in newer:
        stamp = str(item["commitTimeStamp"])
        if stamp[:19] > ready:
            break  # too young: the next poll reads it
        for leaf in get(str(item["@id"])).get("items", []):
            if str(leaf.get("commitTimeStamp", "")) <= cursor:
                continue
            name = tools.get(str(leaf.get("nuget:id", "")).lower())
            if name is None:
                continue
            page.names.add(name)
            if str(leaf.get("@type", "")).endswith("PackageDelete"):
                page.deleted.add(name)
        page.cursor = stamp
    return page
