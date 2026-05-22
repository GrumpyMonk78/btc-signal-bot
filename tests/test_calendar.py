"""
Tests for the macro calendar lookups.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.data import calendar as cal


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_calendar_has_events():
    events = cal.all_events()
    assert len(events) > 10
    # Sorted by timestamp.
    times = [e.timestamp for e in events]
    assert times == sorted(times)


def test_upcoming_within_window():
    # 2026-01-28T19:00Z = FOMC. Ask at 18:30 with 1h horizon → should find it.
    now = _at("2026-01-28T18:30:00+00:00")
    events = cal.upcoming_within(hours=1, now=now)
    assert len(events) == 1
    assert "FOMC" in events[0].name


def test_recent_within_window():
    # 2026-02-11T13:30Z = CPI. Ask at 14:00 with 1h lookback → should find it.
    now = _at("2026-02-11T14:00:00+00:00")
    events = cal.recent_within(hours=1, now=now)
    assert any("CPI" in e.name for e in events)


def test_blackout_window_active_around_event():
    # FOMC at 2026-01-28T19:00Z. Test ±30 min window.
    target = _at("2026-01-28T19:00:00+00:00")

    # Right on the event → blackout
    in_blackout, ev = cal.is_in_blackout_window(blackout_minutes=30, now=target)
    assert in_blackout
    assert ev is not None and "FOMC" in ev.name

    # 25 min before → blackout
    in_blackout, _ev = cal.is_in_blackout_window(
        blackout_minutes=30, now=target - timedelta(minutes=25)
    )
    assert in_blackout

    # 35 min before → safe
    in_blackout, _ev = cal.is_in_blackout_window(
        blackout_minutes=30, now=target - timedelta(minutes=35)
    )
    assert not in_blackout


def test_blackout_returns_none_when_quiet():
    # Random Wednesday with nothing scheduled
    now = _at("2026-03-25T05:00:00+00:00")
    in_blackout, ev = cal.is_in_blackout_window(blackout_minutes=30, now=now)
    assert not in_blackout
    assert ev is None
