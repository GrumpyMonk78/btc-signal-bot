"""
Hand-rolled technical indicators.

We deliberately do not depend on `pandas-ta` here:
  - tighter control over edge cases (initialization, NaN handling)
  - no surprise breakage from upstream NumPy / pandas version bumps
  - trivially unit-testable on synthetic series

All functions take a pd.Series (or pd.DataFrame for OHLC inputs) and
return a pd.Series aligned to the input index. Leading values are NaN
until the indicator has enough history.

Conventions:
- EMA seed = SMA of the first `period` values (standard Wilder/TA-Lib convention).
- ATR uses Wilder smoothing (RMA), not simple rolling mean.
- RSI uses Wilder smoothing (RMA), same as TradingView default.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, seeded with SMA of the first `period` values."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(series) < period:
        return pd.Series(np.nan, index=series.index, name=f"ema_{period}")
    out = pd.Series(np.nan, index=series.index, dtype="float64")
    seed = series.iloc[:period].mean()
    out.iloc[period - 1] = seed
    alpha = 2 / (period + 1)
    for i in range(period, len(series)):
        prev = out.iloc[i - 1]
        out.iloc[i] = alpha * series.iloc[i] + (1 - alpha) * prev
    out.name = f"ema_{period}"
    return out


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    if period < 1:
        raise ValueError("period must be >= 1")
    return series.rolling(window=period, min_periods=period).mean().rename(f"sma_{period}")


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA / SMMA). Used by ATR and RSI."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if len(series) < period:
        return pd.Series(np.nan, index=series.index, name=f"rma_{period}")
    out = pd.Series(np.nan, index=series.index, dtype="float64")
    seed = series.iloc[:period].mean()
    out.iloc[period - 1] = seed
    for i in range(period, len(series)):
        prev = out.iloc[i - 1]
        out.iloc[i] = (prev * (period - 1) + series.iloc[i]) / period
    out.name = f"rma_{period}"
    return out


# ---------------------------------------------------------------------------
# Volatility / range
# ---------------------------------------------------------------------------

def true_range(ohlc: pd.DataFrame) -> pd.Series:
    """True Range = max(H-L, |H-prev_close|, |L-prev_close|)."""
    high = ohlc["high"]
    low = ohlc["low"]
    prev_close = ohlc["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rename("true_range")


def atr(ohlc: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder smoothing."""
    return rma(true_range(ohlc), period).rename(f"atr_{period}")


def rolling_max(series: pd.Series, window: int) -> pd.Series:
    """Rolling max over `window` bars (inclusive of current bar)."""
    return series.rolling(window=window, min_periods=window).max()


def rolling_min(series: pd.Series, window: int) -> pd.Series:
    """Rolling min over `window` bars (inclusive of current bar)."""
    return series.rolling(window=window, min_periods=window).min()


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder smoothing (same as TradingView).

    Values 0-100:
      > 70  overbought - caution on long entries
      < 30  oversold   - potential reversal upward
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    # Where avg_loss == 0 (all gains) -> RSI = 100
    rs = avg_gain / avg_loss.where(avg_loss != 0)
    result = 100 - (100 / (1 + rs))
    result = result.where(avg_loss != 0, other=100.0)
    return result.rename(f"rsi_{period}")


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD — Moving Average Convergence Divergence.

    Returns DataFrame se sloupci:
      macd      = EMA(fast) - EMA(slow)
      signal    = EMA(macd, signal_period)
      histogram = macd - signal  (krizeni nuly = momentum zmena)

    Standardni nastaveni 12/26/9 (stejne jako TradingView).
    Histogram > 0 = bullish momentum, < 0 = bearish.
    Krizeni signal line zdola nahoru = potencialni long signal.
    """
    if fast >= slow:
        raise ValueError("fast must be < slow")
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    macd_line.name = "macd"
    signal_line = ema(macd_line.dropna(), signal).reindex(series.index)
    signal_line.name = "signal"
    histogram = macd_line - signal_line
    histogram.name = "histogram"
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": histogram})


# ---------------------------------------------------------------------------
# Candle anatomy
# ---------------------------------------------------------------------------

def body_pct(ohlc: pd.DataFrame) -> pd.Series:
    """|close - open| / (high - low), 0..1. NaN if high == low (doji)."""
    rng = ohlc["high"] - ohlc["low"]
    body = (ohlc["close"] - ohlc["open"]).abs()
    return (body / rng.where(rng > 0)).rename("body_pct")


def close_position_in_range(ohlc: pd.DataFrame) -> pd.Series:
    """Where the close sits in the bar range: 0 = at low, 1 = at high."""
    rng = ohlc["high"] - ohlc["low"]
    pos = (ohlc["close"] - ohlc["low"]) / rng.where(rng > 0)
    return pos.rename("close_pos")


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def volume_ma(series: pd.Series, period: int = 20) -> pd.Series:
    """Simple rolling mean of volume."""
    return sma(series, period).rename(f"volume_ma_{period}")
