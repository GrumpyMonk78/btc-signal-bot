"""
Crypto news ingestion.

We fetch RSS feeds (free, no API key) and filter to BTC-relevant items.
Two feeds for phase 1:
  - CoinDesk:      https://www.coindesk.com/arc/outboundfeeds/rss/
  - CoinTelegraph: https://cointelegraph.com/rss

Both are RSS 2.0; `feedparser` handles them uniformly.

API
---
    fetch_news(hours: int = 24) -> list[NewsItem]
    is_btc_relevant(title: str, summary: str = "") -> bool

The fetch is synchronous and uncached for now. The orchestrator should
call it at most once per decision (≈ once per scanner trigger), so latency
isn't critical and a Cache layer adds complexity we don't need yet.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import feedparser

from bot.storage.models import NewsItem

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

FEEDS: list[tuple[str, str]] = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
]

# Keywords that mark a story as BTC-relevant. Case-insensitive substring match.
# Tuned for *signal*, not coverage — we want fewer false positives over more
# headlines, because Claude can only weigh so many at once.
BTC_KEYWORDS: tuple[str, ...] = (
    "bitcoin", "btc/", " btc ", "btc:", "btc,", "btc.",
    "spot etf", "bitcoin etf", "ibit", "fbtc",
    "satoshi", "halving", "mining hashrate",
    "microstrategy", "mstr", "saylor",
    "binance", "coinbase", "kraken",
    "sec ", "cftc", "treasury",
    "fed ", "fomc", "powell", "rate cut", "rate hike",
    "cpi ", "ppi ", "pce ", "nfp", "jobs report",
)

# Keywords that strongly suggest the story is NOT about BTC — used to filter
# out altcoin-only news that happens to mention "Bitcoin" once in passing.
NEGATIVE_KEYWORDS: tuple[str, ...] = (
    " solana ", " sol ", " ada ", " cardano ", " doge ", " dogecoin ",
    " ripple ", " xrp ", " pepe ", " shiba ", " bonk ",
)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def is_btc_relevant(title: str, summary: str = "") -> bool:
    """Heuristic: does this headline matter for BTC?"""
    blob = f" {title.lower()} {summary.lower()} "

    # Hard negatives: altcoin-only stories
    if any(neg in blob for neg in NEGATIVE_KEYWORDS):
        # ...unless BTC is explicitly named
        if "bitcoin" not in blob and "btc" not in blob:
            return False

    return any(kw in blob for kw in BTC_KEYWORDS)


def fetch_news(hours: int = 24, max_items: int = 20) -> list[NewsItem]:
    """Fetch + filter recent crypto news.

    Returns up to `max_items` BTC-relevant headlines from the last `hours`,
    newest first.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[NewsItem] = []

    for source_name, url in FEEDS:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("news.fetch_news: %s failed: %s", source_name, exc)
            continue

        for entry in parsed.entries:
            ts = _entry_timestamp(entry)
            if ts is None or ts < cutoff:
                continue

            title = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            url_ = getattr(entry, "link", "").strip()

            if not title:
                continue
            if not is_btc_relevant(title, summary):
                continue

            items.append(
                NewsItem(
                    timestamp=ts,
                    source=source_name,
                    title=title[:512],
                    summary=summary[:2048],
                    url=url_[:1024],
                )
            )

    # Newest first, capped.
    items.sort(key=lambda n: n.timestamp, reverse=True)
    return items[:max_items]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _entry_timestamp(entry) -> datetime | None:
    """Pull a tz-aware UTC datetime from a feedparser entry."""
    # feedparser populates `published_parsed` (time.struct_time) when possible
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
