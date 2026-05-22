"""
Sanity tests for indicators on synthetic series.

These tests verify mathematical correctness — not market behaviour. They
guarantee the indicators do what their docstrings say, so when we later
look at a real-data replay and something looks weird, we can rule out
indicator bugs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.strategy import indicators as ind


def _idx(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")


def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], index=_idx(5), dtype="float64")
    out = ind.sma(s, 3)
    # First two are NaN, then rolling mean of 3
    assert pd.isna(out.iloc[0])
    assert pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(4.0)


def test_ema_seeded_with_sma():
    # EMA(period=3): first two NaN, third = mean(s[:3]) = SMA seed,
    # then standard EMA recurrence with alpha = 2/(3+1) = 0.5.
    s = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0], index=_idx(5))
    out = ind.ema(s, 3)
    assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
    assert out.iloc[2] == pytest.approx((2 + 4 + 6) / 3)        # seed = 4.0
    # alpha=0.5 → ema[3] = 0.5*8 + 0.5*4 = 6.0
    assert out.iloc[3] == pytest.approx(6.0)
    # ema[4] = 0.5*10 + 0.5*6 = 8.0
    assert out.iloc[4] == pytest.approx(8.0)


def test_ema_short_series_all_nan():
    s = pd.Series([1.0, 2.0], index=_idx(2))
    out = ind.ema(s, 5)
    assert out.isna().all()


def test_true_range_with_gap():
    # H/L/C ; |H-prevC| and |L-prevC| dominate when there's a gap up
    df = pd.DataFrame(
        {
            "open": [10, 12, 14],
            "high": [11, 13, 20],   # 3rd bar gaps up
            "low":  [9, 11, 15],
            "close":[10, 12, 18],
        },
        index=_idx(3),
    )
    tr = ind.true_range(df)
    assert tr.iloc[0] == pytest.approx(2.0)        # 11-9
    # bar 2: max(13-11, |13-10|=3, |11-10|=1) = 3
    assert tr.iloc[1] == pytest.approx(3.0)
    # bar 3: H-L=5, |H-prevC|=|20-12|=8, |L-prevC|=|15-12|=3 → 8
    assert tr.iloc[2] == pytest.approx(8.0)


def test_atr_uses_wilder_smoothing():
    # Flat True Range → ATR equals the seed (and stays there).
    n = 30
    df = pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
        },
        index=_idx(n),
    )
    a = ind.atr(df, 14)
    # First 13 NaN, then ≈ 2.0 (each bar TR = 2)
    assert pd.isna(a.iloc[12])
    assert a.iloc[13] == pytest.approx(2.0, abs=1e-9)
    assert a.iloc[-1] == pytest.approx(2.0, abs=1e-9)


def test_body_pct_and_close_position():
    df = pd.DataFrame(
        {
            "open":  [10.0, 10.0, 10.0],
            "high":  [12.0, 12.0, 12.0],
            "low":   [8.0,  8.0,  8.0],
            "close": [11.0, 8.0,  12.0],   # close at 75%, 0%, 100% of range
        },
        index=_idx(3),
    )
    body = ind.body_pct(df)
    pos = ind.close_position_in_range(df)
    assert body.iloc[0] == pytest.approx(0.25)   # |11-10| / 4
    assert pos.iloc[0] == pytest.approx(0.75)
    assert pos.iloc[1] == pytest.approx(0.0)
    assert pos.iloc[2] == pytest.approx(1.0)


def test_rolling_max_min():
    s = pd.Series([1, 5, 3, 7, 2], index=_idx(5), dtype="float64")
    rmax = ind.rolling_max(s, 3)
    rmin = ind.rolling_min(s, 3)
    # First two NaN, then 3-bar windows
    assert pd.isna(rmax.iloc[1])
    assert rmax.iloc[2] == 5
    assert rmax.iloc[3] == 7
    assert rmax.iloc[4] == 7
    assert rmin.iloc[2] == 1
    assert rmin.iloc[4] == 2
