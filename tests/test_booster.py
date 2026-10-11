"""Local booster: ownership, leases, delta publication (docs/OPERATIONS.md "Local booster")."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from global_executables import booster  # noqa: E402
from global_executables.registry_state import load_state, save_state  # noqa: E402

GOLDEN = ROOT / "internal" / "gocrawl" / "testdata" / "refresh" / "owner-golden.json"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def test_owner_buckets_and_ranges_match_the_go_golden():
    golden = json.loads(GOLDEN.read_text())
    assert all(booster.owner_bucket(case["name"]) == case["bucket"] for case in golden["cases"])
    for case in golden["ranges"]:
        assert len(booster.parse_ranges(case["text"])) == case["count"], case
    for bad in ("5-2", "256", "a-b", "-3", "0-256"):
        with pytest.raises(ValueError):
            booster.parse_ranges(bad)


def test_ranges_round_trip_and_slices_partition_the_buckets_exactly():
    assert booster.format_ranges(booster.parse_ranges("128-191,0-63,70,64")) == "0-64,70,128-191"
    for count in (1, 2, 3, 5, 7, 256):
        covered: list[int] = []
        for index in range(count):
            covered += sorted(booster.parse_ranges(booster.slice_ranges(index, count)))
        assert sorted(covered) == list(range(256)), "slices neither overlap nor leave a gap"
    names = [f"pkg-{i}" for i in range(5000)]
    mine = set(booster.parse_ranges(booster.slice_ranges(0, 2)))
    shares = sum(booster.owner_bucket(name) in mine for name in names)
    assert 2200 < shares < 2800, "the hash spreads the catalogue evenly"
    assert [booster.owner_bucket(name) for name in names] == [booster.owner_bucket(name) for name in names]


def test_lease_ownership_heartbeat_expiry_and_conflict():
    document = booster.acquire(booster.empty_document(), "laptop", "0-127", NOW, ttl_hours=12)
    assert booster.actions_exclusion(document, NOW + timedelta(hours=11)) == "0-127"
    # The machine went to sleep: after the TTL the range is Actions' again, nobody has to act.
    assert booster.actions_exclusion(document, NOW + timedelta(hours=13)) == ""
    # A heartbeat before the expiry keeps it, and keeps when the lease started.
    renewed = booster.acquire(document, "laptop", "0-127", NOW + timedelta(hours=10), ttl_hours=12)
    assert booster.actions_exclusion(renewed, NOW + timedelta(hours=21)) == "0-127"
    assert renewed["leases"]["laptop"]["started_at"] == NOW.isoformat()
    # An extra machine takes the other half; overlapping a live lease is refused.
    both = booster.acquire(renewed, "desktop", "128-255", NOW + timedelta(hours=10))
    assert booster.actions_exclusion(both, NOW + timedelta(hours=11)) == "0-255"
    with pytest.raises(booster.LeaseConflict):
        booster.acquire(renewed, "intruder", "100-140", NOW + timedelta(hours=11))
    # ...but an expired lease does not block a new owner.
    taken = booster.acquire(document, "intruder", "100-140", NOW + timedelta(hours=13))
    assert booster.actions_exclusion(taken, NOW + timedelta(hours=14)) == "100-140"
    assert booster.actions_exclusion(booster.release(both, "laptop"), NOW + timedelta(hours=11)) == "128-255"
    ancient = booster.acquire(document, "new", "200-210", NOW + timedelta(days=booster.PRUNE_AFTER_DAYS + 1))
    assert "laptop" not in ancient["leases"], "long-dead leases are pruned"
    for bad_id in ("", "../x", "a b"):
        with pytest.raises(ValueError):
            booster.acquire(booster.empty_document(), bad_id, "0-1", NOW)
    assert booster.actions_exclusion({"leases": {"x": {"ranges": "0-9", "heartbeat": "garbage"}}}, NOW) == ""


def source(**overrides):
    entry = {"cursor": 100, "catalog_size": 100, "catalog_complete": True, "feed_cursor": "10",
             "feed_pending": {"f": {"kind": "update"}}, "unavailable": {"gone": "HTTP 404"},
             "failures": {}, "retry_projects": [], "snapshot_generation": 7}
    entry.update(overrides)
    return entry


def test_state_delta_keeps_the_other_writers_packages_and_is_idempotent():
    base = source()
    published = source(unavailable={"gone": "HTTP 404", "by-actions": "HTTP 404"}, feed_cursor="12", snapshot_generation=8)
    # The booster saw two more packages vanish and one come back, retried one, and moved a cursor it does not own.
    local = source(unavailable={"by-booster": "HTTP 404"}, failures={"flaky": "timeout"}, retry_projects=["flaky"],
                   feed_cursor="stale", cursor=3)
    merged, changed = booster.merge_source_delta(published, local, base, scalars=False)
    assert changed
    assert merged["unavailable"] == {"by-actions": "HTTP 404", "by-booster": "HTTP 404"}
    assert merged["failures"] == {"flaky": "timeout"} and merged["retry_projects"] == ["flaky"]
    assert merged["feed_cursor"] == "12" and merged["cursor"] == 100, "a booster never touches cursors or the feed"
    assert merged["snapshot_generation"] == 9
    again, changed_again = booster.merge_source_delta(merged, local, base, scalars=False)
    assert not changed_again and again == merged, "applying the same delta twice changes nothing"
    # The Actions writer takes its own checkpoint scalars as well.
    actions_local = source(feed_cursor="20", snapshot_generation=9, unavailable={"gone": "HTTP 404", "late": "HTTP 404"})
    after_actions, _ = booster.merge_source_delta(merged, actions_local, base, scalars=True)
    assert after_actions["feed_cursor"] == "20"
    assert set(after_actions["unavailable"]) == {"by-actions", "by-booster", "late"}
    # A name the writer removed disappears from the published map, and only that name.
    healed = source(unavailable={})
    cleared, _ = booster.merge_source_delta(after_actions, healed, base, scalars=False)
    assert "gone" not in cleared["unavailable"] and "late" in cleared["unavailable"]


def row(package, version, command="tool"):
    return json.dumps({"package": package, "version": version, "command": command}, sort_keys=True)


def test_rows_delta_replaces_only_the_packages_the_writer_changed(tmp_path):
    base = tmp_path / "base.jsonl"
    base.write_text("\n".join([row("a", "1"), row("b", "1"), row("c", "1")]) + "\n")
    published = tmp_path / "published.jsonl"  # Actions meanwhile re-read c and found d
    published.write_text("\n".join([row("a", "1"), row("b", "1"), row("c", "2"), row("d", "1")]) + "\n")
    local = tmp_path / "local.jsonl"  # the booster re-read b (new version, second command) and dropped a
    local.write_text("\n".join([row("b", "2"), row("b", "2", "other"), row("c", "1")]) + "\n")
    out = tmp_path / "out.jsonl"
    touched, written = booster.merge_rows_delta(published, local, base, out)
    assert (touched, written) == (2, 2)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert {(r["package"], r["version"], r["command"]) for r in rows} == {
        ("c", "2", "tool"), ("d", "1", "tool"), ("b", "2", "tool"), ("b", "2", "other")}
    assert "a" not in {r["package"] for r in rows}, "a was dropped by the writer"
    first = out.read_bytes()
    out2 = tmp_path / "out2.jsonl"
    booster.merge_rows_delta(out, local, base, out2)
    assert out2.read_bytes() == first, "the same delta applied twice gives the same bytes"


# --- end to end: two publishers against one bare origin -----------------------------------

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


class Origin:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.bare = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.bare)], check=True)
        seed = tmp_path / "seed"
        seed.mkdir()
        for args in (("init", "-q", "-b", "artifact-data"), ("commit", "--allow-empty", "-qm", "seed"),
                     ("push", "-q", f"file://{self.bare}", "artifact-data")):
            subprocess.run([*GIT, *args], cwd=seed, check=True)
        self.work = tmp_path / "work"
        for relative in ("tools/crawl_parallel.sh", "tools/merge_observations.py", "tools/merge_registry_publication.py",
                         "tools/registry_state.py", "tools/transport_shards.py", "tools/booster.py",
                         "src/global_executables/__init__.py", "src/global_executables/registry_state.py",
                         "src/global_executables/booster.py", "src/global_executables/refresh_policy.py",
                         "tools/local_booster.sh"):
            (self.work / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, self.work / relative)
        for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-qm", "tools"),
                     ("remote", "add", "origin", f"file://{self.bare}")):
            subprocess.run([*GIT, *args], cwd=self.work, check=True)

    def writer(self, name, *, booster_mode, base_state, base_rows):
        """A writer's crawl directory ``<base>-pypi`` seeded with the published base."""
        base = self.root / name
        directory = Path(f"{base}-pypi")
        save_state(directory / "data/production/registry-state", {"version": 1, "sources": {"pypi": base_state}})
        rows = directory / "data/production/intermediate/pypi.jsonl"
        rows.parent.mkdir(parents=True, exist_ok=True)
        rows.write_text("".join(line + "\n" for line in base_rows))
        shutil.copytree(directory / "data/production/registry-state", directory / ".base/registry-state")
        shutil.copy2(rows, directory / ".base/rows.jsonl")
        environment = {**os.environ, "BASE": str(base), "SOURCES": "pypi", "OBSERVATION_SOURCES": " ",
                       "DELTA_SOURCES": "pypi", "CONTAINER_RUNTIME": "docker", "BOOSTER": "1" if booster_mode else "0", "PUBLISH_MAX_ATTEMPTS": "6",
                       "PUBLISH_LOCK": str(self.root / f"{name}.lock"), "GIT_AUTHOR_NAME": "t",
                       "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        return directory, environment

    def publish(self, environment):
        return subprocess.run(["bash", "tools/crawl_parallel.sh", "publish"], cwd=self.work, env=environment,
                              capture_output=True, text=True)

    def head(self):
        return subprocess.run(["git", "rev-parse", "artifact-data"], cwd=self.bare, capture_output=True, text=True).stdout.strip()

    def published(self, tmp):
        clone = tmp / "clone"
        shutil.rmtree(clone, ignore_errors=True)
        subprocess.run(["git", "clone", "-q", "-b", "artifact-data", f"file://{self.bare}", str(clone)], check=True)
        state = load_state(clone / "data/production/registry-state")["sources"]["pypi"]
        rows = subprocess.run([sys.executable, "tools/transport_shards.py", "unpack", "--input-dir",
                               str(clone / "data/production/transport/pypi-observations"), "--output", str(tmp / "rows.jsonl")],
                              cwd=self.work, capture_output=True, text=True)
        assert rows.returncode == 0, rows.stderr
        return clone, state, [json.loads(line) for line in (tmp / "rows.jsonl").read_text().splitlines()]


def rewrite(directory, state_update, rows):
    document = load_state(directory / "data/production/registry-state")
    document["sources"]["pypi"].update(state_update)
    save_state(directory / "data/production/registry-state", document)
    (directory / "data/production/intermediate/pypi.jsonl").write_text("".join(line + "\n" for line in rows))


def test_two_concurrent_publishers_merge_without_losing_either_and_republishing_is_a_no_op(tmp_path):
    origin = Origin(tmp_path)
    initial_rows = [row("a", "1"), row("b", "1")]
    # The first publication: Actions publishes the baseline.
    actions, actions_env = origin.writer("actions", booster_mode=False, base_state=source(), base_rows=[])
    rewrite(actions, {}, initial_rows)
    assert origin.publish(actions_env).returncode == 0
    clone, state, rows = origin.published(tmp_path)
    assert {r["package"] for r in rows} == {"a", "b"} and state["cursor"] == 100
    # Both writers start from that baseline.
    actions, actions_env = origin.writer("actions2", booster_mode=False, base_state=state, base_rows=initial_rows)
    local, booster_env = origin.writer("booster", booster_mode=True, base_state=state, base_rows=initial_rows)
    rewrite(actions, {"feed_cursor": "99", "unavailable": {"gone": "HTTP 404", "x-actions": "HTTP 404"}},
            [row("a", "2"), row("b", "1"), row("feed-pkg", "1")])
    rewrite(local, {"feed_cursor": "stale", "cursor": 5, "unavailable": {"gone": "HTTP 404", "x-booster": "HTTP 404"}},
            [row("a", "1"), row("b", "1"), row("new-1", "1"), row("new-2", "1")])
    results = {}
    threads = [threading.Thread(target=lambda n=n, e=e: results.__setitem__(n, origin.publish(e)))
               for n, e in (("actions", actions_env), ("booster", booster_env))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(result.returncode == 0 for result in results.values()), {k: v.stderr for k, v in results.items()}
    _, state, rows = origin.published(tmp_path)
    assert {(r["package"], r["version"]) for r in rows} == {
        ("a", "2"), ("b", "1"), ("feed-pkg", "1"), ("new-1", "1"), ("new-2", "1")}, "both writers' packages survive"
    assert state["feed_cursor"] == "99" and state["cursor"] == 100, "only Actions moves the feed and the cursors"
    assert set(state["unavailable"]) == {"gone", "x-actions", "x-booster"}
    # Idempotent: publishing the same writers again produces no commit at all.
    head = origin.head()
    for env in (booster_env, actions_env):
        output = origin.publish(env)
        assert output.returncode == 0 and "nothing to publish" in output.stdout, output.stdout + output.stderr
    assert origin.head() == head


def test_a_failed_publish_is_retried_with_the_same_delta_after_a_crash(tmp_path):
    origin = Origin(tmp_path)
    local, env = origin.writer("booster", booster_mode=True, base_state=source(), base_rows=[row("a", "1")])
    seed_env = dict(env)
    # Publish the baseline once so the branch has the source.
    actions, actions_env = origin.writer("actions", booster_mode=False, base_state=source(), base_rows=[])
    rewrite(actions, {}, [row("a", "1")])
    assert origin.publish(actions_env).returncode == 0
    rewrite(local, {"unavailable": {"v": "HTTP 404"}}, [row("a", "1"), row("fresh", "3")])
    hook = origin.bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    env["PUBLISH_MAX_ATTEMPTS"] = "1"
    before = origin.head()
    failed = origin.publish(env)
    assert failed.returncode != 0 and origin.head() == before
    assert not (local / ".base/rows.jsonl").read_text().count("fresh"), "a failed publish must not advance the base"
    hook.unlink()  # the machine comes back / the network recovers
    ok = origin.publish(seed_env)
    assert ok.returncode == 0 and "published" in ok.stdout.splitlines(), ok.stdout + ok.stderr
    _, state, rows = origin.published(tmp_path)
    assert ("fresh", "3") in {(r["package"], r["version"]) for r in rows} and state["unavailable"] == {"v": "HTTP 404"}
    assert "fresh" in (local / ".base/rows.jsonl").read_text(), "after success the published snapshot is the new base"


def test_lease_cli_acquire_conflict_exclusion_expiry_and_release(tmp_path):
    origin = Origin(tmp_path)
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

    def cli(*args):
        return subprocess.run([sys.executable, "tools/booster.py", *args], cwd=origin.work, env=environment,
                              capture_output=True, text=True)

    assert cli("exclusions", "--source", "pypi", "--ref", "origin/artifact-data").stdout.strip() == ""
    assert cli("acquire", "--source", "pypi", "--id", "laptop", "--ranges", "0-127").returncode == 0
    subprocess.run(["git", "fetch", "-q", "origin", "artifact-data"], cwd=origin.work, check=True)
    assert cli("exclusions", "--source", "pypi", "--ref", "FETCH_HEAD").stdout.strip() == "0-127"
    clash = cli("acquire", "--source", "pypi", "--id", "other", "--ranges", "64-200")
    assert clash.returncode == 4 and "held by another live lease" in clash.stderr
    assert cli("acquire", "--source", "pypi", "--id", "desktop", "--ranges", "128-255").returncode == 0
    head = subprocess.run(["git", "rev-parse", "artifact-data"], cwd=origin.bare, capture_output=True, text=True).stdout
    assert cli("renew", "--source", "pypi", "--id", "laptop").returncode == 0, "renew keeps the ranges"
    document = json.loads(subprocess.run(["git", "show", "artifact-data:data/production/booster/pypi.json"], cwd=origin.bare,
                                         capture_output=True, text=True).stdout)
    assert document["leases"]["laptop"]["ranges"] == "0-127" and document["leases"]["desktop"]["ranges"] == "128-255"
    assert cli("release", "--source", "pypi", "--id", "laptop").returncode == 0
    subprocess.run(["git", "fetch", "-q", "origin", "artifact-data"], cwd=origin.work, check=True)
    assert cli("exclusions", "--source", "pypi", "--ref", "FETCH_HEAD").stdout.strip() == "128-255"
    # A lease that is never renewed expires by itself (ttl in the past): Actions owns everything again.
    assert cli("acquire", "--source", "pypi", "--id", "sleepy", "--ranges", "0-9", "--ttl-hours", "0.0000001").returncode == 0
    subprocess.run(["git", "fetch", "-q", "origin", "artifact-data"], cwd=origin.work, check=True)
    assert "0-9" not in cli("exclusions", "--source", "pypi", "--ref", "FETCH_HEAD").stdout
    assert head != ""


def test_registry_refresh_leaves_a_live_boosters_buckets_to_it():
    import yaml
    document = yaml.safe_load((ROOT / ".github/workflows/registry-refresh.yml").read_text())
    steps = next(iter(document["jobs"].values()))["steps"]
    names = [step.get("name", "") for step in steps]
    find = steps[names.index("Find the buckets a local booster owns")]
    refresh = steps[names.index("Refresh one transactional batch")]
    assert names.index("Find the buckets a local booster owns") < names.index("Refresh one transactional batch")
    assert "tools/booster.py exclusions" in find["run"] and "|| true" in find["run"], "a lookup failure means full ownership"
    assert refresh["env"]["EXCLUDE"] == "${{ steps.booster.outputs.exclude }}"
    assert '${EXCLUDE:+--rotation-exclude "${EXCLUDE}"}' in refresh["run"]
    assert "--no-feed" not in refresh["run"], "Actions keeps the feed"


def test_local_booster_script_requires_a_contact_and_documents_every_platform():
    script = ROOT / "tools/local_booster.sh"
    refused = subprocess.run([str(script), "pypi", "run"], capture_output=True, text=True,
                             env={k: v for k, v in os.environ.items() if k != "CONTACT"})
    assert refused.returncode == 2 and "CONTACT" in refused.stderr, "never crawl anonymously"
    assert subprocess.run([str(script), "nuget", "run"], capture_output=True).returncode == 2
    unit = subprocess.run([str(script), "pypi", "unit"], capture_output=True, text=True,
                          env={**os.environ, "CONTACT": "me@example.org", "BOOSTER_SLICE": "1/2"})
    assert unit.returncode == 0
    for needle in ("[Service]", "launchd", "@reboot", "Restart=on-failure", "me@example.org"):
        assert needle in unit.stdout
    text = script.read_text()
    assert 'DELTA_SOURCES' in text and "BOOSTER=1" in text, "a booster publishes deltas, never the whole source"
    assert "--no-feed" in text and "--rotation-include" in text and "--max-bytes-per-second" in text


# --- native mode supervisor: PID tracking, stop ordering, stale PID, double start --------------

import signal  # noqa: E402
import time  # noqa: E402

FAKE_CRAWLER = """#!/usr/bin/env bash
echo "started $$" >> "$EVENTS"
%s
while :; do sleep 0.2; done
"""
POLITE = "trap 'sleep 1; echo crawler-exited >> \"$EVENTS\"; exit 0' TERM"
DEAF = "trap '' TERM"


class Native:
    def __init__(self, tmp_path, crawler_body=POLITE, stop_timeout="20"):
        self.origin = Origin(tmp_path)
        self.tmp = tmp_path
        self.events = tmp_path / "events.log"
        self.events.write_text("")
        self.crawler = tmp_path / "fake-crawler"
        self.crawler.write_text(FAKE_CRAWLER % crawler_body)
        self.crawler.chmod(0o755)
        self.base = tmp_path / "base"
        save_state(self.base / "data/production/registry-state", {"version": 1, "sources": {"pypi": source()}})
        self.dir = Path(f"{self.base}-pypi")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, "BASE": str(self.base), "CONTACT": "t@example.org", "BOOSTER_ID": "box-test",
                    "MODE": "native", "CRAWLER_BIN": str(self.crawler), "EVENTS": str(self.events),
                    "PUBLISH_INTERVAL": "1000", "HEARTBEAT_INTERVAL": "1000", "STOP_TIMEOUT": stop_timeout,
                    "PUBLISH_LOCK": str(tmp_path / "publish.lock"),
                    "PUBLISH_CMD": 'echo publish >> "$EVENTS"', "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.procs = []

    def lines(self):
        return self.events.read_text().split("\n")[:-1]

    def run(self, command="run", **extra):
        process = subprocess.Popen(["bash", "tools/local_booster.sh", "pypi", command], cwd=self.origin.work,
                                   env={**self.env, **extra}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
        self.procs.append(process)
        return process

    def wait_for(self, predicate, seconds=40):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def lease_document(self):
        shown = subprocess.run(["git", "show", "artifact-data:data/production/booster/pypi.json"], cwd=self.origin.bare,
                               capture_output=True, text=True)
        return json.loads(shown.stdout) if shown.returncode == 0 else {"leases": {}}

    def crawler_processes(self):
        found = subprocess.run(["pgrep", "-f", str(self.crawler)], capture_output=True, text=True).stdout.split()
        return [pid for pid in found if pid != str(os.getpid())]

    def cleanup(self):
        for process in self.procs:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
        subprocess.run(["pkill", "-KILL", "-f", str(self.crawler)])


@pytest.fixture
def native(tmp_path):
    harness = Native(tmp_path)
    yield harness
    harness.cleanup()


def test_native_term_stops_the_crawler_first_then_publishes_then_releases_the_lease(native):
    supervisor = native.run()
    assert native.wait_for(lambda: len(native.lines()) == 1), "crawler never started"
    pid = int((native.dir / "booster.pid").read_text())
    assert str(pid) in native.lines()[0], "the PID file names the crawler itself, not a subshell"
    assert native.lease_document()["leases"]["box-test"]["ranges"] == "0-255"
    os.kill(supervisor.pid, signal.SIGTERM)
    assert supervisor.wait(timeout=60) == 0
    # The crawler exits (one second after TERM) BEFORE the final publication.
    assert [line.split()[0] for line in native.lines()] == ["started", "crawler-exited", "publish"], native.lines()
    assert native.lease_document()["leases"] == {}, "the lease is released last"
    assert native.crawler_processes() == [] and not (native.dir / "booster.pid").exists(), "no orphan crawler"
    assert not (native.dir / "supervisor.pid").exists()


def test_a_crawler_that_ignores_term_is_killed_and_nothing_is_published_over_a_live_one(tmp_path):
    harness = Native(tmp_path, crawler_body=DEAF, stop_timeout="2")
    try:
        supervisor = harness.run()
        assert harness.wait_for(lambda: harness.lines())
        os.kill(supervisor.pid, signal.SIGTERM)
        assert supervisor.wait(timeout=60) == 0
        assert harness.lines()[-1] == "publish" and harness.crawler_processes() == [], "KILL after the timeout, then publish"
        assert harness.lease_document()["leases"] == {}
    finally:
        harness.cleanup()


def test_a_stale_or_foreign_pid_file_is_ignored_and_a_second_start_is_refused(native):
    bystander = subprocess.Popen(["sleep", "300"])
    try:
        (native.dir / "booster.pid").write_text("999999\n")
        first = native.run()
        assert native.wait_for(lambda: native.lines()), "a stale PID file must not block the start"
        # A second supervisor for the same directory is refused and starts no second crawler.
        second = native.run()
        assert second.wait(timeout=60) == 3, second.stdout.read()
        assert [line.split()[0] for line in native.lines()] == ["started"]
        os.kill(first.pid, signal.SIGTERM)
        assert first.wait(timeout=60) == 0
        # A PID file naming an unrelated live process is never trusted or killed.
        (native.dir / "booster.pid").write_text(f"{bystander.pid}\n")
        third = native.run()
        assert native.wait_for(lambda: native.lines().count("publish") == 1 and sum(l.startswith("started") for l in native.lines()) == 2)
        os.kill(third.pid, signal.SIGTERM)
        assert third.wait(timeout=60) == 0
        assert bystander.poll() is None, "the unrelated process survived"
    finally:
        bystander.kill()


def test_an_orphan_crawler_without_a_pid_file_blocks_start_and_is_stopped_by_stop(native):
    orphan = subprocess.Popen([str(native.crawler), "crawl", "--source", "pypi", "--rotation-include", "0-255"],
                              cwd=native.dir, env=native.env, start_new_session=True)
    try:
        assert native.wait_for(lambda: native.lines())
        refused = native.run()
        assert refused.wait(timeout=60) == 3 and "already running" in refused.stdout.read()
        stopped = native.run("stop")
        assert stopped.wait(timeout=60) == 0
        assert orphan.wait(timeout=10) == 0 and native.lines()[-1] == "crawler-exited"
    finally:
        if orphan.poll() is None:
            orphan.kill()


def test_heartbeat_only_renewal_commits_nothing_until_the_lease_is_old(tmp_path):
    origin = Origin(tmp_path)
    environment = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}

    def cli(*args):
        return subprocess.run([sys.executable, "tools/booster.py", *args, "--source", "pypi", "--id", "m"], cwd=origin.work,
                              env=environment, capture_output=True, text=True)

    assert cli("acquire", "--ranges", "0-255", "--ttl-hours", "18").returncode == 0
    head = origin.head()
    assert cli("renew", "--min-age-hours", "6").returncode == 0
    assert origin.head() == head, "a fresh heartbeat is left alone: no commit"
    time.sleep(0.2)
    assert cli("renew", "--min-age-hours", "0.00001").returncode == 0
    assert origin.head() != head, "an old heartbeat is renewed"
    changed = subprocess.run(["git", "diff", "--name-only", head, origin.head()], cwd=origin.bare, capture_output=True,
                             text=True).stdout.split()
    assert changed == ["data/production/booster/pypi.json"], "only the lease file, no data blobs"
    document = {"leases": {"m": {"ranges": "0-255", "heartbeat": NOW.isoformat(), "ttl_hours": 18}}}
    assert booster.fresh(document, "m", "0-255", 6, NOW + timedelta(hours=5))
    assert not booster.fresh(document, "m", "0-255", 6, NOW + timedelta(hours=7))
    assert not booster.fresh(document, "m", "0-100", 6, NOW), "other ranges are never fresh"
    assert not booster.fresh(document, "m", "0-255", 6, NOW + timedelta(hours=19)), "an expired lease is not fresh"
