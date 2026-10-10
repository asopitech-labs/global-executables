"""Change-driven refresh for the Python crawlers: NuGet, ConanCenter, crates.io.

Every test drives the real crawl functions with a recorded-shape registry and counts
what they request, so the savings in docs/OPERATIONS.md are a property of the code.
"""
import json
import urllib.error
from datetime import timedelta
from pathlib import Path

import pytest

from global_executables import change_feeds, refresh_policy, registry_artifact
from global_executables.registry_state import load_state, save_state


class Clock:
    def __init__(self, monkeypatch):
        self.now = registry_artifact._utc_now()
        monkeypatch.setattr(registry_artifact, "_utc_now", lambda: self.now)

    def advance(self, days):
        self.now += timedelta(days=days)


class FakeNuGet:
    """index.json per tool plus a nupkg download that costs `nupkg_bytes`."""

    nupkg_bytes = 4_000_000

    def __init__(self, monkeypatch, versions):
        self.versions = dict(versions)
        self.index_requests = 0
        self.nupkg_requests = 0
        self.bytes = 0
        monkeypatch.setattr(registry_artifact, "fetch", self.fetch)
        monkeypatch.setattr(registry_artifact, "_nuget_tool_commands", self.nupkg)

    def fetch(self, url, timeout=120, attempts=4):
        name = url.split("/v3-flatcontainer/")[1].split("/")[0]
        original = next((tool for tool in self.versions if tool.lower() == name), None)
        if original is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        self.index_requests += 1
        body = json.dumps({"versions": [self.versions[original]]}).encode()
        self.bytes += len(body)
        return body, {"downloaded_bytes": len(body)}

    def nupkg(self, url, timeout):
        self.nupkg_requests += 1
        self.bytes += self.nupkg_bytes
        return ["cmd-" + url.rsplit("/", 1)[1].split(".")[0]], self.nupkg_bytes


def nuget_state(tmp_path, tools):
    catalog = tmp_path / "tools.txt"
    registry_artifact.write_catalog(catalog, tools)
    return {"tools_file": str(catalog), "cursor": len(tools), "catalog_advertised": len(tools), "catalog_truncated": False}


def test_nuget_unchanged_version_skips_the_package_download_and_keeps_rows(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, {"alpha": "1.0.0", "beta": "2.0.0"})
    state = nuget_state(tmp_path, ["alpha", "beta"])
    output = tmp_path / "nuget.jsonl"

    first = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert registry.nupkg_requests == 2 and first["unchanged"] == 0
    rows_before = output.read_text()

    clock.advance(2)
    registry.index_requests = registry.nupkg_requests = 0
    registry.bytes = 0
    second = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert registry.nupkg_requests == 0, "an unchanged tool must not be downloaded again"
    assert registry.index_requests == 2 and second["unchanged"] == 2
    assert output.read_text() == rows_before
    assert registry.bytes < 1000, "only the two small index documents were read"
    assert state["extraction_revision"] == refresh_policy.EXTRACTION_REVISION


def test_nuget_changed_version_replaces_rows_and_restarts_backoff(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, {"alpha": "1.0.0"})
    state = nuget_state(tmp_path, ["alpha"])
    output = tmp_path / "nuget.jsonl"
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    clock.advance(2)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert refresh_policy.unpack(state["checked"]["alpha"])[1] == 1

    registry.versions["alpha"] = "1.1.0"
    clock.advance(4)
    registry.nupkg_requests = 0
    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert registry.nupkg_requests == 1 and report["unchanged"] == 0
    assert {row["version"] for row in rows} == {"1.1.0"}
    assert refresh_policy.unpack(state["checked"]["alpha"]) == (refresh_policy.today(clock.now), 0, "1.1.0")


def test_nuget_rotation_skips_packages_that_are_not_due_without_spending_budget(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    tools = [f"tool{index:02d}" for index in range(30)]
    registry = FakeNuGet(monkeypatch, {tool: "1.0.0" for tool in tools})
    state = nuget_state(tmp_path, tools)
    output = tmp_path / "nuget.jsonl"
    registry_artifact._crawl_nuget(state, output, 100, 10**9, 120)
    day = refresh_policy.today(clock.now)
    # Twenty packages were checked today after a long unchanged streak; ten are overdue.
    for tool in tools[:20]:
        state["checked"][tool] = refresh_policy.pack(day, 20, "1.0.0")
    for tool in tools[20:]:
        state["checked"][tool] = refresh_policy.pack(day - 30, 20, "1.0.0")
    state["refresh_cursor"] = 0
    registry.index_requests = 0

    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert report["skipped_not_due"] == 20 and report["refreshed"] == 10
    assert registry.index_requests == 10, "the budget was spent only on due packages"
    assert state["refresh_cursor"] == 0, "the rotation wrapped around the whole catalogue"


def test_nuget_first_recheck_is_seeded_from_stored_rows(tmp_path, monkeypatch):
    Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, {"alpha": "3.0.0"})
    output = tmp_path / "nuget.jsonl"
    output.write_text(json.dumps({"command": "old", "ecosystem": "nuget", "package": "alpha",
                                  "source": "s", "version": "3.0.0", "latest_version": "3.0.0"}) + "\n")
    state = nuget_state(tmp_path, ["alpha"])
    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert registry.nupkg_requests == 0 and report["unchanged"] == 1
    assert "old" in output.read_text()


def test_nuget_withdrawn_tool_loses_its_check_and_is_reported_unavailable(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, {"alpha": "1.0.0"})
    state = nuget_state(tmp_path, ["alpha"])
    output = tmp_path / "nuget.jsonl"
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert "alpha" in state["checked"]
    registry.versions.clear()
    clock.advance(3)
    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert "alpha" not in state["checked"], "a deleted package must be re-inspected if it comes back"
    assert report["unavailable"] == 1


def test_a_newer_extraction_revision_drops_the_checks_once(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, {"alpha": "1.0.0"})
    state = nuget_state(tmp_path, ["alpha"])
    output = tmp_path / "nuget.jsonl"
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    monkeypatch.setattr(refresh_policy, "EXTRACTION_REVISION", refresh_policy.EXTRACTION_REVISION + 1)
    clock.advance(2)
    registry.nupkg_requests = 0
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert registry.nupkg_requests == 1, "rows written by older extraction logic must be rebuilt"


# --------------------------------------------------------------------------- Conan

BASE = "https://center2.conan.io/v2/conans"


class FakeConan:
    def __init__(self, monkeypatch, recipes):
        self.recipes = dict(recipes)  # name -> (version, recipe revision)
        self.requests: list[str] = []
        self.bytes = 0
        monkeypatch.setattr(registry_artifact, "fetch", self.fetch)

    def fetch(self, url, timeout=120, attempts=4):
        self.requests.append(url)
        rest = url[len(BASE) + 1:]
        name, version = rest.split("/")[:2]
        if name not in self.recipes or self.recipes[name][0] != version:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        revision = self.recipes[name][1]
        if rest.endswith("/_/_/revisions"):
            body = json.dumps({"revisions": [{"revision": revision, "time": "2026-01-01T00:00:00.000+0000"}]}).encode()
        elif rest.endswith("/search"):
            body = b'{"linpkg": {"settings": {"os": "Linux"}}}'
        elif rest.endswith("/packages/linpkg/revisions"):
            body = b'{"revisions": [{"revision": "pr1", "time": "2026-01-02T00:00:00.000+0000"}]}'
        elif rest.endswith("conanmanifest.txt"):
            body = f"1\nbin/{name}: aa\n".encode() + b"x" * 20_000
        else:
            raise AssertionError(url)
        self.bytes += len(body)
        return body, {"downloaded_bytes": len(body)}


def conan_state(tmp_path, references, **extra):
    catalog = tmp_path / "conan-recipes.txt"
    registry_artifact.write_catalog(catalog, references)
    return {"recipes_file": str(catalog), "cursor": len(references), "catalog_size": len(references),
            "catalog_complete": True, "catalog_fetched_at": registry_artifact._utc_now().isoformat(),
            "catalog_digest": registry_artifact._catalog_digest(references), **extra}


def test_conan_unchanged_recipe_revision_skips_search_revisions_and_manifest(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeConan(monkeypatch, {"alpha": ("1.0", "rr1"), "beta": ("2.0", "rr7")})
    state = conan_state(tmp_path, ["alpha/1.0", "beta/2.0"])
    output = tmp_path / "conan.jsonl"
    first = registry_artifact._crawl_conan(state, output, 10, 10**9, 120)
    assert len(registry.requests) == 8 and first["unchanged"] == 0  # four requests per recipe
    assert state["checked"]["alpha/1.0"].endswith(":1.0@rr1")
    rows_before = output.read_text()
    baseline_requests, baseline_bytes = len(registry.requests), registry.bytes

    clock.advance(2)
    registry.requests.clear(); registry.bytes = 0
    second = registry_artifact._crawl_conan(state, output, 10, 10**9, 120)
    assert second["unchanged"] == 2 and len(registry.requests) == 2
    assert all(url.endswith("/_/_/revisions") for url in registry.requests)
    assert output.read_text() == rows_before
    assert registry.bytes * 20 < baseline_bytes
    print(f"conan unchanged re-check: {len(registry.requests)} requests / {registry.bytes} B "
          f"(full: {baseline_requests} / {baseline_bytes} B)")


def test_conan_new_recipe_revision_is_re_inspected_by_the_backstop_without_a_feed(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeConan(monkeypatch, {"alpha": ("1.0", "rr1")})
    state = conan_state(tmp_path, ["alpha/1.0"])
    output = tmp_path / "conan.jsonl"
    registry_artifact._crawl_conan(state, output, 10, 10**9, 120)
    registry.recipes["alpha"] = ("1.0", "rr2")  # rebuilt; nothing announced it
    clock.advance(2)
    registry.requests.clear()
    report = registry_artifact._crawl_conan(state, output, 10, 10**9, 120)
    # One extra request: the revision check, then the full inspection starts over.
    assert report["unchanged"] == 0 and len(registry.requests) == 5
    assert state["checked"]["alpha/1.0"].endswith(":1.0@rr2")


def test_conan_rotation_skips_recipes_that_are_not_due(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeConan(monkeypatch, {"alpha": ("1.0", "rr1")})
    state = conan_state(tmp_path, ["alpha/1.0"])
    output = tmp_path / "conan.jsonl"
    registry_artifact._crawl_conan(state, output, 10, 10**9, 120)
    clock.advance(1)
    registry_artifact._crawl_conan(state, output, 10, 10**9, 120)  # streak 1: next due in ~2 days
    registry.requests.clear()
    clock.advance(1)
    report = registry_artifact._crawl_conan(state, output, 10, 10**9, 120)
    assert report["skipped_not_due"] == 1 and registry.requests == []


def commit_feed(monkeypatch, responses):
    calls = []

    def request(url, timeout, headers=None, method="GET", attempts=3):
        calls.append(url)
        result = responses[url] if url in responses else responses[url.split("?")[0]]
        if isinstance(result, Exception):
            raise result
        return result, {"downloaded_bytes": len(result)}

    monkeypatch.setattr(registry_artifact, "_conan_feed_request", request)
    return calls


def compare_url(cursor):
    return f"{change_feeds.GITHUB_API}/repos/{change_feeds.CONAN_REPOSITORY}/compare/{cursor}...master?per_page=100"


def head_url():
    return f"{change_feeds.GITHUB_API}/repos/{change_feeds.CONAN_REPOSITORY}/commits/master"


def test_conan_feed_first_poll_records_the_head_and_queues_nothing(tmp_path, monkeypatch):
    Clock(monkeypatch)
    FakeConan(monkeypatch, {"alpha": ("1.0", "rr1")})
    commit_feed(monkeypatch, {head_url(): b"a" * 40})
    state = conan_state(tmp_path, ["alpha/1.0"])
    report = registry_artifact._crawl_conan(state, tmp_path / "conan.jsonl", 10, 10**9, 120)
    assert state["feed_cursor"] == "a" * 40 and report["feed"]["enqueued"] == 0


def test_conan_feed_queues_changed_recipes_and_commits_cursor_with_the_queue(tmp_path, monkeypatch):
    Clock(monkeypatch)
    registry = FakeConan(monkeypatch, {"alpha": ("1.0", "rr1"), "beta": ("2.0", "rr7")})
    body = json.dumps({"status": "ahead", "total_commits": 2, "commits": [{"sha": "b" * 40}, {"sha": "c" * 40}],
                       "files": [{"filename": "recipes/beta/all/conanfile.py", "status": "modified"},
                                 {"filename": "docs/readme.md", "status": "modified"}]}).encode()
    commit_feed(monkeypatch, {compare_url("a" * 40): body})
    state = conan_state(tmp_path, ["alpha/1.0", "beta/2.0"], feed_cursor="a" * 40)
    saved = []
    # A budget of zero: the run queues the announcement and persists it, nothing more.
    report = registry_artifact._crawl_conan(state, tmp_path / "conan.jsonl", 0, 10**9, 120,
                                            checkpoint=lambda **updates: saved.append(
                                                (state["feed_cursor"], list(state["catalog_pending"]))))
    assert report["feed"]["enqueued"] == 1
    assert saved[0] == ("c" * 40, ["beta/2.0"]), "the cursor is saved together with the queue it produced"
    assert registry.requests == []

    # The queued recipe is inspected ahead of the rotation on the next run.
    report = registry_artifact._crawl_conan(state, tmp_path / "conan.jsonl", 1, 10**9, 120)
    assert [url.split("/")[-4] for url in registry.requests[:1]] == ["beta"] or "beta" in registry.requests[0]
    assert state["catalog_pending"] == []


def test_conan_feed_failure_leaves_cursor_and_queue_for_replay(tmp_path, monkeypatch):
    Clock(monkeypatch)
    FakeConan(monkeypatch, {"alpha": ("1.0", "rr1")})
    commit_feed(monkeypatch, {compare_url("a" * 40): urllib.error.URLError("boom")})
    state = conan_state(tmp_path, ["alpha/1.0"], feed_cursor="a" * 40)
    report = registry_artifact._crawl_conan(state, tmp_path / "conan.jsonl", 0, 10**9, 120)
    assert state["feed_cursor"] == "a" * 40 and report["feed"]["error"]


def test_conan_feed_resync_forgets_history_and_makes_every_recipe_due(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    FakeConan(monkeypatch, {"alpha": ("1.0", "rr1")})
    commit_feed(monkeypatch, {compare_url("a" * 40): json.dumps({"status": "diverged", "commits": [], "files": []}).encode(),
                              head_url(): b"d" * 40})
    state = conan_state(tmp_path, ["alpha/1.0"], feed_cursor="a" * 40,
                        checked={"alpha/1.0": refresh_policy.pack(refresh_policy.today(clock.now), 20, "1.0@rr1")})
    registry_artifact._crawl_conan(state, tmp_path / "conan.jsonl", 0, 10**9, 120)
    assert state["feed_cursor"] == "d" * 40 and state["due_floor"] == refresh_policy.today(clock.now)
    assert refresh_policy.is_due(state["checked"], "alpha/1.0", refresh_policy.today(clock.now), 30, state["due_floor"] - 0) is False
    clock.advance(1)
    assert refresh_policy.is_due(state["checked"], "alpha/1.0", refresh_policy.today(clock.now), 30, state["due_floor"] + 1)


def test_conan_feed_flags_a_catalogue_reread_when_a_recipe_is_added(monkeypatch):
    body = json.dumps({"status": "ahead", "total_commits": 1, "commits": [{"sha": "e" * 40}],
                       "files": [{"filename": "recipes/newlib/all/conanfile.py", "status": "added"},
                                 {"filename": "recipes/newlib/config.yml", "status": "added"}]}).encode()
    calls = commit_feed(monkeypatch, {compare_url("a" * 40): body})
    page = change_feeds.conan_poll(registry_artifact._conan_feed_request, "a" * 40, 30)
    assert page.names == {"newlib"} and page.catalog_changed and not page.resync and len(calls) == 1


def test_conan_feed_treats_a_truncated_comparison_as_a_resync(monkeypatch):
    files = [{"filename": f"recipes/r{index}/all/conanfile.py", "status": "modified"} for index in range(300)]
    commit_feed(monkeypatch, {compare_url("a" * 40): json.dumps(
        {"status": "ahead", "total_commits": 4, "commits": [{"sha": "f" * 40}], "files": files}).encode(),
        head_url(): b"9" * 40})
    page = change_feeds.conan_poll(registry_artifact._conan_feed_request, "a" * 40, 30)
    assert page.resync and page.cursor == "9" * 40 and not page.names


def test_conan_feed_identical_head_changes_nothing(monkeypatch):
    commit_feed(monkeypatch, {compare_url("a" * 40): json.dumps({"status": "identical", "commits": [], "files": []}).encode()})
    page = change_feeds.conan_poll(registry_artifact._conan_feed_request, "a" * 40, 30)
    assert page.cursor == "a" * 40 and not page.names and not page.resync


# --------------------------------------------------------------------------- crates / floor

def test_crates_unchanged_dump_reports_its_ttl_and_costs_one_head_request(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    heads = []

    def head(url, timeout, headers=None, method="GET", attempts=3):
        heads.append(method)
        return b"", {"headers": {"Last-Modified": "Fri, 09 Oct 2026 01:00:00 GMT", "Content-Length": "100"},
                     "downloaded_bytes": 0}

    monkeypatch.setattr(registry_artifact, "_request_bytes", head)
    output = tmp_path / "crates.jsonl"
    output.write_text("{}\n")
    state = {"dump_last_modified": "Fri, 09 Oct 2026 01:00:00 GMT", "catalog_size": 5}
    report = registry_artifact._crawl_crates(state, output, 10, 10**9, 120)
    assert heads == ["HEAD"] and report["unchanged"] is True and report["downloaded_bytes"] == 0
    assert report["ttl"] in {"fresh", "due", "stale"}


def test_paced_host_reservations_queue_in_order(monkeypatch):
    now = [100.0]
    slept = []
    monkeypatch.setattr(registry_artifact.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(registry_artifact.time, "sleep", slept.append)
    monkeypatch.setattr(registry_artifact, "_last_request", {})
    for _ in range(3):
        registry_artifact._throttle("https://crates.io/api/v1/crates/a")
    assert slept == [pytest.approx(1.0), pytest.approx(2.0)], "back-to-back requests are spaced one second apart"


# --- NuGet V3 catalog feed (P1) -------------------------------------------------------


def stamp(clock, minutes_ago):
    return (clock.now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


class FakeCatalog:
    """catalog0/index.json plus its pages; `commit` appends a leaf to a new page."""

    def __init__(self, monkeypatch, clock):
        self.clock = clock
        self.pages = []  # (page stamp, [leaf, ...])
        self.calls = []
        self.fail = None
        monkeypatch.setattr(registry_artifact, "_nuget_feed_request", self.request)

    def commit(self, minutes_ago, *leaves):
        stamped = [{"@type": f"nuget:{kind}", "commitTimeStamp": stamp(self.clock, minutes_ago),
                    "nuget:id": name, "nuget:version": "1.0"} for kind, name in leaves]
        self.pages.append((stamp(self.clock, minutes_ago), stamped))

    @property
    def head(self):
        return self.pages[-1][0] if self.pages else stamp(self.clock, 10_000)

    def request(self, url, timeout, headers=None, method="GET", attempts=3):
        self.calls.append(url)
        if self.fail:
            raise self.fail
        if url == change_feeds.NUGET_CATALOG:
            body = {"commitTimeStamp": self.head, "items": [
                {"@id": f"https://catalog/page{index}.json", "commitTimeStamp": page[0]}
                for index, page in enumerate(self.pages)]}
        else:
            body = {"items": self.pages[int(url.rsplit("page", 1)[1].split(".")[0])][1]}
        raw = json.dumps(body).encode()
        return raw, {"downloaded_bytes": len(raw)}


def nuget_fixture(tmp_path, monkeypatch, versions=None):
    clock = Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, versions or {"Alpha": "1.0.0", "Beta": "2.0.0", "Gamma": "3.0.0"})
    catalog = FakeCatalog(monkeypatch, clock)
    state = nuget_state(tmp_path, sorted(registry.versions))
    return clock, registry, catalog, state, tmp_path / "nuget.jsonl"


def test_nuget_feed_first_poll_records_the_head_and_queues_nothing(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    catalog.commit(60, ("PackageDetails", "Alpha"))
    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert state["feed_cursor"] == catalog.head and report["feed"]["enqueued"] == 0
    assert state["feed_queue"] == [] and catalog.calls == [change_feeds.NUGET_CATALOG]


def test_nuget_feed_queues_announced_tools_and_commits_cursor_with_the_queue(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    clock.advance(0)
    # Cursor is recorded now; then two packages publish (one unrelated to the catalogue).
    registry_artifact._crawl_nuget(state, output, 0, 10**9, 120)
    first_cursor = state["feed_cursor"]
    registry.versions["Beta"] = "2.1.0"
    catalog.commit(40, ("PackageDetails", "beta"), ("PackageDetails", "Unrelated"))
    saved = []
    report = registry_artifact._crawl_nuget(state, output, 0, 10**9, 120, lambda **kw: saved.append(
        (state["feed_cursor"], list(state["feed_queue"]))))
    # Budget 0: nothing inspected, but the queue and the new cursor were stored together.
    assert saved and saved[-1] == (catalog.head, ["Beta"]) and state["feed_cursor"] != first_cursor
    assert report["feed"]["events"] == 1 and report["feed"]["enqueued"] == 1
    # The next run consumes the queue: Beta is re-read although it is not due yet.
    registry.nupkg_requests = 0
    done = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert registry.nupkg_requests == 1 and state["feed_queue"] == []
    assert done["unchanged"] == 0 and '"2.1.0"' in output.read_text()


def test_nuget_feed_failure_leaves_cursor_and_queue_for_replay(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    cursor = state["feed_cursor"]
    catalog.commit(40, ("PackageDetails", "Alpha"))
    catalog.fail = OSError("catalog down")
    report = registry_artifact._crawl_nuget(state, output, 0, 10**9, 120)
    assert report["feed"]["error"] and state["feed_cursor"] == cursor and state["feed_queue"] == []
    catalog.fail = None
    report = registry_artifact._crawl_nuget(state, output, 0, 10**9, 120)
    assert state["feed_queue"] == ["Alpha"] and state["feed_cursor"] == catalog.head


def test_nuget_feed_ignores_catalog_pages_younger_than_the_ready_floor(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    cursor = state["feed_cursor"]
    catalog.commit(1, ("PackageDetails", "Alpha"))  # one minute old: the flat container may lag
    registry_artifact._crawl_nuget(state, output, 0, 10**9, 120)
    assert state["feed_queue"] == [] and state["feed_cursor"] == cursor
    clock.now += timedelta(minutes=10)
    registry_artifact._crawl_nuget(state, output, 0, 10**9, 120)
    assert state["feed_queue"] == ["Alpha"]


def test_nuget_feed_delete_leaf_queues_the_tool_and_a_vanished_one_is_reported_unavailable(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    catalog.commit(40, ("PackageDelete", "Gamma"))
    del registry.versions["Gamma"]  # the flat container no longer lists it
    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert report["feed"]["deleted"] == 1
    assert "Gamma" in state["unavailable"] and "Gamma" not in state["checked"]
    assert state["feed_queue"] == []


def test_nuget_feed_page_cap_resynchronises_and_makes_every_tool_due(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    for minute in range(change_feeds.NUGET_PAGE_LIMIT + 2):
        catalog.commit(1000 - minute, ("PackageDetails", "Unrelated"))
    registry.index_requests = 0
    report = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert report["feed"]["resync"] and state["feed_cursor"] == catalog.head
    day = refresh_policy.today(clock.now)
    assert state["due_floor"] == day
    # Checks written on the same day as the floor are not older than it; next day all are due.
    clock.advance(1)
    registry.index_requests = 0
    again = registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    assert again["skipped_not_due"] == 0 and registry.index_requests == 3
    assert len(catalog.calls) < 2 * (change_feeds.NUGET_PAGE_LIMIT + 2), "a resync reads no catalog pages"


def test_nuget_rotation_backstop_finds_a_release_the_feed_never_announced(tmp_path, monkeypatch):
    clock, registry, catalog, state, output = nuget_fixture(tmp_path, monkeypatch)
    registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
    registry.versions["Alpha"] = "1.5.0"  # no catalog leaf for it
    for _ in range(3):
        clock.advance(2)
        registry_artifact._crawl_nuget(state, output, 10, 10**9, 120)
        if '"1.5.0"' in output.read_text():
            break
    assert '"1.5.0"' in output.read_text()


def test_reparsed_merge_and_change_driven_state_cover_disjoint_sources():
    """#62's `<source>.reparsed.json` merge and #65's `checked`/feed state never meet.

    tools/merge_observations.py only handles OBSERVATION_SOURCES (snapshot collectors);
    every source with checks or a feed is published by copying its checkpoint and rows
    wholesale, so a re-parsed package cannot resurrect rows through a stale check and a
    check cannot hide a re-parse.
    """
    import re
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "tools" / "crawl_parallel.sh").read_text()
    declared = re.search(r'^OBSERVATION_SOURCES="\$\{OBSERVATION_SOURCES:-([^}]*)\}"', script, re.M).group(1).split()
    stateful = {"go", "npm", "pypi", "rubygems", "packagist", "nuget", "crates", "conan"}
    assert declared and not stateful & set(declared)
    # vcpkg and xmake publish through the merge step in cpp-registries.yml, and they keep no checks.
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cpp-registries.yml").read_text()
    assert "OBSERVATION_SOURCES='vcpkg xmake'" in workflow


# --- History and cache split ------------------------------------------------------------


def tree(root):
    root = Path(root)
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def run_nuget(tmp_path, budget=10):
    state = tmp_path / "data" / "production" / "registry-state"
    return registry_artifact.crawl_registry_sources(
        ["nuget"], state, tmp_path / "data" / "production" / "intermediate",
        tmp_path / "reports" / "crawl.json", package_budget=budget, byte_budget=10**9, timeout=120), state


def seeded_nuget_project(tmp_path, monkeypatch, versions):
    clock = Clock(monkeypatch)
    registry = FakeNuGet(monkeypatch, versions)
    catalog = tmp_path / "data" / "production" / "nuget-tools.txt"
    registry_artifact.write_catalog(catalog, sorted(versions))
    state = tmp_path / "data" / "production" / "registry-state"
    registry_artifact.save_state(state, {"version": 1, "sources": {"nuget": {
        "tools_file": str(catalog), "cursor": len(versions), "catalog_advertised": len(versions),
        "catalog_truncated": False}}})
    return clock, registry


def test_an_all_unchanged_run_leaves_the_state_directory_byte_identical(tmp_path, monkeypatch):
    clock, registry = seeded_nuget_project(tmp_path, monkeypatch, {"Alpha": "1.0.0", "Beta": "2.0.0", "Gamma": "3.0.0"})
    _, state = run_nuget(tmp_path)  # the first look records rows, cursors and the catalogue verdict
    clock.advance(1)
    run_nuget(tmp_path)  # settles anything the first rotation still had to write
    settled = tree(state)
    cache_file = tmp_path / "data" / "production" / "cache" / "nuget.cache.gz"
    cache_before = cache_file.read_bytes()
    requests = registry.index_requests
    for _ in range(3):
        clock.advance(40)
        report, _ = run_nuget(tmp_path)
        assert report["sources"]["nuget"]["unchanged"] >= 1
    assert registry.index_requests > requests, "the packages are still checked"
    assert tree(state) == settled, "checking an unchanged package must not write history"
    assert cache_file.read_bytes() != cache_before, "the checks went to the cache"
    assert "checked" not in registry_artifact.load_state(state)["sources"]["nuget"]
    assert "refresh_cursor" not in registry_artifact.load_state(state)["sources"]["nuget"]


def test_a_lost_cache_rechecks_everything_and_never_touches_history(tmp_path, monkeypatch):
    clock, registry = seeded_nuget_project(tmp_path, monkeypatch, {"Alpha": "1.0.0", "Beta": "2.0.0", "Gamma": "3.0.0"})
    _, state = run_nuget(tmp_path)
    clock.advance(1)
    run_nuget(tmp_path)
    settled, rows = tree(state), (tmp_path / "data" / "production" / "intermediate" / "nuget.jsonl").read_bytes()
    (tmp_path / "data" / "production" / "cache" / "nuget.cache.gz").unlink()
    registry.index_requests = registry.nupkg_requests = 0
    clock.advance(1)
    report, _ = run_nuget(tmp_path)
    assert registry.index_requests == 3, "a cold cache makes every package due"
    assert registry.nupkg_requests == 0, "rows seed the known version, so no artifact is read again"
    assert report["sources"]["nuget"]["unchanged"] == 3
    assert tree(state) == settled
    assert (tmp_path / "data" / "production" / "intermediate" / "nuget.jsonl").read_bytes() == rows


def test_a_legacy_checked_map_is_read_once_and_dropped_from_the_state(tmp_path, monkeypatch):
    clock, registry = seeded_nuget_project(tmp_path, monkeypatch, {"Alpha": "1.0.0"})
    day = refresh_policy.today(clock.now)
    state = tmp_path / "data" / "production" / "registry-state"
    document = registry_artifact.load_state(state)
    document["sources"]["nuget"]["checked"] = {"Alpha": refresh_policy.pack(day, 3, "1.0.0")}
    document["sources"]["nuget"]["refresh_cursor"] = 0
    document["sources"]["nuget"]["extraction_revision"] = refresh_policy.EXTRACTION_REVISION
    registry_artifact.save_state(state, document)
    assert "checked" in registry_artifact.load_state(state)["sources"]["nuget"]
    run_nuget(tmp_path)
    assert "checked" not in registry_artifact.load_state(state)["sources"]["nuget"]
    checks, _, warm = refresh_policy.read_cache(tmp_path / "data" / "production" / "cache" / "nuget.cache.gz")
    assert warm and refresh_policy.unpack(checks["Alpha"])[2] == "1.0.0"


def test_a_released_package_changes_only_its_rows(tmp_path, monkeypatch):
    clock, registry = seeded_nuget_project(tmp_path, monkeypatch, {"Alpha": "1.0.0", "Beta": "2.0.0"})
    _, state = run_nuget(tmp_path)
    clock.advance(1)
    run_nuget(tmp_path)
    settled = tree(state)
    rows_path = tmp_path / "data" / "production" / "intermediate" / "nuget.jsonl"
    rows_before = rows_path.read_text().splitlines()
    registry.versions["Beta"] = "2.1.0"
    for _ in range(4):
        clock.advance(3)
        run_nuget(tmp_path)
    changed = [line for line in rows_path.read_text().splitlines() if line not in rows_before]
    assert changed and all('"Beta"' in line for line in changed)
    assert tree(state) == settled, "a new version is recorded in the rows alone"


def test_conan_unchanged_run_writes_nothing_to_the_state_and_a_cold_cache_rereads_recipes(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    registry = FakeConan(monkeypatch, {"alpha": ("1.0", "rr1"), "beta": ("2.0", "rr7")})
    references = ["alpha/1.0", "beta/2.0"]
    seeded = conan_state(tmp_path, references)
    state = tmp_path / "data" / "production" / "registry-state"
    save_state(state, {"version": 1, "sources": {"conan": seeded}})

    def run():
        return registry_artifact.crawl_registry_sources(
            ["conan"], state, tmp_path / "data" / "production" / "intermediate", tmp_path / "reports" / "c.json",
            package_budget=10, byte_budget=10**9, timeout=120)

    run(); clock.advance(1); run()
    settled = tree(state)
    for _ in range(3):
        clock.advance(40)
        report = run()
        assert report["sources"]["conan"]["unchanged"] == 2
    assert tree(state) == settled, "an unchanged recipe revision writes nothing to the history"
    (tmp_path / "data" / "production" / "cache" / "conan.cache.gz").unlink()
    registry.requests.clear()
    clock.advance(1)
    report = run()
    assert report["sources"]["conan"]["unchanged"] == 0 and len(registry.requests) >= 8, "cold: recipes are read again"
    assert tree(state) == settled, "and the rows they produce are the ones already stored"


def test_publication_of_an_all_unchanged_run_is_empty(tmp_path, monkeypatch):
    """Two crawls, the second finding nothing new: the second publish makes no commit."""
    import shutil
    import subprocess
    import time
    root = Path(__file__).resolve().parents[1]
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    for args in (("init", "-q", "-b", "artifact-data"), ("commit", "--allow-empty", "-qm", "seed"),
                 ("push", "-q", f"file://{origin}", "artifact-data")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=seed, check=True)
    work = tmp_path / "work"
    for relative in ("tools/crawl_parallel.sh", "tools/merge_observations.py", "tools/merge_registry_publication.py",
                     "tools/registry_state.py", "tools/transport_shards.py", "src/global_executables/__init__.py",
                     "src/global_executables/registry_state.py"):
        (work / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, work / relative)
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-qm", "tools"),
                 ("remote", "add", "origin", f"file://{origin}")):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=work, check=True)

    clock, registry = seeded_nuget_project(tmp_path / "base-nuget", monkeypatch, {"Alpha": "1.0.0", "Beta": "2.0.0"})
    crawl_root = tmp_path / "base-nuget"
    environment = {**__import__("os").environ, "BASE": str(tmp_path / "base"), "SOURCES": "nuget",
                   "OBSERVATION_SOURCES": " ", "PUBLISH_MAX_ATTEMPTS": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    # crawl_parallel.sh looks for the run's checkout at "${BASE}-nuget".
    assert (tmp_path / "base-nuget") == crawl_root

    def crawl_and_publish(advance):
        clock.advance(advance)
        registry_artifact.crawl_registry_sources(
            ["nuget"], crawl_root / "data/production/registry-state", crawl_root / "data/production/intermediate",
            crawl_root / "reports/registry-artifact-crawl.json", package_budget=10, byte_budget=10**9, timeout=120)
        result = subprocess.run(["bash", "tools/crawl_parallel.sh", "publish"], cwd=work, env=environment,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def head():
        return subprocess.run(["git", "rev-parse", "artifact-data"], cwd=origin, capture_output=True, text=True).stdout

    assert "published" in crawl_and_publish(0)
    crawl_and_publish(1)  # settles what the first rotation still wrote
    settled = head()
    for days in (40, 40, 40):
        output = crawl_and_publish(days)
        assert "nothing to publish" in output, output
    assert head() == settled
    # Idle for more than a day (a heartbeat of a few milliseconds stands in for it): the
    # report alone is republished; no state or row file is touched.
    environment["REPORT_HEARTBEAT_HOURS"] = "0.000001"
    time.sleep(0.01)
    output = crawl_and_publish(40)
    assert "published" in output.splitlines()
    assert head() != settled
    changed = subprocess.run(["git", "diff", "--name-only", settled.strip(), head().strip()], cwd=origin,
                             capture_output=True, text=True).stdout.split()
    assert changed == ["reports/registry-artifact-crawl.json"], changed


def workflow_steps(path):
    import yaml
    document = yaml.safe_load(path.read_text())
    return next(iter(document["jobs"].values()))["steps"]


def test_workflows_keep_the_schedule_cache_between_runs_and_git_ignores_it():
    root = Path(__file__).resolve().parents[1]
    assert "data/production/cache/" in (root / ".gitignore").read_text().splitlines()
    for name in ("registry-refresh.yml", "cpp-registries.yml"):
        steps = workflow_steps(root / ".github" / "workflows" / name)
        cache = next(step for step in steps if str(step.get("uses", "")).startswith("actions/cache@"))
        # Workspace-relative: the runner user owns it. A /tmp path written by a root container made the
        # post-job save fail with "Permission denied" on every 2026-10-09 scheduled run.
        assert cache["with"]["path"] == "data/production/cache", name
        assert "run_id" in cache["with"]["key"] and "run_attempt" in cache["with"]["key"], "a fresh key per run"
        assert cache["with"]["restore-keys"].strip().startswith("schedule-v1-"), "restore falls back to the prefix"
        assert cache.get("continue-on-error") is True, "a failed cache never fails the run"


def test_registry_refresh_gives_the_crawlers_a_runner_owned_cache_directory():
    root = Path(__file__).resolve().parents[1]
    steps = workflow_steps(root / ".github" / "workflows" / "registry-refresh.yml")
    names = [step.get("name", "") for step in steps]
    prepare, restore = names.index("Prepare the schedule cache"), names.index("Restore the schedule cache")
    assert prepare < restore < names.index("Refresh one transactional batch")
    assert "mkdir -p data/production/cache" in steps[prepare]["run"], "created by the runner, not by Docker"
    go = steps[names.index("Refresh one transactional batch")]["run"]
    assert '-v "${PWD}/data/production/cache:/cache"' in go and '--cache "/cache/${SOURCE}.cache.gz"' in go
    assert "--cache-dir data/production/cache" in steps[names.index("Refresh one NuGet batch")]["run"]
    handback = steps[names.index("Hand the schedule cache back to the runner")]
    assert "always()" in handback["if"] and handback.get("continue-on-error") is True
    assert "chown" in handback["run"] and "chmod" in handback["run"]


def test_a_cache_written_by_another_user_stays_readable(tmp_path):
    import stat
    path = tmp_path / "cache" / "x.cache.gz"
    refresh_policy.write_cache(path, {"a": "1:0:1"}, 0)
    # mkstemp makes 0600; the CI cache action (another user than a root container) must be able to read it.
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
