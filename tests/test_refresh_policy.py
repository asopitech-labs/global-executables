import json
from pathlib import Path

import pytest

from global_executables import refresh_policy as policy

GOLDEN = Path(__file__).resolve().parents[1] / "internal" / "gocrawl" / "testdata" / "refresh" / "policy-golden.json"


def test_recheck_interval_matches_the_go_implementation_table():
    golden = json.loads(GOLDEN.read_text())
    assert len(golden["cases"]) >= 20
    for case in golden["cases"]:
        assert policy.recheck_interval(case["module"], case["streak"], golden["max_days"]) == case["interval"], case


def test_check_round_trip_and_garbage():
    assert policy.unpack(policy.pack(20000, 3, "1.2.3")) == (20000, 3, "1.2.3")
    assert policy.unpack("v1:2:x:y") is None
    assert policy.unpack("1:2:x:y") == (1, 2, "x:y")
    for bad in ("", "1", "1:2", "x:1:v", "1:y:v", "-1:0:v", None, 5):
        assert policy.unpack(bad) is None


def test_due_follows_backoff_cap_and_due_floor():
    checked = {"m": policy.pack(100, 0, "1")}
    assert not policy.is_due(checked, "m", 100, 14)
    assert policy.is_due(checked, "m", 101, 14)
    checked = {"m": policy.pack(100, 30, "1")}
    assert not policy.is_due(checked, "m", 113, 14)
    assert policy.is_due(checked, "m", 114, 14)
    assert policy.is_due(checked, "m", 101, 14, due_floor=101), "a resync makes every older check due"
    assert policy.is_due({}, "unknown", 5, 14)


def test_record_check_counts_unchanged_streak_and_restarts_on_change():
    checked: dict[str, str] = {}
    assert policy.record_check(checked, "m", "1", 10) is True
    assert policy.record_check(checked, "m", "1", 11) is False
    assert policy.unpack(checked["m"]) == (11, 1, "1")
    assert policy.record_check(checked, "m", "2", 12) is True
    assert policy.unpack(checked["m"]) == (12, 0, "2")
    policy.forget(checked, "m")
    assert "m" not in checked


def test_seed_from_rows_makes_the_first_recheck_cheap_but_due():
    checked = {"kept": policy.pack(50, 2, "9")}
    seeded = policy.seed_from_rows(checked, [{"package": "a", "version": "1.0"}, {"package": "kept", "version": "0"},
                                              {"package": "", "version": "1"}, {"package": "b"}])
    assert seeded == 1 and checked["a"] == "0:0:1.0" and checked["kept"] == "50:2:9"
    assert policy.is_due(checked, "a", 1, 14) and policy.known_version(checked, "a") == "1.0"


def test_ttl_classification_and_summary():
    assert [policy.classify(age, 10, 120) for age in (0, 9, 10, 119, 120)] == ["fresh", "fresh", "due", "due", "stale"]
    checked = {"a": policy.pack(130, 0, "1"), "b": policy.pack(0, 0, "1"), "c": "garbage", "d": policy.pack(129, 0, "1")}
    summary = policy.ttl_summary(checked, 130, 14)
    assert summary == {"fresh": 1, "due": 1, "stale": 1, "unchecked": 1}


def test_token_bucket_reserves_in_order_and_refills():
    now = [0.0]
    slept: list[float] = []
    bucket = policy.TokenBucket(rate=2.0, burst=2, clock=lambda: now[0], sleep=slept.append)
    assert bucket.reserve() == 0 and bucket.reserve() == 0
    assert bucket.reserve() == pytest.approx(0.5), "the third caller queues behind the first two"
    assert bucket.reserve() == pytest.approx(1.0), "reservations stack, they are not reused"
    now[0] = 10.0
    assert bucket.reserve() == 0, "the bucket refills up to its burst"
    assert bucket.wait() == 0 and slept == []
    assert bucket.wait() == pytest.approx(0.5) and slept == [pytest.approx(0.5)]
    with pytest.raises(ValueError):
        policy.TokenBucket(0)


def test_feed_floor_defers_young_announcements():
    assert policy.feed_ready_at(1000, 60) == 1060
