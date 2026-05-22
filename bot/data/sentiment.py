"""
Sentiment data — Fear & Greed Index from alternative.me.

Free, no API key. Endpoint:
    https://api.alternative.me/fng/?limit=N&format=json

Response shape (relevant fields):
    {
      "data": [
        {"value": "62", "value_classification": "Greed",
         "timestamp": "1700000000", "time_until_update": "..."},
        ...
      ]
    }

API
---
    fetch_fear_greed(history_days: int = 7) -> SentimentSnapshot
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from bot.storage.models import SentimentSnapshot

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.alternative.me/fng/"
DEFAULT_TIMEOUT = 10.0


def fetch_fear_greed(history_days: int = 7) -> SentimentSnapshot:
    """Return current F&G value + classification + last N-day trend.

    Raises
    ------
    httpx.HTTPError
        On network / HTTP failure. Caller decides whether to swallow.
    ValueError
        If the response shape is unexpected.
    """
    # We ask for history_days+1 to be safe; the API includes the current day
    # as item 0.
    params = {"limit": str(history_days + 1), "format": "json"}
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.get(ENDPOINT, params=params)
        resp.raise_for_status()
        payload = resp.json()

    data = payload.get("data")
    if not data or not isinstance(data, list):
        raise ValueError(f"Unexpected F&G payload shape: {payload!r}")

    current = data[0]
    try:
        value = int(current["value"])
        classification = str(current["value_classification"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Malformed current F&G entry: {current!r}") from exc

    # Build trend_7d, oldest first
    trend_raw = list(reversed(data[1 : history_days + 1])) if len(data) > 1 else []
    trend: list[int] = []
    for entry in trend_raw:
        try:
            trend.append(int(entry["value"]))
        except (KeyError, ValueError):
            # skip malformed days rather than erroring out the snapshot
            logger.warning("sentiment.fetch_fear_greed: skipping malformed entry %r", entry)

    return SentimentSnapshot(value=value, classification=classification, trend_7d=trend)


def latest_timestamp(snapshot_payload: dict) -> datetime | None:
    """Helper for tests / introspection: pull the timestamp of the most recent
    data point from a raw API payload."""
    try:
        ts = int(snapshot_payload["data"][0]["timestamp"])
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (KeyError, IndexError, ValueError, TypeError):
        return None
