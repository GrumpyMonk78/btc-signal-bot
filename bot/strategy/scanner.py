"""
Cheap, deterministic technical filters for BTC/USD long-only on H1.

The scanner is a *trigger*, not a decision-maker. Its job is to answer:
"Is this moment worth spending Claude tokens on?" — high recall, low
precision is the desired profile. Claude + the risk manager make the
actual go/no-go call downstream.

Three filters, combined with OR, ALL gated by H4 uptrend (long-only):
  1. EMA pullback from a stretched position
  2. Range breakout with ATR expansion
  3. Volume spike with bullish absorption

API
---
    scan(primary_df, context_df) -> list[ScannerSignal]

Transition + cooldown
---------------------
- A filter fires on bar `t` only when its condition is True at `t` and
  was False at `t-1` (rising edge). Prevents repeated triggers while a
  filter stays on for many bars.
- A minimum 4-hour cooldown between any two signals is enforced by
  `scan` — even if a different filter would otherwise fire, it's
  suppressed if the previous signal was within the cooldown window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

import pandas as pd

from bot.strategy import indicators as ind


# ─────────────────────────────────────────────────────────────────────────────
# Tunable parameters (kept here for visibility; can be moved to config later)
# ─────────────────────────────────────────────────────────────────────────────

# Trend definition (H4)
H4_EMA_FAST = 20
H4_EMA_SLOW = 50

# H1 pullback parameters
H1_EMA_FAST = 20
H1_EMA_SLOW = 50
PULLBACK_TOUCH_TOL = 0.002       # bar's low must come within 0.2% of EMA20 (or briefly below)
PULLBACK_PRIOR_LOOKBACK = 5      # within last N bars there must have been a "stretched" close…
PULLBACK_STRETCH_ATR = 0.5       # …defined as close > EMA20 + this many ATRs above EMA20.
# Rationale: without this, the filter fires repeatedly while price oscillates
# around EMA20. We want a *true* pullback from a stretched position, not noise.

# Breakout parameters
BREAKOUT_LOOKBACK = 20           # 20 H1 bars ≈ 20h
ATR_PERIOD = 14
ATR_EXPANSION_MULT = 1.2         # current ATR must be > 1.2× ATR_MA_50
ATR_MA_PERIOD = 50
BREAKOUT_BODY_MIN = 0.6          # |close-open|/range >= 0.6

# Volume spike parameters
VOLUME_MA_PERIOD = 20
VOLUME_SPIKE_MULT = 2.0
CLOSE_POS_MIN = 0.66             # close in upper third of bar's range

# Cooldown between any two triggers
COOLDOWN = timedelta(hours=4)


FilterName = Literal["ema_pullback", "breakout_atr", "volume_absorption"]


# ─────────────────────────────────────────────────────────────────────────────
# Output type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScannerSignal:
    """A scanner trigger on a specific bar.

    Carries the bar timestamp, the filter that fired, and a dict of numeric
    context (current price, ATR, EMA values, etc.) so downstream code can
    log it and Claude can see what made the scanner wake up.
    """

    timestamp: pd.Timestamp
    filter: FilterName
    price: float
    context: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# H4 trend gate
# ─────────────────────────────────────────────────────────────────────────────


def h4_uptrend_series(context_df: pd.DataFrame) -> pd.Series:
    """Per-H4-bar boolean: 'we are in an H4 uptrend'.

    Definition: close > EMA50 AND EMA20 > EMA50.
    Returns a Series indexed by H4 timestamps.
    """
    if context_df.empty:
        return pd.Series(dtype=bool, name="h4_uptrend")

    ema_fast = ind.ema(context_df["close"], H4_EMA_FAST)
    ema_slow = ind.ema(context_df["close"], H4_EMA_SLOW)
    cond = (context_df["close"] > ema_slow) & (ema_fast > ema_slow)
    return cond.fillna(False).rename("h4_uptrend")


def align_h4_to_h1(h4_flag: pd.Series, h1_index: pd.DatetimeIndex) -> pd.Series:
    """For each H1 timestamp, return the most-recent *closed* H4 flag.

    `reindex(method='ffill')` gives us forward-fill, which uses the H4 bar
    that opened at-or-before each H1 bar. That's the right semantics — we
    use the H4 trend that was known when the H1 bar opened.
    """
    if len(h4_flag) == 0:
        return pd.Series(False, index=h1_index, name="h4_uptrend")
    return h4_flag.reindex(h1_index, method="ffill").fillna(False)


# ─────────────────────────────────────────────────────────────────────────────
# Filters — each returns a bool Series aligned to primary_df.index
# All filters are gated by H4 uptrend (long-only bot).
# ─────────────────────────────────────────────────────────────────────────────


def filter_ema_pullback(primary_df: pd.DataFrame, h4_up_on_h1: pd.Series) -> pd.Series:
    """EMA pullback from a stretched position, in H4 uptrend.

    Trigger when:
      - H4 trend is up (gate)
      - Within the last PULLBACK_PRIOR_LOOKBACK bars (excluding current),
        price was *stretched* above EMA20 by at least PULLBACK_STRETCH_ATR × ATR
      - H1 bar's low touched within tolerance of EMA20 (or briefly below)
      - H1 close is back above EMA20
      - EMA20 > EMA50 on H1 (i.e. H1 structure isn't broken)
    """
    close = primary_df["close"]
    low = primary_df["low"]
    ema_fast = ind.ema(close, H1_EMA_FAST)
    ema_slow = ind.ema(close, H1_EMA_SLOW)
    atr14 = ind.atr(primary_df, ATR_PERIOD)

    # Was price stretched above EMA20 by >= 0.5 ATR within the recent window?
    # We compare *prior* bars only (shift by 1) so the trigger bar itself
    # doesn't count as its own stretch.
    stretched = close > (ema_fast + PULLBACK_STRETCH_ATR * atr14)
    was_stretched_recently = (
        stretched.shift(1)
        .rolling(window=PULLBACK_PRIOR_LOOKBACK, min_periods=1)
        .max()
        .fillna(0)
        .astype(bool)
    )

    touched = low <= ema_fast * (1 + PULLBACK_TOUCH_TOL)
    held = close > ema_fast
    structure_ok = ema_fast > ema_slow

    cond = h4_up_on_h1 & was_stretched_recently & touched & held & structure_ok
    return cond.fillna(False).rename("ema_pullback")


def filter_breakout_atr(primary_df: pd.DataFrame, h4_up_on_h1: pd.Series) -> pd.Series:
    """Range breakout with ATR expansion and decisive body, gated by H4 uptrend.

    Trigger when:
      - H4 trend is up (gate)
      - Bar's close > rolling max of the prior `BREAKOUT_LOOKBACK` highs
      - Current ATR > ATR_EXPANSION_MULT × SMA of ATR over ATR_MA_PERIOD
      - Body / range >= BREAKOUT_BODY_MIN (no upper wick shenanigans)
      - Bullish bar (close > open)
    """
    close = primary_df["close"]
    open_ = primary_df["open"]

    # 'Prior' rolling max — shift by 1 so we compare to the high before
    # the current bar, not including it.
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
    """Volume spike with close in upper third of range (bullish absorption),
    gated by H4 uptrend.

    Trigger when:
      - H4 trend is up (gate) — volume spikes in downtrends are usually
        capitulation, not absorption
      - Volume > VOLUME_SPIKE_MULT × SMA(volume, VOLUME_MA_PERIOD)
      - Close position in bar's range >= CLOSE_POS_MIN
      - Bullish bar (close > open)
    """
    volume = primary_df["volume"]
    vol_baseline = ind.volume_ma(volume, VOLUME_MA_PERIOD)
    spike = volume > vol_baseline * VOLUME_SPIKE_MULT

    close_pos = ind.close_position_in_range(primary_df)
    in_upper_third = close_pos >= CLOSE_POS_MIN

    bullish = primary_df["close"] > primary_df["open"]

    cond = h4_up_on_h1 & spike & in_upper_third & bullish
    return cond.fillna(False).rename("volume_absorption")


# ─────────────────────────────────────────────────────────────────────────────
# Public scan() — combine filters, apply transition + cooldown
# ─────────────────────────────────────────────────────────────────────────────


def _rising_edge(s: pd.Series) -> pd.Series:
    """True where `s` transitions False→True from the previous bar."""
    return s & ~s.shift(1, fill_value=False)


def scan(primary_df: pd.DataFrame, context_df: pd.DataFrame) -> list[ScannerSignal]:
    """Run all filters and return a chronological list of signals.

    Combination rule: OR (any filter firing → signal), with transition-based
    detection (rising edge per filter) and a global 4h cooldown between
    consecutive signals.
    """
    if primary_df.empty:
        return []

    # ── H4 trend gate, aligned to H1 ──────────────────────────────────────
    h4_flag = h4_uptrend_series(context_df)
    h4_up_on_h1 = align_h4_to_h1(h4_flag, primary_df.index)

    # ── Filters (all gated by H4 uptrend for long-only) ──────────────────
    cond_pullback = filter_ema_pullback(primary_df, h4_up_on_h1)
    cond_breakout = filter_breakout_atr(primary_df, h4_up_on_h1)
    cond_volume = filter_volume_absorption(primary_df, h4_up_on_h1)

    # ── Rising edges ──────────────────────────────────────────────────────
    edge_pullback = _rising_edge(cond_pullback)
    edge_breakout = _rising_edge(cond_breakout)
    edge_volume = _rising_edge(cond_volume)

    # ── Precompute context indicators for logging ─────────────────────────
    ema20 = ind.ema(primary_df["close"], H1_EMA_FAST)
    ema50 = ind.ema(primary_df["close"], H1_EMA_SLOW)
    atr14 = ind.atr(primary_df, ATR_PERIOD)
    vol_ma20 = ind.volume_ma(primary_df["volume"], VOLUME_MA_PERIOD)
    rsi14 = ind.rsi(primary_df["close"], 14)
    macd_df = ind.macd(primary_df["close"])

    # ── Walk chronologically, apply cooldown ──────────────────────────────
    signals: list[ScannerSignal] = []
    last_ts: pd.Timestamp | None = None

    # When multiple filters fire on the same bar, prefer the strongest
    # signal type. Order encodes priority.
    priority: list[tuple[FilterName, pd.Series]] = [
        ("breakout_atr", edge_breakout),
        ("ema_pullback", edge_pullback),
        ("volume_absorption", edge_volume),
    ]

    for ts in primary_df.index:
        fired: FilterName | None = None
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
            "close": float(bar["close"]),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "volume": float(bar["volume"]),
            "ema20": float(ema20.get(ts, float("nan"))),
            "ema50": float(ema50.get(ts, float("nan"))),
            "atr14": float(atr14.get(ts, float("nan"))),
            "rsi14": float(rsi14.get(ts, float("nan"))),
            "macd": float(macd_df["macd"].get(ts, float("nan"))),
            "macd_signal": float(macd_df["signal"].get(ts, float("nan"))),
            "macd_hist": float(macd_df["histogram"].get(ts, float("nan"))),
            "vol_ma20": float(vol_ma20.get(ts, float("nan"))),
            "h4_uptrend": float(bool(h4_up_on_h1.get(ts, False))),
        }
        signals.append(
            ScannerSignal(
                timestamp=ts,
                filter=fired,
                price=float(bar["close"]),
                context=ctx,
            )
        )
        last_ts = ts

    return signals
