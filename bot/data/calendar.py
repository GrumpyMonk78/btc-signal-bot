"""
Macro economic calendar — high-impact US events that move BTC.

Phase 1 implementation: hardcoded events for 2026 H1+H2. Maintain by hand
from a calendar source like https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
or https://www.bls.gov/schedule/news_release/.

Phase 2: scrape ForexFactory or Investing.com calendar XML. Not needed yet
— the bot has at most one decision call per few hours, so a few hours of
calendar staleness costs nothing.

API
---
    upcoming_within(hours: int) -> list[MacroEvent]
    recent_within(hours: int) -> list[MacroEvent]
    is_in_blackout_window(blackout_minutes: int) -> tuple[bool, MacroEvent | None]
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.storage.models import MacroEvent


# ─────────────────────────────────────────────────────────────────────────────
# Static calendar — UTC times
# ─────────────────────────────────────────────────────────────────────────────
#
# Times in UTC. FOMC press conferences are typically 18:30 UTC (2:30 PM ET).
# CPI / NFP releases are 12:30 UTC (8:30 AM ET).
#
# UPDATE THIS quarterly. The bot warns if the next event is more than
# ~30 days away (probably means the calendar is stale).

_CALENDAR_2026: list[tuple[str, str, str]] = [
    # (ISO datetime UTC, event name, region)
    # ── FOMC ──────────────────────────────────────────────────────────────
    ("2026-01-28T19:00:00+00:00", "FOMC Rate Decision",       "US"),
    ("2026-03-18T18:00:00+00:00", "FOMC Rate Decision + SEP", "US"),
    ("2026-04-29T18:00:00+00:00", "FOMC Rate Decision",       "US"),
    ("2026-06-17T18:00:00+00:00", "FOMC Rate Decision + SEP", "US"),
    ("2026-07-29T18:00:00+00:00", "FOMC Rate Decision",       "US"),
    ("2026-09-16T18:00:00+00:00", "FOMC Rate Decision + SEP", "US"),
    ("2026-11-04T19:00:00+00:00", "FOMC Rate Decision",       "US"),
    ("2026-12-16T19:00:00+00:00", "FOMC Rate Decision + SEP", "US"),
    # ── CPI (mid-month, ~12:30 UTC) ───────────────────────────────────────
    ("2026-01-14T13:30:00+00:00", "CPI",  "US"),
    ("2026-02-11T13:30:00+00:00", "CPI",  "US"),
    ("2026-03-12T12:30:00+00:00", "CPI",  "US"),
    ("2026-04-14T12:30:00+00:00", "CPI",  "US"),
    ("2026-05-13T12:30:00+00:00", "CPI",  "US"),
    ("2026-06-10T12:30:00+00:00", "CPI",  "US"),
    ("2026-07-15T12:30:00+00:00", "CPI",  "US"),
    ("2026-08-12T12:30:00+00:00", "CPI",  "US"),
    # ── NFP (first Friday of month, 12:30 UTC) ────────────────────────────
    ("2026-01-09T13:30:00+00:00", "NFP",  "US"),
    ("2026-02-06T13:30:00+00:00", "NFP",  "US"),
    ("2026-03-06T13:30:00+00:00", "NFP",  "US"),
    ("2026-04-03T12:30:00+00:00", "NFP",  "US"),
    ("2026-05-01T12:30:00+00:00", "NFP",  "US"),
    ("2026-06-05T12:30:00+00:00", "NFP",  "US"),
    ("2026-07-02T12:30:00+00:00", "NFP",  "US"),
    ("2026-08-07T12:30:00+00:00", "NFP",  "US"),
]


def _calendar() -> list[MacroEvent]:
    """Materialise the static table into MacroEvent instances."""
    out: list[MacroEvent] = []
    for ts_iso, name, region in _CALENDAR_2026:
        out.append(MacroEvent(
            timestamp=datetime.fromisoformat(ts_iso),
            name=name,
            importance="high",
            region=region,
        ))
    return out


# Module-level cache; refresh manually when _CALENDAR_2026 changes.
_EVENTS: list[MacroEvent] = sorted(_calendar(), key=lambda e: e.timestamp)


def all_events() -> list[MacroEvent]:
    """All known events, sorted by time."""
    return list(_EVENTS)


# ─────────────────────────────────────────────────────────────────────────────
# Lookups (relative to "now")
# ─────────────────────────────────────────────────────────────────────────────


def upcoming_within(hours: int, now: datetime | None = None) -> list[MacroEvent]:
    """Events strictly in the future, within `hours` of `now`. Sorted oldest first."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    return [e for e in _EVENTS if now < e.timestamp <= horizon]


def recent_within(hours: int, now: datetime | None = None) -> list[MacroEvent]:
    """Events that already happened, within `hours` of `now`. Sorted oldest first."""
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(hours=hours)
    return [e for e in _EVENTS if floor <= e.timestamp <= now]


def is_in_blackout_window(
    blackout_minutes: int, now: datetime | None = None
) -> tuple[bool, MacroEvent | None]:
    """Is `now` within ±blackout_minutes of any high-impact event?

    Returns (True, event) if blackout is active, (False, None) otherwise.
    Used by the risk manager to veto signals around macro releases.
    """
    now = now or datetime.now(timezone.utc)
    window = timedelta(minutes=blackout_minutes)
    for ev in _EVENTS:
        if abs(ev.timestamp - now) <= window:
            return True, ev
    return False, None
