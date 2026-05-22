"""
Hand-rolled technical indicators.

We deliberately do not depend on `pandas-ta` here:
  - tighter control over edge cases (initialization, NaN handling)
  - no surprise breakage from upstream NumPy / pandas version bumps
  - trivially unit-testable on synthetic series

All functions take a `pd.Series` (or `pd.DataFrame` for OHLC inputs) and
return a `pd.Series` aligned to the input index. Leading values are NaN
until the indicator has enough history.

Conventions
-----------
- EMA seed = SMA of the first `period` values (standard Wilder/TA-Lib
  convention, matches most charting platforms).
- ATR uses Wilder smoothing (RMA), not simple rolling mean.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Moving averages
# ─────────────────────────────────────────────────────────────────────────────


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
    # Vectorised loop is fine — series lengths are O(1000), not O(1e6).
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
    """Wilder's smoothing (also called RMA / SMMA). Used by ATR."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Volatility / range
# ─────────────────────────────────────────────────────────────────────────────


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


# ─────────────────────────────────────────────────────────────────────────────
# Candle anatomy
# ─────────────────────────────────────────────────────────────────────────────


def body_pct(ohlc: pd.DataFrame) -> pd.Series:
    """|close - open| / (high - low), 0..1. NaN if high == low (doji at flat)."""
    rng = ohlc["high"] - ohlc["low"]
    body = (ohlc["close"] - ohlc["open"]).abs()
    return (body / rng.where(rng > 0)).rename("body_pct")


def close_position_in_range(ohlc: pd.DataFrame) -> pd.Series:
    """Where the close sits in the bar's range: 0 = at low, 1 = at high.

    Useful for 'bullish absorption' detection — close in the upper third
    of the range after a volume spike implies buyers absorbed the supply.
    """
    rng = ohlc["high"] - ohlc["low"]
    pos = (ohlc["close"] - ohlc["low"]) / rng.where(rng > 0)
    return pos.rename("close_pos")


# ─────────────────────────────────────────────────────────────────────────────
# Volume
# ─────────────────────────────────────────────────────────────────────────────


def volume_ma(series: pd.Series, period: int = 20) -> pd.Series:
    """Simple rolling mean of volume."""
    return sma(series, period).rename(f"volume_ma_{period}")
