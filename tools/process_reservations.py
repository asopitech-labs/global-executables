"""Drain a bounded batch of central reservations in the scheduled Actions job."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from global_executables.acquisition_reservation import GitHubReservationStore
from global_executables.discovery import PackageDiscovery
from global_executables.on_demand import ROOT, PROJECTION, persist_observation


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True)


def publish(root: Path) -> bool:
    git(root, "add", "-f", str(ROOT), str(PROJECTION))
    changed = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"]).returncode != 0
    if not changed:
        return False
    git(root, "commit", "-m", "Record centrally reserved package observation")
    for attempt in range(3):
        git(root, "fetch", "origin", "artifact-data")
        git(root, "rebase", "origin/artifact-data")
        result = subprocess.run(["git", "-C", str(root), "push", "origin", "HEAD:artifact-data"])
        if result.returncode == 0:
            return True
    raise RuntimeError("artifact-data push failed after three attempts")


def run(root: Path, limit: int = 50) -> int:
    store = GitHubReservationStore(os.environ["GH_TOKEN"])
    engine = PackageDiscovery()
    worker_id = f"actions-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    git(root, "config", "user.name", "github-actions[bot]")
    git(root, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    changed = 0
    for item in store.list_pending(limit):
        now = utc_now()
        lease = store.claim(item["key"], worker_id, now.isoformat(), lease_seconds=600)
        if lease is None:
            continue
        lease_id = lease["lease"]["id"]
        try:
            observation = engine.discover(lease["ecosystem"], lease["package"], revalidate=True)
            if observation.get("outcome") not in {"verified_entry_point", "unverified", "not_published"}:
                raise RuntimeError(f"revalidation outcome: {observation.get('outcome')}")
            result = persist_observation(root, observation)
            if publish(root):
                changed += 1
            store.mark_processed(lease["key"], lease_id, utc_now().isoformat(),
                                 observed_version=observation.get("version"),
                                 evidence_digest=result["claim"]["event_digest"],
                                 outcome=observation["outcome"])
        except Exception as error:
            store.mark_retryable(lease["key"], lease_id, str(error)[:256],
                                 (utc_now() + timedelta(minutes=30)).isoformat())
            raise
    return changed


if __name__ == "__main__":
    root = Path(sys.argv[1])
    changed = run(root)
    print(json.dumps({"changed": changed}))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"changed={changed}\n")
