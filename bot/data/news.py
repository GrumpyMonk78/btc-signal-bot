"""
Univerzalni news ingestion - funguje pro crypto i akcie.

Zdroje dle typu instrumentu:
  crypto -> CoinDesk RSS + CoinTelegraph RSS
  stock  -> Yahoo Finance RSS + Finviz RSS (per-ticker, bez API klice)

Zadne filtrovani klicovymi slovy - vsechny zpravy z feedu jdou primo
Claudovi, ktery sam posudi co je relevantni pro dane rozhodnuti.
Yahoo Finance a Finviz RSS jsou jiz per-ticker, takze jsou prirozene
relevantni. CoinDesk/CoinTelegraph jsou crypto-specificke.

Verejna API:
    fetch_news_for(instrument, hours=24, max_items=15) -> list[NewsItem]
    fetch_news(hours=24, max_items=20) -> list[NewsItem]   # BTC zpetna kompatibilita
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import feedparser

from bot.storage.models import NewsItem

if TYPE_CHECKING:
    from bot.config import InstrumentConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

CRYPTO_FEEDS: list[tuple[str, str]] = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph", "https://cointelegraph.com/rss"),
]

FEEDS = CRYPTO_FEEDS  # alias pro zpetnou kompatibilitu


def _stock_feeds(ticker: str) -> list[tuple[str, str]]:
    """Per-ticker RSS feeds pro akcie."""
    return [
        ("Yahoo Finance",
         "https://feeds.finance.yahoo.com/rss/2.0/headline"
         "?s=" + ticker + "&region=US&lang=en-US"),
        ("Finviz",
         "https://finviz.com/rss.ashx?t=" + ticker),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry_timestamp(entry) -> datetime | None:
    parsed = (
        getattr(entry, "published_parsed", None)
        or getattr(entry, "updated_parsed", None)
    )
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _fetch_from_feeds(
    feeds: list[tuple[str, str]],
    cutoff: datetime,
    max_items: int,
) -> list[NewsItem]:
    """Stahne vsechny zpravy z feedu bez filtrovani - Claude posudi relevanci sam."""
    items: list[NewsItem] = []
    for source_name, url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:
            logger.warning("news: %s feed failed: %s", source_name, exc)
            continue
        if getattr(parsed, "bozo", False) and not parsed.entries:
            logger.debug("news: %s prazdny/bozo feed", source_name)
            continue
        for entry in parsed.entries:
            ts = _entry_timestamp(entry)
            if ts is None or ts < cutoff:
                continue
            title   = getattr(entry, "title",   "").strip()
            summary = getattr(entry, "summary", "").strip()
            url_    = getattr(entry, "link",    "").strip()
            if not title:
                continue
            items.append(NewsItem(
                timestamp=ts,
                source=source_name,
                title=title[:512],
                summary=summary[:2048],
                url=url_[:1024],
            ))
    items.sort(key=lambda n: n.timestamp, reverse=True)
    return items[:max_items]


# ---------------------------------------------------------------------------
# Verejna API
# ---------------------------------------------------------------------------

def fetch_news_for(
    instrument: "InstrumentConfig",
    hours: int = 24,
    max_items: int = 15,
) -> list[NewsItem]:
    """Stahne zpravy pro dany instrument a preda je Claudovi bez filtrovani.

    Pro crypto: CoinDesk + CoinTelegraph (prirozene crypto-specificke).
    Pro stock:  Yahoo Finance + Finviz per-ticker RSS (prirozene per-symbol).
    Claude sam posudi co je relevantni - zadne klicove slovo filtry.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    if instrument.kind == "crypto":
        feeds = CRYPTO_FEEDS
    elif instrument.kind == "stock":
        feeds = _stock_feeds(instrument.symbol.upper())
    else:
        logger.warning("news: nezname kind %r pro %s", instrument.kind, instrument.symbol)
        return []

    items = _fetch_from_feeds(feeds, cutoff, max_items)
    logger.info(
        "news [%s]: %d zprav za poslednich %dh z %d feedu",
        instrument.symbol, len(items), hours, len(feeds),
    )
    return items


def fetch_news(hours: int = 24, max_items: int = 20) -> list[NewsItem]:
    """Zpetna kompatibilita - vsechny zpravy z crypto feedu pro BTC."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return _fetch_from_feeds(CRYPTO_FEEDS, cutoff, max_items)
