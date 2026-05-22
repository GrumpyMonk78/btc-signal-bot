"""
Market data layer.

Definuje abstraktní `MarketDataProvider` a dvě Alpaca implementace:
  - AlpacaProvider      → crypto (BTC/USD, ETH/USD …)
  - AlpacaStocksProvider → akcie (NVDA, TSLA, IONQ …)

Nový kód by měl používat `provider_for(instrument)` nebo
`providers_for_all()` — ty automaticky vrátí správný provider
podle `InstrumentConfig.kind`.

Konvence
--------
- Všechny timestamps jsou tz-aware UTC.
- Vrácený DataFrame má DatetimeIndex (name='timestamp', tz='UTC') a
  sloupce: ['open', 'high', 'low', 'close', 'volume', 'vwap', 'trade_count'].
- Bary jsou seřazeny chronologicky (nejstarší → nejnovější).
- `limit` je maximální počet barů; provider může vrátit méně.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pandas as pd

from bot.config import settings

if TYPE_CHECKING:
    from bot.config import InstrumentConfig

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe mapping
# ─────────────────────────────────────────────────────────────────────────────

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
    """Vrátí počet minut odpovídající timeframe stringu."""
    try:
        return _TIMEFRAME_MINUTES[tf]
    except KeyError as exc:
        raise ValueError(f"Unknown timeframe: {tf!r}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Provider interface
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BarRequest:
    """Parametry pro stažení historických barů."""
    symbol: str
    timeframe: str
    limit: int = 200
    end: datetime | None = None  # None = now


class MarketDataProvider(ABC):
    """Abstraktní zdroj tržních dat."""

    @abstractmethod
    def fetch_bars(self, req: BarRequest) -> pd.DataFrame:
        """Vrátí OHLCV bary jako DataFrame. Viz konvence v hlavičce modulu."""

    def fetch_primary_and_context(
        self,
        symbol: str,
        timeframe_primary: str,
        timeframe_context: str,
        limit_primary: int = 200,
        limit_context: int = 200,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Stáhne oba timeframy najednou. Parametry timeframu bere explicitně
        (ne ze settings) aby fungovalo pro libovolný InstrumentConfig."""
        primary = self.fetch_bars(
            BarRequest(symbol=symbol, timeframe=timeframe_primary, limit=limit_primary)
        )
        context = self.fetch_bars(
            BarRequest(symbol=symbol, timeframe=timeframe_context, limit=limit_context)
        )
        return primary, context


# ─────────────────────────────────────────────────────────────────────────────
# Sdílená helper logika
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_df(df: pd.DataFrame, symbol: str, limit: int) -> pd.DataFrame:
    """Normalizuje DataFrame vrácený z Alpaca API na standardní formát."""
    if df.empty:
        return _empty_bars_df()

    # Alpaca vrací multi-index (symbol, timestamp) — flatten
    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.xs(symbol, level="symbol")
        except KeyError:
            # Někdy Alpaca vrátí jiný level name — zkusíme první level
            df = df.xs(symbol, level=0)

    df = df.tail(limit).copy()
    df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
    df.index.name = "timestamp"
    df.columns = [c.lower() for c in df.columns]

    expected = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]
    for col in expected:
        if col not in df.columns:
            df[col] = pd.NA
    return df[expected]


def _empty_bars_df() -> pd.DataFrame:
    """Schema-correct empty DataFrame pro případ že data nejsou dostupná."""
    idx = pd.DatetimeIndex([], name="timestamp", tz="UTC")
    return pd.DataFrame(
        {c: pd.Series(dtype="float64")
         for c in ["open", "high", "low", "close", "volume", "vwap", "trade_count"]},
        index=idx,
    )


def _alpaca_timeframe(tf: str):
    """Převede náš timeframe string na Alpaca TimeFrame objekt."""
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    mapping = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
        "1H":  TimeFrame(1, TimeFrameUnit.Hour),
        "4H":  TimeFrame(4, TimeFrameUnit.Hour),
        "1D":  TimeFrame(1, TimeFrameUnit.Day),
    }
    try:
        return mapping[tf]
    except KeyError as exc:
        raise ValueError(f"Unsupported timeframe for Alpaca: {tf!r}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Alpaca CRYPTO provider
# ─────────────────────────────────────────────────────────────────────────────

class AlpacaProvider(MarketDataProvider):
    """Alpaca crypto market data (BTC/USD, ETH/USD …).

    Funguje s free paper účtem — žádná extra subscription není potřeba.
    Symbol musí být ve formátu 'BASE/QUOTE', např. 'BTC/USD'.
    """

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        from alpaca.data.historical import CryptoHistoricalDataClient
        self._key = api_key or settings.alpaca_api_key
        self._secret = api_secret or settings.alpaca_api_secret
        if not self._key or not self._secret:
            raise RuntimeError(
                "Alpaca credentials not configured. Set ALPACA_API_KEY and "
                "ALPACA_API_SECRET in .env"
            )
        self._client = CryptoHistoricalDataClient(self._key, self._secret)

    def fetch_bars(self, req: BarRequest) -> pd.DataFrame:
        from alpaca.data.requests import CryptoBarsRequest

        end = req.end or datetime.now(timezone.utc)
        minutes = timeframe_to_minutes(req.timeframe)
        lookback = timedelta(minutes=minutes * req.limit * 2)
        start = end - lookback

        logger.debug(
            "alpaca_crypto.fetch_bars symbol=%s tf=%s limit=%d",
            req.symbol, req.timeframe, req.limit,
        )

        request = CryptoBarsRequest(
            symbol_or_symbols=req.symbol,
            timeframe=_alpaca_timeframe(req.timeframe),
            start=start,
            end=end,
        )
        bars = self._client.get_crypto_bars(request)
        df = bars.df

        if df.empty:
            logger.warning("alpaca_crypto: empty df for %s %s", req.symbol, req.timeframe)
            return _empty_bars_df()

        return _normalise_df(df, req.symbol, req.limit)


# ─────────────────────────────────────────────────────────────────────────────
# Alpaca STOCKS provider
# ─────────────────────────────────────────────────────────────────────────────

class AlpacaStocksProvider(MarketDataProvider):
    """Alpaca stock market data (NVDA, TSLA, IONQ …).

    Používá StockHistoricalDataClient — jiný endpoint než crypto.
    Symbol je plain ticker bez lomítka, např. 'NVDA'.

    Poznámka k datům:
    - V rámci market hours (9:30–16:00 ET) jsou data real-time.
    - Free Alpaca plán má 15-min delay pro SIP feed; IEX feed je real-time
      ale méně likvidní. Pro 1H timeframe to není problém.
    - Pre/post-market bary nejsou ve výchozím nastavení zahrnuty.
    """

    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        self._key = api_key or settings.alpaca_api_key
        self._secret = api_secret or settings.alpaca_api_secret
        if not self._key or not self._secret:
            raise RuntimeError(
                "Alpaca credentials not configured. Set ALPACA_API_KEY and "
                "ALPACA_API_SECRET in .env"
            )
        self._client = StockHistoricalDataClient(self._key, self._secret)

    def fetch_bars(self, req: BarRequest) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.enums import DataFeed

        end = req.end or datetime.now(timezone.utc)
        minutes = timeframe_to_minutes(req.timeframe)
        lookback = timedelta(minutes=minutes * req.limit * 2)
        start = end - lookback

        logger.debug(
            "alpaca_stocks.fetch_bars symbol=%s tf=%s limit=%d",
            req.symbol, req.timeframe, req.limit,
        )

        request = StockBarsRequest(
            symbol_or_symbols=req.symbol,
            timeframe=_alpaca_timeframe(req.timeframe),
            start=start,
            end=end,
            feed=DataFeed.IEX,   # IEX = real-time, SIP = 15min delay (needs subscription)
        )
        bars = self._client.get_stock_bars(request)
        df = bars.df

        if df.empty:
            logger.warning("alpaca_stocks: empty df for %s %s", req.symbol, req.timeframe)
            return _empty_bars_df()

        return _normalise_df(df, req.symbol, req.limit)


# ─────────────────────────────────────────────────────────────────────────────
# Factory — hlavní vstupní bod pro nový kód
# ─────────────────────────────────────────────────────────────────────────────

# Cache instancí — vytváříme klienty jen jednou, jsou thread-safe
_crypto_provider: AlpacaProvider | None = None
_stocks_provider: AlpacaStocksProvider | None = None


def provider_for(instrument: "InstrumentConfig") -> MarketDataProvider:
    """Vrátí správný provider pro daný InstrumentConfig (singleton cache).

    Použití:
        from bot.config import get_enabled_instruments
        from bot.data.market import provider_for

        for inst in get_enabled_instruments():
            prov = provider_for(inst)
            primary, context = prov.fetch_primary_and_context(
                inst.symbol, inst.timeframe_primary, inst.timeframe_context
            )
    """
    global _crypto_provider, _stocks_provider

    if instrument.kind == "crypto":
        if _crypto_provider is None:
            _crypto_provider = AlpacaProvider()
        return _crypto_provider

    if instrument.kind == "stock":
        if _stocks_provider is None:
            _stocks_provider = AlpacaStocksProvider()
        return _stocks_provider

    raise ValueError(f"Neznámý typ instrumentu: {instrument.kind!r}")


def providers_for_all() -> dict[str, MarketDataProvider]:
    """Vrátí slovník {symbol: provider} pro všechny aktivní instrumenty.

    Každý typ sdílí jednu instanci providera (singleton).
    """
    from bot.config import get_enabled_instruments
    return {inst.symbol: provider_for(inst) for inst in get_enabled_instruments()}


def default_provider() -> MarketDataProvider:
    """Zpětná kompatibilita — vrátí crypto provider pro primární instrument.

    Nový kód by měl používat provider_for(instrument) místo toho.
    """
    return AlpacaProvider()
