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


def obv(ohlc: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — kumulativní volume potvrzující cenový trend.

    OBV roste když close > prev_close (bulls control), klesá jinak.
    Divergence mezi OBV a cenou = varování před reversal.
    """
    direction = np.sign(ohlc["close"].diff()).fillna(0)
    return (direction * ohlc["volume"]).cumsum().rename("obv")


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands (SMA ± N×std).

    Returns DataFrame se sloupci:
      bb_mid   = SMA(period)
      bb_upper = bb_mid + std_dev * rolling_std
      bb_lower = bb_mid - std_dev * rolling_std
      bb_pct_b = (close - bb_lower) / (bb_upper - bb_lower)  — 0=at lower, 1=at upper
      bb_width = (bb_upper - bb_lower) / bb_mid               — normalizovaná šíře pásma

    bb_pct_b < 0.2 → cena u dolní hranice (potenciální reversal long)
    bb_width nízké  → squeeze, čeká se na breakout
    """
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    band_width = upper - lower
    pct_b = (series - lower) / band_width.where(band_width > 0)
    width = band_width / mid.where(mid > 0)
    return pd.DataFrame({
        "bb_mid": mid,
        "bb_upper": upper,
        "bb_lower": lower,
        "bb_pct_b": pct_b,
        "bb_width": width,
    })


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

def vwap(ohlc: pd.DataFrame) -> pd.Series:
    """Volume-Weighted Average Price — resetuje se každý obchodní den.

    VWAP = cumsum(typical_price * volume) / cumsum(volume) per day.
    Typická cena = (H + L + C) / 3.

    Klíčový intraday level pro US stocks — cena nad VWAP = bullish intraday,
    pod VWAP = bearish intraday. Institucionální hráči nakupují blízko VWAP.
    """
    typical = (ohlc["high"] + ohlc["low"] + ohlc["close"]) / 3
    tp_vol = typical * ohlc["volume"]

    # Group by date for daily reset
    dates = ohlc.index.normalize() if hasattr(ohlc.index, "normalize") else ohlc.index.date
    result = pd.Series(np.nan, index=ohlc.index, dtype="float64")

    for date, group in ohlc.groupby(dates):
        idx = group.index
        cum_tp_vol = tp_vol.loc[idx].cumsum()
        cum_vol = ohlc["volume"].loc[idx].cumsum()
        result.loc[idx] = cum_tp_vol / cum_vol.where(cum_vol > 0)

    return result.rename("vwap")


# ---------------------------------------------------------------------------
# Stochastic RSI
# ---------------------------------------------------------------------------

def stoch_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14,
              k_smooth: int = 3, d_smooth: int = 3) -> pd.DataFrame:
    """Stochastic RSI — aplikuje stochastiku na RSI místo na cenu.

    Citlivější než RSI samotný. Výborný pro timing vstupu.
    %K < 20 → přeprodáno, %K > 80 → překoupeno.
    %K kříží %D zdola nahoru = potenciální buy signal.

    Returns DataFrame se sloupci: stoch_k, stoch_d
    """
    rsi_values = rsi(series, rsi_period)
    rsi_min = rsi_values.rolling(window=stoch_period, min_periods=stoch_period).min()
    rsi_max = rsi_values.rolling(window=stoch_period, min_periods=stoch_period).max()
    rsi_range = rsi_max - rsi_min
    raw_k = (rsi_values - rsi_min) / rsi_range.where(rsi_range > 0) * 100
    k = raw_k.rolling(window=k_smooth, min_periods=1).mean().rename("stoch_k")
    d = k.rolling(window=d_smooth, min_periods=1).mean().rename("stoch_d")
    return pd.DataFrame({"stoch_k": k, "stoch_d": d})


# ---------------------------------------------------------------------------
# RSI Divergence
# ---------------------------------------------------------------------------

def rsi_divergence(ohlc: pd.DataFrame, rsi_period: int = 14, lookback: int = 20) -> pd.Series:
    """Detekuje bullish RSI divergenci: cena dělá lower low, RSI higher low.

    Bullish divergence = potenciální reversal nahoru.
    Returns Series: +1 = bullish divergence, 0 = žádná, -1 = bearish divergence.

    Algoritmus:
      - Hledá local minima ceny a RSI v posledních `lookback` barech
      - Bullish: current_price_low < prior_low AND current_rsi_low > prior_rsi_low
      - Bearish: current_price_high > prior_high AND current_rsi_high < prior_rsi_high
    """
    rsi_vals = rsi(ohlc["close"], rsi_period)
    result = pd.Series(0, index=ohlc.index, dtype="int8")

    for i in range(lookback, len(ohlc)):
        window_price_low = ohlc["low"].iloc[i - lookback: i]
        window_rsi = rsi_vals.iloc[i - lookback: i]

        if window_price_low.empty or window_rsi.isna().all():
            continue

        cur_price_low = ohlc["low"].iloc[i]
        cur_rsi = rsi_vals.iloc[i]
        if pd.isna(cur_rsi):
            continue

        prior_price_low = window_price_low.min()
        prior_rsi_at_low = window_rsi.iloc[window_price_low.argmin()]

        if cur_price_low < prior_price_low and cur_rsi > prior_rsi_at_low:
            result.iloc[i] = 1  # bullish divergence

        # Bearish divergence
        window_price_high = ohlc["high"].iloc[i - lookback: i]
        cur_price_high = ohlc["high"].iloc[i]
        prior_price_high = window_price_high.max()
        prior_rsi_at_high = window_rsi.iloc[window_price_high.argmax()]

        if cur_price_high > prior_price_high and cur_rsi < prior_rsi_at_high:
            result.iloc[i] = -1  # bearish divergence

    return result.rename("rsi_divergence")
