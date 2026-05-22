"""
Sanity tests for the scanner — synthetic OHLCV series engineered to fire
(or not fire) each filter. The point is to verify the *logic* — real
behaviour is validated by `scripts/scanner_replay.py` on Alpaca data.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from bot.strategy import scanner as sc


def _h1_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")


def _h4_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC")


def _flat_bars(n: int, price: float = 100.0, freq: str = "1h") -> pd.DataFrame:
    """A perfectly flat, low-volatility series — should fire nothing."""
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": [price] * n,
            "high": [price + 0.1] * n,
            "low": [price - 0.1] * n,
            "close": [price] * n,
            "volume": [1.0] * n,
            "vwap": [price] * n,
            "trade_count": [10] * n,
        },
        index=idx,
    )


def _uptrending_context(n: int = 100, start: float = 90.0, end: float = 130.0,
                        start_offset_hours: int = 240) -> pd.DataFrame:
    """H4 context that produces a clean uptrend gate (close > EMA50 and EMA20 > EMA50).

    Starts ``start_offset_hours`` before ``2026-01-01`` so that the H4 EMA50
    (which needs ~50 bars to seed) is already valid by the time the H1
    primary series begins."""
    start_ts = pd.Timestamp("2026-01-01", tz="UTC") - pd.Timedelta(hours=start_offset_hours)
    idx = pd.date_range(start_ts, periods=n, freq="4h", tz="UTC")
    closes = np.linspace(start, end, n)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": [1.0] * n,
            "vwap": closes,
            "trade_count": [10] * n,
        },
        index=idx,
    )


def _all_true_h4_flag(primary_df: pd.DataFrame) -> pd.Series:
    """Helper for unit tests that want to exercise filters without H4 noise:
    just return a Series of `True` aligned to the primary index."""
    return pd.Series(True, index=primary_df.index, name="h4_uptrend")


# ─────────────────────────────────────────────────────────────────────────────
# Empty input
# ─────────────────────────────────────────────────────────────────────────────


def test_scan_empty_returns_empty_list():
    empty = sc.scan(pd.DataFrame(), pd.DataFrame())
    assert empty == []


# ─────────────────────────────────────────────────────────────────────────────
# Quiet input — no filter should fire
# ─────────────────────────────────────────────────────────────────────────────


def test_flat_market_fires_nothing():
    primary = _flat_bars(300)
    context = _flat_bars(80, freq="4h")
    signals = sc.scan(primary, context)
    assert signals == []


# ─────────────────────────────────────────────────────────────────────────────
# H4 uptrend gate
# ─────────────────────────────────────────────────────────────────────────────


def test_h4_uptrend_detected_on_rising_series():
    # Slowly rising H4 series — eventually close > EMA50 and EMA20 > EMA50.
    n = 120
    idx = _h4_index(n)
    closes = np.linspace(100, 200, n)
    df = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": [1.0] * n,
            "vwap": closes,
            "trade_count": [10] * n,
        },
        index=idx,
    )
    flag = sc.h4_uptrend_series(df)
    # The last bar in a steady uptrend must be flagged as uptrend.
    assert bool(flag.iloc[-1]) is True
    # Early bars (before EMA50 is even seeded) must not be flagged.
    assert bool(flag.iloc[10]) is False


# ─────────────────────────────────────────────────────────────────────────────
# Breakout + ATR filter
# ─────────────────────────────────────────────────────────────────────────────


def test_breakout_filter_fires_on_engineered_breakout():
    # 80 quiet bars then a big bullish bar that breaks the prior 20-bar high
    # with expansion. Use freq='1h' so timestamps are realistic.
    n_quiet = 80
    base = _flat_bars(n_quiet, price=100.0)

    # Append a breakout bar
    breakout_ts = base.index[-1] + timedelta(hours=1)
    breakout_row = pd.DataFrame(
        {
            "open": [100.05],
            "high": [105.0],   # well above any prior high
            "low": [100.0],
            "close": [104.8],  # body 4.75 / range 5.0 = 0.95 ≥ 0.6
            "volume": [10.0],
            "vwap": [102.0],
            "trade_count": [100],
        },
        index=[breakout_ts],
    )
    primary = pd.concat([base, breakout_row])
    # Bypass the H4 gate for the unit test — gate behaviour is tested separately.
    cond = sc.filter_breakout_atr(primary, _all_true_h4_flag(primary))
    # Must fire on the engineered last bar.
    assert bool(cond.iloc[-1]) is True
    # Must not fire on any prior quiet bar.
    assert not cond.iloc[:-1].any()


def test_breakout_filter_does_not_fire_without_expansion():
    # Prior 20-bar high keeps creeping up so the new bar can't really break out.
    n = 100
    idx = _h1_index(n)
    closes = np.linspace(100, 110, n)
    primary = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.1,
            "low": closes - 0.1,
            "close": closes,
            "volume": [1.0] * n,
            "vwap": closes,
            "trade_count": [10] * n,
        },
        index=idx,
    )
    cond = sc.filter_breakout_atr(primary, _all_true_h4_flag(primary))
    assert not cond.any()


def test_breakout_filter_gated_by_h4_downtrend():
    """Even an engineered breakout must NOT fire if H4 says we're in a downtrend."""
    base = _flat_bars(80, price=100.0)
    breakout_ts = base.index[-1] + timedelta(hours=1)
    breakout_row = pd.DataFrame(
        {"open": [100.05], "high": [105.0], "low": [100.0], "close": [104.8],
         "volume": [10.0], "vwap": [102.0], "trade_count": [100]},
        index=[breakout_ts],
    )
    primary = pd.concat([base, breakout_row])
    # H4 gate = all False
    h4_off = pd.Series(False, index=primary.index, name="h4_uptrend")
    cond = sc.filter_breakout_atr(primary, h4_off)
    assert not cond.any()


# ─────────────────────────────────────────────────────────────────────────────
# Volume spike + absorption
# ─────────────────────────────────────────────────────────────────────────────


def test_volume_absorption_fires_on_spike_with_close_at_top():
    base = _flat_bars(40, price=100.0)
    spike_ts = base.index[-1] + timedelta(hours=1)
    # Volume 10× baseline, close near the top of range, bullish.
    spike_row = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.8],
            "close": [100.95],   # pos in range ≈ (100.95-99.8)/1.2 ≈ 0.96
            "volume": [10.0],
            "vwap": [100.4],
            "trade_count": [100],
        },
        index=[spike_ts],
    )
    primary = pd.concat([base, spike_row])
    cond = sc.filter_volume_absorption(primary, _all_true_h4_flag(primary))
    assert bool(cond.iloc[-1]) is True


def test_volume_absorption_gated_by_h4_downtrend():
    """Volume spike in H4 downtrend = capitulation, must NOT fire for long-only."""
    base = _flat_bars(40, price=100.0)
    spike_ts = base.index[-1] + timedelta(hours=1)
    spike_row = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.8], "close": [100.95],
         "volume": [10.0], "vwap": [100.4], "trade_count": [100]},
        index=[spike_ts],
    )
    primary = pd.concat([base, spike_row])
    h4_off = pd.Series(False, index=primary.index, name="h4_uptrend")
    cond = sc.filter_volume_absorption(primary, h4_off)
    assert not cond.any()


# ─────────────────────────────────────────────────────────────────────────────
# Transition + cooldown
# ─────────────────────────────────────────────────────────────────────────────


def test_rising_edge_helper():
    s = pd.Series([False, True, True, False, True, True, True])
    e = sc._rising_edge(s)
    assert list(e) == [False, True, False, False, True, False, False]


def test_cooldown_suppresses_followups_within_4h():
    # Build a primary series of 100 quiet bars and 3 engineered breakouts
    # spaced 2h apart. Only the first should produce a signal (cooldown=4h).
    # Use an H4 uptrend context so the gate lets the breakouts through.
    n_quiet = 80
    base = _flat_bars(n_quiet, price=100.0)

    next_ts = base.index[-1] + timedelta(hours=1)
    rows = []
    for offset, price in enumerate([105.0, 106.0, 107.0]):
        ts = next_ts + timedelta(hours=offset * 2)
        rows.append(
            pd.DataFrame(
                {
                    "open": [price - 4.5],
                    "high": [price + 0.2],
                    "low": [price - 4.6],
                    "close": [price],
                    "volume": [10.0],
                    "vwap": [price - 2],
                    "trade_count": [100],
                },
                index=[ts],
            )
        )
    primary = pd.concat([base, *rows])
    context = _uptrending_context(100, start=90.0, end=110.0)

    signals = sc.scan(primary, context)
    # Cooldown enforced: at most one signal in the 4h window we engineered.
    assert len(signals) == 1
    assert signals[0].filter == "breakout_atr"


def test_pullback_filter_requires_prior_extension():
    """Constant price = EMA20 sits exactly at price, ATR is ~0, but the
    `was_stretched_recently` condition can never be satisfied (close is
    never > EMA20 + 0.5*ATR by a positive margin). No signal."""
    n = 100
    idx = _h1_index(n)
    closes = np.full(n, 100.0)
    primary = pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.05,
            "low": closes - 0.05,
            "close": closes,
            "volume": [1.0] * n,
            "vwap": closes,
            "trade_count": [10] * n,
        },
        index=idx,
    )
    h4_on = _all_true_h4_flag(primary)
    cond = sc.filter_ema_pullback(primary, h4_on)
    assert not cond.any()
