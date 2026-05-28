"""
Tests for the news module (the network-free part).

After the multi-instrument refactor, news.py no longer filters by keyword —
all items from the feed go to Claude, which decides relevance itself.
The old is_btc_relevant() helper was removed.

We test:
  - _entry_timestamp() helper
  - fetch_news_for() feed selection logic (crypto vs stock)
  - fetch_news() backwards-compat shim still importable
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from bot.data.news import fetch_news_for, fetch_news, CRYPTO_FEEDS, _stock_feeds
from bot.config import InstrumentConfig


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_instrument(symbol: str, kind: str) -> InstrumentConfig:
    return InstrumentConfig(
        symbol=symbol,
        kind=kind,
        timeframe_primary="1H",
        timeframe_context="4H",
    )


def _make_feed_entry(title: str, published_offset_hours: int = -1):
    """Fake feedparser entry published N hours ago."""
    ts = datetime.now(timezone.utc) + timedelta(hours=published_offset_hours)
    entry = MagicMock()
    entry.title = title
    entry.summary = "summary"
    entry.link = "https://example.com/news"
    # feedparser uses time.struct_time (9-tuple)
    entry.published_parsed = ts.timetuple()
    entry.updated_parsed = None
    return entry


# ─────────────────────────────────────────────────────────────────────────────
# Feed selection
# ─────────────────────────────────────────────────────────────────────────────

def test_crypto_instrument_uses_crypto_feeds():
    """fetch_news_for crypto → picks CRYPTO_FEEDS (CoinDesk, CoinTelegraph)."""
    inst = _make_instrument("BTC/USD", "crypto")
    with patch("bot.data.news._fetch_from_feeds", return_value=[]) as mock_fetch:
        fetch_news_for(inst, hours=24)
    mock_fetch.assert_called_once()
    feeds_arg = mock_fetch.call_args[0][0]
    sources = [name for name, _ in feeds_arg]
    assert "CoinDesk" in sources
    assert "CoinTelegraph" in sources


def test_stock_instrument_uses_per_ticker_feeds():
    """fetch_news_for stock → picks Yahoo Finance + Finviz feeds for that ticker."""
    inst = _make_instrument("NVDA", "stock")
    with patch("bot.data.news._fetch_from_feeds", return_value=[]) as mock_fetch:
        fetch_news_for(inst, hours=24)
    mock_fetch.assert_called_once()
    feeds_arg = mock_fetch.call_args[0][0]
    sources = [name for name, _ in feeds_arg]
    assert "Yahoo Finance" in sources
    assert "Finviz" in sources
    # URLs must contain the ticker symbol
    for _, url in feeds_arg:
        assert "NVDA" in url


def test_stock_feeds_contain_ticker():
    """_stock_feeds embeds ticker in both URLs."""
    feeds = _stock_feeds("IONQ")
    for _, url in feeds:
        assert "IONQ" in url



# ─────────────────────────────────────────────────────────────────────────────
# Backwards-compat shim
# ─────────────────────────────────────────────────────────────────────────────

def test_fetch_news_shim_importable():
    """fetch_news() shim exists and returns a list (even if feed is unreachable)."""
    with patch("bot.data.news._fetch_from_feeds", return_value=[]):
        result = fetch_news(hours=1)
    assert isinstance(result, list)


# ─────────────────────────────────────────────────────────────────────────────
# _entry_timestamp
# ─────────────────────────────────────────────────────────────────────────────

def test_entry_timestamp_parsed():
    """_entry_timestamp returns a UTC datetime from published_parsed."""
    from bot.data.news import _entry_timestamp
    entry = MagicMock()
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    entry.published_parsed = ts.timetuple()
    entry.updated_parsed = None
    result = _entry_timestamp(entry)
    assert result is not None
    assert result.year == 2026
    assert result.tzinfo == timezone.utc


def test_entry_timestamp_none_when_missing():
    """_entry_timestamp returns None if both published and updated are absent."""
    from bot.data.news import _entry_timestamp
    entry = MagicMock()
    entry.published_parsed = None
    entry.updated_parsed = None
    assert _entry_timestamp(entry) is None
