"""
Technical filters for H1 -- both long AND short directions.

The scanner is a *trigger*, not a decision-maker. Its job is to answer:
"Is this moment worth spending Claude tokens on?" -- high recall, low
precision is the desired profile. Claude + the risk manager make the
actual go/no-go call downstream.

Six filters (3 long + 3 short), gated by H4 trend direction:
  Long gate:  H4 close > EMA200  -> fires long filters only
  Short gate: H4 close < EMA200  -> fires short filters only

Long filters:
  1. ema_pullback         -- pullback to EMA20 from above
  2. breakout_atr         -- breakout above 20h high with ATR expansion
  3. volume_absorption    -- volume spike, close in upper third of bar

Short filters (mirror image):
  4. ema_pullback_short      -- bounce to EMA20 from below
  5. breakout_atr_short      -- breakdown below 20h low with ATR expansion
  6. volume_absorption_short -- volume spike, close in lower third of bar

API
---
    scan(primary_df, context_df) -> list[ScannerSignal]

Transition + cooldown
---------------------
- A filter fires on bar `t` only when its condition is True at `t` and
  was False at `t-1` (rising edge).
- A minimum 4-hour cooldown between any two signals is enforced by scan().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

import pandas as pd

from bot.strategy import indicators as ind


# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

H4_EMA_FAST = 20
H4_EMA_SLOW = 50
H4_EMA_LONG = 200

H1_EMA_FAST = 20
H1_EMA_SLOW = 50
PULLBACK_TOUCH_TOL = 0.002
PULLBACK_PRIOR_LOOKBACK = 5
PULLBACK_STRETCH_ATR = 0.5

BREAKOUT_LOOKBACK = 20
ATR_PERIOD = 14
ATR_EXPANSION_MULT = 1.2
ATR_MA_PERIOD = 50
BREAKOUT_BODY_MIN = 0.6

VOLUME_MA_PERIOD = 20
VOLUME_SPIKE_MULT = 2.0
CLOSE_POS_MIN = 0.66

COOLDOWN = timedelta(hours=4)

FilterName = Literal[
    "ema_pullback", "breakout_atr", "volume_absorption",
    "ema_pullback_short", "breakout_atr_short", "volume_absorption_short",
]


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScannerSignal:
    timestamp: pd.Timestamp
    filter: FilterName
    price: float
    context: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# H4 trend gates
# ---------------------------------------------------------------------------

def h4_uptrend_series(context_df: pd.DataFrame) -> pd.Series:
    """H4 close > EMA200 -> long gate."""
    if context_df.empty:
        return pd.Series(dtype=bool, name="h4_uptrend")
    ema_long = ind.ema(context_df["close"], H4_EMA_LONG)
    return (context_df["close"] > ema_long).fillna(False).rename("h4_uptrend")


def h4_downtrend_series(context_df: pd.DataFrame) -> pd.Series:
    """H4 close < EMA200 -> short gate."""
    if context_df.empty:
        return pd.Series(dtype=bool, name="h4_downtrend")
    ema_long = ind.ema(context_df["close"], H4_EMA_LONG)
    return (context_df["close"] < ema_long).fillna(False).rename("h4_downtrend")


def align_h4_to_h1(h4_flag: pd.Series, h1_index: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill H4 flag to H1 index."""
    if len(h4_flag) == 0:
        return pd.Series(False, index=h1_index, name=h4_flag.name)
    return h4_flag.reindex(h1_index, method="ffill").fillna(False)


# ---------------------------------------------------------------------------
# Long filters
# ---------------------------------------------------------------------------

def filter_ema_pullback(primary_df: pd.DataFrame, h4_up_on_h1: pd.Series) -> pd.Series:
    """EMA pullback from a stretched position, in H4 uptrend."""
    close = primary_df["close"]
    low = primary_df["low"]
    ema_fast = ind.ema(close, H1_EMA_FAST)
    ema_slow = ind.ema(close, H1_EMA_SLOW)
    atr14 = ind.atr(primary_df, ATR_PERIOD)

    stretched = close > (ema_fast + PULLBACK_STRETCH_ATR * atr14)
    was_stretched_recently = (
        stretched.shift(1)
        .rolling(window=PULLBACK_PRIOR_LOOKBACK, min_periods=1)
        .max().fillna(0).astype(bool)
    )
    touched = low <= ema_fast * (1 + PULLBACK_TOUCH_TOL)
    held = close > ema_fast
    structure_ok = ema_fast > ema_slow

    cond = h4_up_on_h1 & was_stretched_recently & touched & held & structure_ok
    return cond.fillna(False).rename("ema_pullback")


def filter_breakout_atr(primary_df: pd.DataFrame, h4_up_on_h1: pd.Series) -> pd.Series:
    """Range breakout above 20h high with ATR expansion, in H4 uptrend."""
    close = primary_df["close"]
    open_ = primary_df["open"]

    prior_high = ind.rolling_max(primary_df["high"], BREAKOUT_LOOKBACK).shift(1)
    broke = close > prior_high

    atr_now = ind.atr(primary_df, ATR_PERIOD)
    atr_baseline = ind.sma(atr_now, ATR_MA_PERIOD)
    vol_expansion = atr_now > atr_baseline * ATR_EXPANSION_MULT

    body_ok = ind.body_pct(primary_df) >= BREAKOUT_BODY_MIN
    bullish = close > open_

    cond = h4_up_on_h1 & broke & vol_expansion & body_ok & bullish
    return cond.fillna(False).rename("breakout_atr")


def filter_volume_absorption(primary_df: pd.DataFrame, h4_up_on_h1: pd.Series) -> pd.Series:
    """Volume spike with close in upper third of bar, in H4 uptrend."""
    volume = primary_df["volume"]
    vol_baseline = ind.volume_ma(volume, VOLUME_MA_PERIOD)
    spike = volume > vol_baseline * VOLUME_SPIKE_MULT

    close_pos = ind.close_position_in_range(primary_df)
    in_upper_third = close_pos >= CLOSE_POS_MIN
    bullish = primary_df["close"] > primary_df["open"]

    cond = h4_up_on_h1 & spike & in_upper_third & bullish
    return cond.fillna(False).rename("volume_absorption")


# ---------------------------------------------------------------------------
# Short filters (mirror image of long)
# ---------------------------------------------------------------------------

def filter_ema_pullback_short(primary_df: pd.DataFrame, h4_down_on_h1: pd.Series) -> pd.Series:
    """Bounce to EMA20 from below (short re-entry), in H4 downtrend."""
    close = primary_df["close"]
    high = primary_df["high"]
    ema_fast = ind.ema(close, H1_EMA_FAST)
    ema_slow = ind.ema(close, H1_EMA_SLOW)
    atr14 = ind.atr(primary_df, ATR_PERIOD)

    stretched_down = close < (ema_fast - PULLBACK_STRETCH_ATR * atr14)
    was_stretched_recently = (
        stretched_down.shift(1)
        .rolling(window=PULLBACK_PRIOR_LOOKBACK, min_periods=1)
        .max().fillna(0).astype(bool)
    )
    touched = high >= ema_fast * (1 - PULLBACK_TOUCH_TOL)
    held = close < ema_fast
    structure_ok = ema_fast < ema_slow

    cond = h4_down_on_h1 & was_stretched_recently & touched & held & structure_ok
    return cond.fillna(False).rename("ema_pullback_short")


def filter_breakout_atr_short(primary_df: pd.DataFrame, h4_down_on_h1: pd.Series) -> pd.Series:
    """Breakdown below 20h low with ATR expansion, in H4 downtrend."""
    close = primary_df["close"]
    open_ = primary_df["open"]

    prior_low = ind.rolling_min(primary_df["low"], BREAKOUT_LOOKBACK).shift(1)
    broke_down = close < prior_low

    atr_now = ind.atr(primary_df, ATR_PERIOD)
    atr_baseline = ind.sma(atr_now, ATR_MA_PERIOD)
    vol_expansion = atr_now > atr_baseline * ATR_EXPANSION_MULT

    body_ok = ind.body_pct(primary_df) >= BREAKOUT_BODY_MIN
    bearish = close < open_

    cond = h4_down_on_h1 & broke_down & vol_expansion & body_ok & bearish
    return cond.fillna(False).rename("breakout_atr_short")


def filter_volume_absorption_short(primary_df: pd.DataFrame, h4_down_on_h1: pd.Series) -> pd.Series:
    """Volume spike with close in lower third of bar, in H4 downtrend."""
    volume = primary_df["volume"]
    vol_baseline = ind.volume_ma(volume, VOLUME_MA_PERIOD)
    spike = volume > vol_baseline * VOLUME_SPIKE_MULT

    close_pos = ind.close_position_in_range(primary_df)
    in_lower_third = close_pos <= (1.0 - CLOSE_POS_MIN)
    bearish = primary_df["close"] < primary_df["open"]

    cond = h4_down_on_h1 & spike & in_lower_third & bearish
    return cond.fillna(False).rename("volume_absorption_short")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rising_edge(s: pd.Series) -> pd.Series:
    """True where s transitions False->True."""
    return s & ~s.shift(1, fill_value=False)


# ---------------------------------------------------------------------------
# Public scan()
# ---------------------------------------------------------------------------

def scan(primary_df: pd.DataFrame, context_df: pd.DataFrame) -> list[ScannerSignal]:
    """Run all filters (long + short) and return chronological signals.

    Long gate:  H4 close > EMA200 -> only long filters fire
    Short gate: H4 close < EMA200 -> only short filters fire
    """
    if primary_df.empty:
        return []

    h4_up_flag   = h4_uptrend_series(context_df)
    h4_down_flag = h4_downtrend_series(context_df)
    h4_up_on_h1   = align_h4_to_h1(h4_up_flag,   primary_df.index)
    h4_down_on_h1 = align_h4_to_h1(h4_down_flag, primary_df.index)

    # Long filters
    cond_pullback = filter_ema_pullback(primary_df, h4_up_on_h1)
    cond_breakout = filter_breakout_atr(primary_df, h4_up_on_h1)
    cond_volume   = filter_volume_absorption(primary_df, h4_up_on_h1)

    # Short filters
    cond_pullback_s = filter_ema_pullback_short(primary_df, h4_down_on_h1)
    cond_breakout_s = filter_breakout_atr_short(primary_df, h4_down_on_h1)
    cond_volume_s   = filter_volume_absorption_short(primary_df, h4_down_on_h1)

    # Rising edges
    edge_pullback   = _rising_edge(cond_pullback)
    edge_breakout   = _rising_edge(cond_breakout)
    edge_volume     = _rising_edge(cond_volume)
    edge_pullback_s = _rising_edge(cond_pullback_s)
    edge_breakout_s = _rising_edge(cond_breakout_s)
    edge_volume_s   = _rising_edge(cond_volume_s)

    # Precompute H1 indicators
    ema20    = ind.ema(primary_df["close"], H1_EMA_FAST)
    ema50    = ind.ema(primary_df["close"], H1_EMA_SLOW)
    ema200   = ind.ema(primary_df["close"], 200)
    atr14    = ind.atr(primary_df, ATR_PERIOD)
    vol_ma20 = ind.volume_ma(primary_df["volume"], VOLUME_MA_PERIOD)
    rsi14    = ind.rsi(primary_df["close"], 14)
    macd_df  = ind.macd(primary_df["close"])
    bb_df    = ind.bollinger_bands(primary_df["close"])
    vwap_s   = ind.vwap(primary_df)
    stoch_df = ind.stoch_rsi(primary_df["close"])
    obv_s    = ind.obv(primary_df)
    rsi_div  = ind.rsi_divergence(primary_df)

    # Precompute H4 indicators (smoother — better for trend strength & SL/TP)
    # context_df is H4; we forward-fill to H1 index for aligned lookup
    h4_ema20_raw  = ind.ema(context_df["close"], H4_EMA_FAST) if not context_df.empty else None
    h4_ema50_raw  = ind.ema(context_df["close"], H4_EMA_SLOW) if not context_df.empty else None
    h4_rsi_raw    = ind.rsi(context_df["close"], ATR_PERIOD) if not context_df.empty else None
    h4_atr_raw    = ind.atr(context_df, ATR_PERIOD) if not context_df.empty else None
    h4_macd_raw   = ind.macd(context_df["close"]) if not context_df.empty else None

    def _h4_val(series_or_df, key=None, ts=None):
        """Get H4 indicator value at or before H1 timestamp ts via ffill."""
        if series_or_df is None:
            return float("nan")
        s = series_or_df[key] if key else series_or_df
        # reindex to get the last H4 value <= ts
        try:
            subset = s[s.index <= ts]
            return float(subset.iloc[-1]) if len(subset) else float("nan")
        except Exception:
            return float("nan")

    signals: list[ScannerSignal] = []
    last_ts = None

    # Priority: breakout > pullback > volume (same for both directions)
    priority = [
        ("breakout_atr",            edge_breakout),
        ("ema_pullback",            edge_pullback),
        ("volume_absorption",       edge_volume),
        ("breakout_atr_short",      edge_breakout_s),
        ("ema_pullback_short",      edge_pullback_s),
        ("volume_absorption_short", edge_volume_s),
    ]

    for ts in primary_df.index:
        fired = None
        for name, edge in priority:
            if bool(edge.get(ts, False)):
                fired = name
                break
        if fired is None:
            continue
        if last_ts is not None and (ts - last_ts) < COOLDOWN:
            continue

        bar = primary_df.loc[ts]
        ctx = {
            "close":          float(bar["close"]),
            "open":           float(bar["open"]),
            "high":           float(bar["high"]),
            "low":            float(bar["low"]),
            "volume":         float(bar["volume"]),
            "ema20":          float(ema20.get(ts, float("nan"))),
            "ema50":          float(ema50.get(ts, float("nan"))),
            "ema200":         float(ema200.get(ts, float("nan"))),
            "atr14":          float(atr14.get(ts, float("nan"))),
            "bb_upper":       float(bb_df["bb_upper"].get(ts, float("nan"))),
            "bb_lower":       float(bb_df["bb_lower"].get(ts, float("nan"))),
            "bb_pct_b":       float(bb_df["bb_pct_b"].get(ts, float("nan"))),
            "bb_width":       float(bb_df["bb_width"].get(ts, float("nan"))),
            "vwap":           float(vwap_s.get(ts, float("nan"))),
            "rsi14":          float(rsi14.get(ts, float("nan"))),
            "stoch_k":        float(stoch_df["stoch_k"].get(ts, float("nan"))),
            "stoch_d":        float(stoch_df["stoch_d"].get(ts, float("nan"))),
            "macd":           float(macd_df["macd"].get(ts, float("nan"))),
            "macd_signal":    float(macd_df["signal"].get(ts, float("nan"))),
            "macd_hist":      float(macd_df["histogram"].get(ts, float("nan"))),
            "vol_ma20":       float(vol_ma20.get(ts, float("nan"))),
            "obv":            float(obv_s.get(ts, float("nan"))),
            "rsi_divergence": int(rsi_div.get(ts, 0)),
            "h4_uptrend":     float(bool(h4_up_on_h1.get(ts, False))),
            "h4_downtrend":   float(bool(h4_down_on_h1.get(ts, False))),
            # H4 indicators — smoother, less noise than H1
            "h4_ema20":       _h4_val(h4_ema20_raw, ts=ts),
            "h4_ema50":       _h4_val(h4_ema50_raw, ts=ts),
            "h4_rsi14":       _h4_val(h4_rsi_raw, ts=ts),
            "h4_atr14":       _h4_val(h4_atr_raw, ts=ts),
            "h4_macd":        _h4_val(h4_macd_raw, "macd", ts=ts),
            "h4_macd_signal": _h4_val(h4_macd_raw, "signal", ts=ts),
            "h4_macd_hist":   _h4_val(h4_macd_raw, "histogram", ts=ts),
        }
        signals.append(ScannerSignal(
            timestamp=ts,
            filter=fired,
            price=float(bar["close"]),
            context=ctx,
        ))
        last_ts = ts

    return signals
