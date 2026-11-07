#!/usr/bin/env python3
"""Tests for the jitter and seen-pages changes; stdlib only, no browser needed.

Run: python3 tests/test_jitter_seen_pages.py
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bahamut_auto_bump import (
    PostInfo,
    _day_jitter_seconds,
    _inventory_candidates,
    _inventory_from_posts,
    _jittered_seconds,
    _retry_delay_seconds,
    _should_skip_page,
)


def _config(**overrides):
    config = {
        "browser": {"navigation_interval_seconds": 3, "navigation_jitter_seconds": 2},
        "schedule": {
            "timezone": "Asia/Taipei",
            "time": "09:00",
            "retry_interval_seconds": 900,
            "retry_jitter_seconds": 300,
            "retry_backoff_max_seconds": 7200,
            "schedule_jitter_seconds": 1800,
        },
    }
    for section, values in overrides.items():
        config.setdefault(section, {}).update(values)
    return config


def test_jittered_seconds():
    for _ in range(200):
        value = _jittered_seconds(3, 2)
        assert 3.0 <= value <= 5.0, value
    assert _jittered_seconds(3, 0) == 3.0
    assert _jittered_seconds(-5, -2) == 0.0


def test_day_jitter_is_stable_within_a_day():
    config = _config()
    now = datetime(2026, 11, 5, 9, 0, tzinfo=ZoneInfo("Asia/Taipei"))
    first = _day_jitter_seconds(now, config)
    assert 0 <= first <= 1800
    assert _day_jitter_seconds(now, config) == first
    assert _day_jitter_seconds(datetime(2026, 11, 5, 23, 59, tzinfo=ZoneInfo("Asia/Taipei")), config) == first
    assert _day_jitter_seconds(now, _config(schedule={"schedule_jitter_seconds": 0})) == 0


def test_retry_delay_backoff_and_cap():
    config = _config()
    first = _retry_delay_seconds(config, 1)
    assert 900 <= first <= 1200, first
    second = _retry_delay_seconds(config, 2)
    assert 1800 <= second <= 2100, second
    third = _retry_delay_seconds(config, 3)
    assert 3600 <= third <= 3900, third
    capped = _retry_delay_seconds(config, 10)
    assert 7200 <= capped <= 7500, capped


def test_should_skip_page():
    seen = {1, 2, 3}
    assert _should_skip_page(2, 21, seen, True)
    assert not _should_skip_page(21, 21, seen, True)  # last page is never skipped
    assert not _should_skip_page(2, 21, seen, False)
    assert not _should_skip_page(4, 21, seen, True)
    assert not _should_skip_page(1, 1, {1}, True)  # single-page thread: it is the last page


def _post(sn, floor=2, owner=True, bump=True, raw="2026-11-05 09:00:00"):
    return PostInfo(floor, str(sn), owner, raw, bump)


def test_inventory_from_posts():
    posts = [
        _post(100, floor=1),  # floor 1 is never a bump candidate
        _post(101),
        _post(102, owner=False),  # someone else's reply
        _post(103, bump=False),  # own reply, not the template
        _post(104),
    ]
    inventory = _inventory_from_posts(posts, _config())
    assert [entry["sn"] for entry in inventory] == ["101", "104"]
    assert inventory[0]["date"] == "2026-11-05"


def test_inventory_candidates():
    inventory = [
        {"sn": "101", "floor": 2, "date": "2026-11-03"},
        {"sn": "102", "floor": 3, "date": "2026-11-04"},
        {"sn": "103", "floor": 4, "date": "2026-11-05"},
    ]
    assert [entry["sn"] for entry in _inventory_candidates(inventory, 1)] == ["102", "101"]
    assert [entry["sn"] for entry in _inventory_candidates(inventory, 0)] == ["103", "102", "101"]
    assert _inventory_candidates(inventory, 5) == []
    assert _inventory_candidates([{"sn": "abc"}], 0) == []


if __name__ == "__main__":
    test_jittered_seconds()
    test_day_jitter_is_stable_within_a_day()
    test_retry_delay_backoff_and_cap()
    test_should_skip_page()
    test_inventory_from_posts()
    test_inventory_candidates()
    print("all tests passed")
