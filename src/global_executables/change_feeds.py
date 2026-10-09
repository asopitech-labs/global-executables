"""Change feeds the Python crawlers poll (ConanCenter).

A feed only decides *what to look at sooner*.  The rotation over the whole catalogue
stays the backstop, so a feed that misses, truncates or lies costs latency, not rows.

The Go crawler reads the PyPI, npm, Packagist and Go-index feeds
(``internal/registryinspect/feeds.go``, ``internal/gocrawl/catalog_refresh.go``).
NuGet's catalog feed is deliberately not polled: see docs/OPERATIONS.md.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
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
