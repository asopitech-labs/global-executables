#!/usr/bin/env python3
"""Build the finite ecosyste.ms critical npm catalog."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


START_URL = "https://packages.ecosyste.ms/critical?registry=npmjs.org&per_page=1000"
PACKAGE_PREFIX = "/registries/npmjs.org/packages/"


class _CatalogPageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.names: set[str] = set()
        self.next_url: str | None = None
        self.last_page = 1

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs = dict(attributes)
        href = attrs.get("href")
        if not href:
            return
        absolute = urljoin(self.base_url, href)
        parsed = urlparse(absolute)
        path = parsed.path
        if path.startswith(PACKAGE_PREFIX):
            self.names.add(unquote(path.removeprefix(PACKAGE_PREFIX)))
        if path == "/critical":
            for value in parse_qs(parsed.query).get("page", []):
                if value.isdigit():
                    self.last_page = max(self.last_page, int(value))
        if attrs.get("aria-label") == "Next":
            self.next_url = absolute


def parse_page(body: str, base_url: str) -> tuple[set[str], str | None, int]:
    parser = _CatalogPageParser(base_url)
    parser.feed(body)
    return parser.names, parser.next_url, parser.last_page


def fetch_catalog(start_url: str = START_URL) -> tuple[list[str], int]:
    names: set[str] = set()
    visited: set[str] = set()
    expected_pages: int | None = None
    per_page = int(parse_qs(urlparse(start_url).query).get("per_page", [0])[0])
    url: str | None = start_url
    while url:
        if url in visited:
            raise RuntimeError(f"pagination loop: {url}")
        visited.add(url)
        request = Request(url, headers={
            "User-Agent": "global-executables/1.0 (+https://github.com/asopitech-labs/global-executables)",
        })
        with urlopen(request, timeout=120) as response:
            page_names, next_url, declared_pages = parse_page(response.read().decode(), response.url)
        if not page_names:
            raise RuntimeError("critical npm page contained no package links")
        if expected_pages is None:
            expected_pages = declared_pages
        if not next_url and per_page and len(page_names) >= per_page:
            raise RuntimeError("critical npm pagination is missing from a full page")
        names.update(page_names)
        url = next_url
    if expected_pages != len(visited):
        raise RuntimeError(f"incomplete critical npm pagination: fetched {len(visited)} of {expected_pages} pages")
    return sorted(names), len(visited)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/production/npm-critical-packages.txt"))
    parser.add_argument("--minimum", type=int, default=1000)
    args = parser.parse_args()
    names, pages = fetch_catalog()
    if len(names) < args.minimum:
        raise SystemExit(f"refusing unexpectedly small npm catalog: {len(names)} < {args.minimum}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text("\n".join(names) + "\n")
    temporary.replace(args.output)
    print(f"packages={len(names)} pages={pages} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
