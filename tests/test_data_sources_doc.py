"""Every host a collector, crawler, tool or workflow reaches is listed in docs/DATA_SOURCES.md."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PAGE = ROOT / "docs/DATA_SOURCES.md"
SCANNED = ["src", "tools", "cmd", "internal", ".github/workflows", "playground"]
SCANNED_FILES = ["server.json", *(path.name for path in ROOT.glob("Dockerfile*"))]
SUFFIXES = {".py", ".go", ".sh", ".yml", ".yaml", ".js", ".html", ".json"}
# Placeholders and container-local names, never fetched from the internet.
NOT_A_SOURCE = {"mcp", "localhost", "127.0.0.1", "example.com", "example.test", "example.invalid"}
URL = re.compile(r"https?://([A-Za-z0-9.-]+)")
# The source distribution ships neither docs/ nor tools/, cmd/ and internal/.
pytestmark = pytest.mark.skipif(not PAGE.is_file(), reason="needs a full repository checkout")


def _files():
    for directory in SCANNED:
        for path in sorted((ROOT / directory).rglob("*")):
            if (path.is_file() and path.suffix in SUFFIXES and "__pycache__" not in path.parts
                    and not path.name.endswith("_test.go")):
                yield path
    for name in SCANNED_FILES:
        if (ROOT / name).is_file():
            yield ROOT / name


def hosts() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in _files():
        for host in URL.findall(path.read_text(errors="replace")):
            host = host.lower().rstrip(".")
            if host not in NOT_A_SOURCE and not host.endswith((".test", ".invalid", ".example")):
                found.setdefault(host, str(path.relative_to(ROOT)))
    return found


def test_every_fetched_host_is_listed_on_the_data_sources_page():
    page = PAGE.read_text()
    def listed(host: str) -> bool:
        # A whole host name, so `crates.io` is not satisfied by `static.crates.io`.
        return re.search(rf"(?<![A-Za-z0-9.-]){re.escape(host)}(?![A-Za-z0-9-])", page) is not None

    missing = {host: where for host, where in hosts().items() if not listed(host)}
    assert not missing, f"hosts missing from docs/DATA_SOURCES.md: {missing}"


def test_scan_finds_the_known_collectors():
    found = hosts()
    for host in ("deb.debian.org", "registry.npmjs.org", "packages.ecosyste.ms", "proxy.golang.org"):
        assert host in found
