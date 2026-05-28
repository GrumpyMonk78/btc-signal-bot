"""
alpaca_client.py — Alpaca API wrapper for Liquidity Sweep Bot

Podporuje:
- Crypto (BTC/USD) přes Alpaca Crypto Trading API
- US Stocks přes Alpaca Stock Trading API
- Bracket orders (entry + SL + TP v jednom)
- Portfolio / position query
- Paper trading mode (výchozí)
"""

import logging
from typing import Optional
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import pandas as pd
from datetime import datetime, timezone, timedelta

from bot.config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    LOOKBACK_BARS,
    MAX_OPEN_POSITIONS,
)

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: float
    status: str
    message: str


def _make_trading_client() -> TradingClient:
    return TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        paper=True,  # always paper for safety — change to paper=False for live
    )


def _parse_timeframe(tf_str: str) -> TimeFrame:
    """Converts config string like '15Min', '1Hour' to Alpaca TimeFrame."""
    mapping = {
        "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
        "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "30Min": TimeFrame(30, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
        "4Hour": TimeFrame(4,  TimeFrameUnit.Hour),
        "1Day":  TimeFrame(1,  TimeFrameUnit.Day),
    }
    tf = mapping.get(tf_str)
    if tf is None:
        raise ValueError(f"Unknown timeframe: {tf_str}. Use one of {list(mapping.keys())}")
    return tf


def fetch_bars(symbol: str, asset_class: str, timeframe_str: str,
               n_bars: int = LOOKBACK_BARS) -> pd.DataFrame:
    """
    Fetch OHLCV bars from Alpaca.

    Returns DataFrame with columns: open, high, low, close, volume
    Index: DatetimeIndex (UTC)
    """
    tf = _parse_timeframe(timeframe_str)
    end   = datetime.now(timezone.utc)
    # fetch extra bars to ensure we have enough after market-hours gaps
    start = end - timedelta(days=max(n_bars // 6, 30))

    if asset_class == "crypto":
        client = CryptoHistoricalDataClient()
        req = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=n_bars * 2,
        )
        bars = client.get_crypto_bars(req)
    else:
        client = StockHistoricalDataClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
        )
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=tf,
            start=start,
            end=end,
            limit=n_bars * 2,
        )
        bars = client.get_stock_bars(req)

    df = bars.df
    if df.empty:
        logger.warning(f"No bars returned for {symbol} ({timeframe_str})")
        return pd.DataFrame()

    # Flatten multi-index if present (symbol + timestamp)
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)

    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index().tail(n_bars)
    return df


def get_open_position_count() -> int:
    """Returns number of currently open positions."""
    try:
        client = _make_trading_client()
        positions = client.get_all_positions()
        return len(positions)
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return 0


def has_open_position(symbol: str) -> bool:
    """Check if we already have an open position in this symbol."""
    try:
        client = _make_trading_client()
        positions = client.get_all_positions()
        symbols = [p.symbol for p in positions]
        # Alpaca stores crypto as e.g. "BTCUSD" without slash
        clean_symbol = symbol.replace("/", "")
        return clean_symbol in symbols or symbol in symbols
    except Exception as e:
        logger.error(f"Failed to check position for {symbol}: {e}")
        return False


def place_bracket_order(
    symbol: str,
    asset_class: str,
    direction: str,
    qty: float,
    take_profit: float,
    stop_loss: float,
) -> Optional[OrderResult]:
    """
    Place a bracket market order (entry + SL + TP).

    direction: "long" | "short"
    """
    # ── Safety checks ──────────────────────────────────────────────────────────
    if get_open_position_count() >= MAX_OPEN_POSITIONS:
        logger.warning(f"Max positions ({MAX_OPEN_POSITIONS}) reached, skipping {symbol}")
        return None

    if has_open_position(symbol):
        logger.info(f"Already have position in {symbol}, skipping")
        return None

    side = OrderSide.BUY if direction == "long" else OrderSide.SELL

    # Alpaca crypto uses fractional qty; stocks must be integer for non-fractional
    if asset_class == "stock":
        qty = max(1, int(qty))

    order_req = MarketOrderRequest(
        symbol=symbol.replace("/", "") if asset_class == "crypto" else symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.GTC,
        order_class="bracket",
        take_profit=TakeProfitRequest(limit_price=round(take_profit, 4)),
        stop_loss=StopLossRequest(stop_price=round(stop_loss, 4)),
    )

    try:
        client = _make_trading_client()
        order  = client.submit_order(order_req)
        logger.info(
            f"✅ Order placed: {symbol} {direction.upper()} qty={qty} "
            f"TP={take_profit:.4f} SL={stop_loss:.4f} → order_id={order.id}"
        )
        return OrderResult(
            order_id=str(order.id),
            symbol=symbol,
            side=direction,
            qty=qty,
            status=str(order.status),
            message="OK",
        )
    except Exception as e:
        logger.error(f"❌ Order failed for {symbol}: {e}")
        return OrderResult(
            order_id="",
            symbol=symbol,
            side=direction,
            qty=qty,
            status="error",
            message=str(e),
        )


def get_account_info() -> dict:
    """Returns basic account info (equity, cash, buying power)."""
    try:
        client  = _make_trading_client()
        account = client.get_account()
        return {
            "equity":        float(account.equity),
            "cash":          float(account.cash),
            "buying_power":  float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
        }
    except Exception as e:
        logger.error(f"Failed to get account info: {e}")
        return {}
