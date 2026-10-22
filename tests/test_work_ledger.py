"""Coordinator-free Git work ledger semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from global_executables import work_ledger

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64
COMMIT = "b" * 40


def initialized(size=256):
    document = work_ledger.empty_document("npm-registry")
    sizes = {work_ledger.unit_id(bucket): size // 256 + (bucket < size % 256)
             for bucket in range(256)}
    positions = {key: 0 for key in sizes}
    return work_ledger.ensure_catalog(document, "npm-registry", {
        "digest": DIGEST, "size": size, "snapshot": "2026-10-21", "source": "fixture",
    }, sizes, positions)


def test_independent_actors_claim_disjoint_work_without_a_scheduler():
    document, first = work_ledger.claim(initialized(), "tokyo", now=NOW)
    document, second = work_ledger.claim(document, "osaka", now=NOW)

    assert first["units"] != second["units"]
    assert set(first["units"]).isdisjoint(second["units"])
    assert work_ledger.status(document, NOW)["live_reservations"] == {
        "tokyo": first, "osaka": second}


def test_expired_reservation_returns_to_the_available_pool():
    document, reservation = work_ledger.claim(initialized(1), "offline", now=NOW, ttl_hours=1)
    with pytest.raises(work_ledger.NoWork):
        work_ledger.claim(document, "waiting", now=NOW + timedelta(minutes=30))

    reclaimed, next_reservation = work_ledger.claim(
        document, "waiting", now=NOW + timedelta(hours=2))

    assert next_reservation["units"] == reservation["units"]
    assert "waiting" in reclaimed["reservations"]


def test_checkpoint_is_monotonic_and_stale_actor_cannot_overwrite_new_owner():
    document, reservation = work_ledger.claim(initialized(2560), "first", now=NOW, ttl_hours=1)
    unit = reservation["units"][0]
    document = work_ledger.checkpoint(document, "first", unit, 5, failures=1,
                                      complete=False, result_commit=COMMIT, now=NOW)
    assert document["units"][unit]["position"] == 5

    document, replacement = work_ledger.claim(document, "second", now=NOW + timedelta(hours=2))
    assert replacement["units"]
    with pytest.raises(work_ledger.LedgerConflict):
        work_ledger.checkpoint(document, "first", unit, 6, failures=0,
                               complete=False, result_commit=COMMIT,
                               now=NOW + timedelta(hours=2))


def test_completion_requires_durable_evidence_and_a_finished_unit():
    document, reservation = work_ledger.claim(initialized(), "worker", now=NOW)
    unit = reservation["units"][0]
    with pytest.raises(ValueError, match="completed unit"):
        work_ledger.checkpoint(document, "worker", unit, 0, failures=0,
                               complete=True, result_commit=COMMIT, now=NOW)

    document = work_ledger.checkpoint(document, "worker", unit, 1, failures=0,
                                      complete=True, result_commit=COMMIT, now=NOW)
    assert document["units"][unit]["status"] == "completed"
    assert document["units"][unit]["result_commit"] == COMMIT


def test_new_catalog_generation_reopens_units_without_rewinding_positions():
    document, reservation = work_ledger.claim(initialized(), "worker", now=NOW)
    unit = reservation["units"][0]
    document = work_ledger.checkpoint(document, "worker", unit, 1, failures=0,
                                      complete=True, result_commit=COMMIT, now=NOW)
    sizes = {work_ledger.unit_id(bucket): 1 for bucket in range(256)}
    sizes[unit] = 2
    updated = work_ledger.ensure_catalog(document, "npm-registry", {
        "digest": "sha256:" + "c" * 64, "size": 257,
        "snapshot": "2026-10-22", "source": "fixture",
    }, sizes, {key: 0 for key in sizes})

    assert updated["units"][unit]["position"] == 1
    assert updated["units"][unit]["status"] == "available"
    assert updated["reservations"] == {}
    assert updated["history"][-1]["catalog"]["digest"] == DIGEST
