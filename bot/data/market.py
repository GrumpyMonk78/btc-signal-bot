"""
Market data layer.

Defines an abstract `MarketDataProvider` interface and an `AlpacaProvider`
implementation for BTC/USD (Alpaca crypto). Other modules depend on the
interface, not the concrete provider — this keeps the data source swappable
(e.g. for backtest replay or alternative providers later).

Conventions
-----------
- All timestamps are tz-aware UTC.
- Returned DataFrame has DatetimeIndex (name='timestamp', tz='UTC') and
  columns: ['open', 'high', 'low', 'close', 'volume', 'vwap', 'trade_count'].
- Bars are returned in chronological order (oldest → newest).
- `limit` is the maximum number of bars; provider may return fewer if data
  isn't available that far back.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe mapping
# ─────────────────────────────────────────────────────────────────────────────

# Map our config strings to Alpaca's TimeFrame objects (resolved lazily inside
# AlpacaProvider to avoid importing alpaca-py at module import time, which
# helps unit tests that mock the provider).
_TIMEFRAME_MINUTES: dict[str, int] = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "30Min": 30,
    "1H": 60,
    "4H": 240,
    "1D": 1440,
}


def timeframe_to_minutes(tf: str) -> int:
    """Return the number of minutes in a timeframe string."""
    try:
        return _TIMEFRAME_MINUTES[tf]
    except KeyError as exc:
        raise ValueError(f"Unknown timeframe: {tf!r}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Provider interface
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BarRequest:
    """Parameters for a historical bar fetch."""

    symbol: str
    timeframe: str
    limit: int = 200
    end: datetime | None = None  # None = now


class MarketDataProvider(ABC):
    """Abstract market data source. Implementations must be safe to call
    concurrently from async code (sync calls inside thread executor are OK)."""

    @abstractmethod
    def fetch_bars(self, req: BarRequest) -> pd.DataFrame:
        """Return OHLCV bars as a DataFrame. See module docstring for shape."""

    def fetch_primary_and_context(
        self, symbol: str, limit_primary: int = 200, limit_context: int = 200
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Convenience: fetch both timeframes configured for the bot."""
        primary = self.fetch_bars(
            BarRequest(symbol=symbol, timeframe=settings.timeframe_primary, limit=limit_primary)
        )
        context = self.fetch_bars(
            BarRequest(symbol=symbol, timeframe=settings.timeframe_context, limit=limit_context)
        )
        return primary, context


# ─────────────────────────────────────────────────────────────────────────────
# Alpaca implementation
# ─────────────────────────────────────────────────────────────────────────────


class AlpacaProvider(MarketDataProvider):
    """Alpaca crypto market data. Free tier with paper account is sufficient
    for crypto OHLCV (real-time, no separate subscription needed)."""

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        # Import lazily so the module can be imported without alpaca-py
        # installed (useful for environments that only run unit tests).
        from alpaca.data.historical import CryptoHistoricalDataClient

        self._key = api_key or settings.alpaca_api_key
        self._secret = api_secret or settings.alpaca_api_secret
        if not self._key or not self._secret:
            raise RuntimeError(
                "Alpaca credentials not configured. Set ALPACA_API_KEY and "
                "ALPACA_API_SECRET in .env (paper keys work fine for crypto data)."
            )
        # CryptoHistoricalDataClient works without keys for some endpoints but
        # we pass them anyway for higher rate limits and consistency.
        self._client = CryptoHistoricalDataClient(self._key, self._secret)

    # ── Alpaca TimeFrame helper ───────────────────────────────────────────
    @staticmethod
    def _to_alpaca_timeframe(tf: str):
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        mapping = {
            "1Min": TimeFrame(1, TimeFrameUnit.Minute),
            "5Min": TimeFrame(5, TimeFrameUnit.Minute),
            "15Min": TimeFrame(15, TimeFrameUnit.Minute),
            "30Min": TimeFrame(30, TimeFrameUnit.Minute),
            "1H": TimeFrame(1, TimeFrameUnit.Hour),
            "4H": TimeFrame(4, TimeFrameUnit.Hour),
            "1D": TimeFrame(1, TimeFrameUnit.Day),
        }
        try:
            return mapping[tf]
        except KeyError as exc:
            raise ValueError(f"Unsupported timeframe for Alpaca: {tf!r}") from exc

    # ── Public API ────────────────────────────────────────────────────────
    def fetch_bars(self, req: BarRequest) -> pd.DataFrame:
        from alpaca.data.requests import CryptoBarsRequest

        end = req.end or datetime.now(timezone.utc)
        minutes = timeframe_to_minutes(req.timeframe)
        # Overshoot the lookback window so we get at least `limit` bars even
        # if the venue had a brief outage. Alpaca returns whatever exists.
        lookback = timedelta(minutes=minutes * req.limit * 2)
        start = end - lookback

        logger.debug(
            "alpaca.fetch_bars symbol=%s tf=%s limit=%d start=%s end=%s",
            req.symbol, req.timeframe, req.limit, start.isoformat(), end.isoformat(),
        )

        request = CryptoBarsRequest(
            symbol_or_symbols=req.symbol,
            timeframe=self._to_alpaca_timeframe(req.timeframe),
            start=start,
            end=end,
        )
        bars = self._client.get_crypto_bars(request)
        df = bars.df

        if df.empty:
            logger.warning("alpaca.fetch_bars returned empty df for %s %s", req.symbol, req.timeframe)
            return _empty_bars_df()

        # Alpaca returns a multi-index (symbol, timestamp) — flatten it.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(req.symbol, level="symbol")

        df = df.tail(req.limit).copy()
        df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
        df.index.name = "timestamp"

        # Normalise columns — Alpaca uses lowercase already, but be defensive.
        df.columns = [c.lower() for c in df.columns]
        expected = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]
        for col in expected:
            if col not in df.columns:
                df[col] = pd.NA
        return df[expected]


def _empty_bars_df() -> pd.DataFrame:
    """Schema-correct empty DataFrame for the no-data case."""
    idx = pd.DatetimeIndex([], name="timestamp", tz="UTC")
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in ["open", "high", "low", "close", "volume", "vwap", "trade_count"]},
        index=idx,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────


def default_provider() -> MarketDataProvider:
    """Return the configured default provider. For now: Alpaca."""
    return AlpacaProvider()
