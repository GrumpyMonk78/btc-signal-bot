"""
Tests for the risk manager. Every veto rule has positive + negative cases.

The risk manager has final say over Claude — so we test it harder than
anything else in the codebase.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from bot.risk.manager import (
    ATR_SL_MAX_MULT,
    ATR_SL_MIN_MULT,
    _compute_position_size,
    evaluate,
)
from bot.storage.models import (
    Decision,
    DecisionDirection,
    PortfolioState,
)
from bot.strategy.scanner import ScannerSignal


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _decision(**over) -> Decision:
    base = dict(
        decision="enter",
        direction="long",
        entry_price=70_000.0,
        stop_loss=69_300.0,    # 700 USD = 1% — within ATR sanity for 420 ATR
        take_profit=71_500.0,  # 1500 reward / 700 risk = 2.14 R:R
        confidence=7,
        size_hint="normal",
        reasoning="good setup",
        key_risks=[],
        invalidation="below 69300",
    )
    base.update(over)
    return Decision(**base)


def _portfolio(**over) -> PortfolioState:
    base = dict(
        equity_usd=10_000.0,
        open_positions=0,
        daily_pnl_pct=0.0,
        remaining_position_slots=3,
    )
    base.update(over)
    return PortfolioState(**base)


def _signal(atr14: float = 420.0) -> ScannerSignal:
    return ScannerSignal(
        timestamp=pd.Timestamp("2026-05-21T05:00:00", tz="UTC"),
        filter="ema_pullback",
        price=70_000.0,
        context={"ema20": 70_000.0, "ema50": 69_500.0, "atr14": atr14,
                 "vol_ma20": 0.5, "h4_uptrend": 1.0},
    )


# A safe "now" — far from any calendar event in 2026 calendar.
_QUIET_NOW = datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────


def test_approved_happy_path():
    v = evaluate(_decision(), _portfolio(), _signal(), now=_QUIET_NOW)
    assert v.approved is True
    assert v.veto_codes == []
    # Sizing: risk = 1% * 10k = 100 USD. SL_dist_pct = 700/70000 = 1%.
    # → position = 100/0.01 = 10_000 USD.
    assert v.position_size_usd == pytest.approx(10_000.0, rel=1e-6)
    assert v.position_size_btc == pytest.approx(10_000.0 / 70_000.0, rel=1e-6)
    assert v.r_r_ratio == pytest.approx(1500 / 700, rel=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# Skip path
# ─────────────────────────────────────────────────────────────────────────────


def test_skip_decision_returns_not_approved():
    d = Decision(decision="skip", confidence=4, size_hint="skip", reasoning="meh")
    v = evaluate(d, _portfolio(), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "claude_skip" in v.veto_codes
    assert v.position_size_usd == 0
    assert v.position_size_btc == 0


# ─────────────────────────────────────────────────────────────────────────────
# Long-only veto
# ─────────────────────────────────────────────────────────────────────────────


def test_short_decision_rejected():
    d = Decision(
        decision="enter",
        direction="short",
        entry_price=70_000.0,
        stop_loss=70_700.0,
        take_profit=68_500.0,
        confidence=8,
        size_hint="normal",
        reasoning="x",
    )
    v = evaluate(d, _portfolio(), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "not_long_only" in v.veto_codes


# ─────────────────────────────────────────────────────────────────────────────
# Confidence
# ─────────────────────────────────────────────────────────────────────────────


def test_low_confidence_rejected():
    v = evaluate(_decision(confidence=4), _portfolio(), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "low_confidence" in v.veto_codes


# ─────────────────────────────────────────────────────────────────────────────
# R:R
# ─────────────────────────────────────────────────────────────────────────────


def test_low_rr_rejected():
    # R:R = 600/700 ≈ 0.86 < 1.5
    d = _decision(stop_loss=69_300.0, take_profit=70_600.0)
    v = evaluate(d, _portfolio(), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "low_rr" in v.veto_codes


# ─────────────────────────────────────────────────────────────────────────────
# ATR sanity
# ─────────────────────────────────────────────────────────────────────────────


def test_sl_too_tight_rejected():
    # ATR = 420, MIN = 0.3 → SL must be ≥ 126 USD away.
    # Use SL just 50 USD below entry.
    d = _decision(stop_loss=69_950.0, take_profit=70_500.0)
    v = evaluate(d, _portfolio(), _signal(atr14=420.0), now=_QUIET_NOW)
    assert v.approved is False
    assert "sl_too_tight" in v.veto_codes


def test_sl_too_wide_rejected():
    # ATR = 100, MAX = 4 → SL must be ≤ 400 USD away.
    # Use SL 1000 USD below entry.
    d = _decision(stop_loss=69_000.0, take_profit=72_000.0)
    v = evaluate(d, _portfolio(), _signal(atr14=100.0), now=_QUIET_NOW)
    assert v.approved is False
    assert "sl_too_wide" in v.veto_codes


def test_atr_skipped_when_nan():
    # NaN ATR (pseudo-trigger case) → ATR check passes (skipped).
    # We still expect approval because all other checks are fine.
    sig = _signal()
    sig.context["atr14"] = float("nan")
    v = evaluate(_decision(), _portfolio(), sig, now=_QUIET_NOW)
    assert v.approved is True


def test_atr_skipped_when_no_scanner_signal():
    v = evaluate(_decision(), _portfolio(), scanner_signal=None, now=_QUIET_NOW)
    assert v.approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Daily stop
# ─────────────────────────────────────────────────────────────────────────────


def test_daily_stop_breach_rejected():
    v = evaluate(_decision(), _portfolio(daily_pnl_pct=-0.035), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "daily_stop_hit" in v.veto_codes


def test_daily_stop_exactly_at_boundary_is_breach():
    # daily_pnl_pct <= DAILY_STOP_PCT → breach. Equal also blocks.
    v = evaluate(_decision(), _portfolio(daily_pnl_pct=-0.03), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "daily_stop_hit" in v.veto_codes


# ─────────────────────────────────────────────────────────────────────────────
# Max positions
# ─────────────────────────────────────────────────────────────────────────────


def test_max_positions_rejected():
    v = evaluate(_decision(), _portfolio(open_positions=3), _signal(), now=_QUIET_NOW)
    assert v.approved is False
    assert "max_positions" in v.veto_codes


# ─────────────────────────────────────────────────────────────────────────────
# News blackout
# ─────────────────────────────────────────────────────────────────────────────


def test_news_blackout_rejected():
    # 2026-01-28T19:00 is FOMC in our calendar. 25 min before → blackout.
    fomc_minus_25 = datetime(2026, 1, 28, 18, 35, tzinfo=timezone.utc)
    v = evaluate(_decision(), _portfolio(), _signal(), now=fomc_minus_25)
    assert v.approved is False
    assert "news_blackout" in v.veto_codes


def test_news_blackout_clear_outside_window():
    # 45 min before FOMC — outside the 30 min blackout
    fomc_minus_45 = datetime(2026, 1, 28, 18, 15, tzinfo=timezone.utc)
    v = evaluate(_decision(), _portfolio(), _signal(), now=fomc_minus_45)
    assert v.approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Multiple veto codes
# ─────────────────────────────────────────────────────────────────────────────


def test_multiple_vetoes_listed():
    # Low confidence AND max positions reached → both codes
    v = evaluate(
        _decision(confidence=4),
        _portfolio(open_positions=3),
        _signal(),
        now=_QUIET_NOW,
    )
    assert v.approved is False
    assert "low_confidence" in v.veto_codes
    assert "max_positions" in v.veto_codes


# ─────────────────────────────────────────────────────────────────────────────
# Sizing math
# ─────────────────────────────────────────────────────────────────────────────


def test_sizing_with_reduced_hint_halves_notional():
    d = _decision(size_hint="reduced")
    p = _portfolio()
    usd, btc = _compute_position_size(d, p)
    # Full size would be 10_000 USD; reduced = 5_000.
    assert usd == pytest.approx(5_000.0, rel=1e-6)
    assert btc == pytest.approx(5_000.0 / 70_000.0, rel=1e-6)


def test_sizing_skip_returns_zero():
    d = Decision(decision="skip", confidence=5, size_hint="skip", reasoning="x")
    usd, btc = _compute_position_size(d, _portfolio())
    assert usd == 0
    assert btc == 0


def test_sizing_scales_linearly_with_equity():
    # 5k equity → half the notional of 10k
    usd_10k, _ = _compute_position_size(_decision(), _portfolio(equity_usd=10_000.0))
    usd_5k, _ = _compute_position_size(_decision(), _portfolio(equity_usd=5_000.0))
    assert usd_5k == pytest.approx(usd_10k / 2, rel=1e-6)
