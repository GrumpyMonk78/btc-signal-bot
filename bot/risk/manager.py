"""
Risk manager — the deterministic last line of defence.

This module has **veto power** over Claude. Even if Claude returns a
confidence-10 enter decision with beautiful geometry, the risk manager
will reject it if any of these checks fail:

  1. Long-only (phase 1)                       — short → veto
  2. decision == "enter"                       — skip → veto (trivially)
  3. Direction is LONG                         — short → veto
  4. confidence >= MIN_CONFIDENCE              — Claude not confident enough
  5. R:R >= MIN_RR                             — geometry too tight
  6. SL within sane ATR multiple               — 0.3..4 * ATR
  7. daily_pnl > DAILY_STOP_PCT                — daily drawdown breached
  8. open_positions < MAX_OPEN_POSITIONS       — already at max exposure
  9. No high-impact news within blackout       — FOMC/CPI/NFP within ±30 min

Position sizing is computed deterministically (not asked of Claude):
    position_usd = RISK_PER_TRADE × equity / SL_distance_pct
    position_btc = position_usd / entry_price

Public API
----------
    evaluate(decision, portfolio, scanner_signal, *, now=None) -> RiskVerdict

`now` defaults to datetime.now(UTC). Calendar lookup is module-level.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import settings
from bot.data import calendar as macro_cal
from bot.storage.models import (
    Decision,
    DecisionDirection,
    PortfolioState,
    RiskVerdict,
)
from bot.strategy.scanner import ScannerSignal

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ATR sanity bounds — SL distance must be within [MIN, MAX] multiples of ATR.
# Below MIN  → SL too tight, normal noise will stop us out.
# Above MAX → SL too wide, position sizing forces tiny size or violates risk.
# ─────────────────────────────────────────────────────────────────────────────

ATR_SL_MIN_MULT = 0.3
ATR_SL_MAX_MULT = 4.0


@dataclass
class _Check:
    """Result of a single rule check."""

    passed: bool
    code: str
    detail: str = ""


def _check_decision_is_enter(d: Decision) -> _Check:
    if d.decision != "enter":
        return _Check(False, "not_enter", f"decision=={d.decision}")
    return _Check(True, "is_enter")


def _check_long_only(d: Decision) -> _Check:
    if d.direction != DecisionDirection.LONG:
        return _Check(False, "not_long_only",
                      f"direction={d.direction.value if d.direction else None}; phase 1 is long-only")
    return _Check(True, "is_long")


def _check_confidence(d: Decision) -> _Check:
    if d.confidence < settings.min_confidence:
        return _Check(False, "low_confidence",
                      f"confidence={d.confidence} < min={settings.min_confidence}")
    return _Check(True, "confidence_ok")


def _check_rr(d: Decision) -> _Check:
    rr = d.risk_reward_ratio()
    if rr < settings.min_rr:
        return _Check(False, "low_rr", f"R:R={rr:.2f} < min={settings.min_rr}")
    return _Check(True, "rr_ok")


def _check_atr_sanity(d: Decision, scanner_signal: ScannerSignal | None) -> _Check:
    """SL distance must be in [0.3, 4] × ATR.

    If we don't have an ATR (e.g. pseudo-trigger with NaN), we skip this
    check rather than veto — the risk manager shouldn't be stricter than
    the data allows. In practice, real scanner triggers always have ATR.
    """
    if scanner_signal is None:
        return _Check(True, "atr_skipped_no_scanner")
    atr = scanner_signal.context.get("atr14")
    if atr is None or atr != atr:  # NaN check
        return _Check(True, "atr_skipped_nan")
    if atr <= 0:
        return _Check(True, "atr_skipped_nonpositive")

    sl_dist = abs(d.entry_price - d.stop_loss)
    ratio = sl_dist / atr
    if ratio < ATR_SL_MIN_MULT:
        return _Check(False, "sl_too_tight",
                      f"SL distance {sl_dist:.2f} = {ratio:.2f}*ATR < {ATR_SL_MIN_MULT}*ATR")
    if ratio > ATR_SL_MAX_MULT:
        return _Check(False, "sl_too_wide",
                      f"SL distance {sl_dist:.2f} = {ratio:.2f}*ATR > {ATR_SL_MAX_MULT}*ATR")
    return _Check(True, "atr_ok")


def _check_daily_stop(p: PortfolioState) -> _Check:
    if p.daily_pnl_pct <= settings.daily_stop_pct:
        return _Check(False, "daily_stop_hit",
                      f"daily_pnl={p.daily_pnl_pct:+.4f} <= stop={settings.daily_stop_pct}")
    return _Check(True, "daily_stop_ok")


def _check_max_positions(p: PortfolioState) -> _Check:
    if p.open_positions >= settings.max_open_positions:
        return _Check(False, "max_positions",
                      f"open_positions={p.open_positions} >= max={settings.max_open_positions}")
    return _Check(True, "positions_ok")


def _check_news_blackout(now: datetime) -> _Check:
    in_blackout, ev = macro_cal.is_in_blackout_window(
        settings.news_blackout_minutes, now=now
    )
    if in_blackout and ev is not None:
        return _Check(False, "news_blackout",
                      f"within +/- {settings.news_blackout_minutes} min of {ev.name} at {ev.timestamp.isoformat()}")
    return _Check(True, "news_clear")


# ─────────────────────────────────────────────────────────────────────────────
# Position sizing
# ─────────────────────────────────────────────────────────────────────────────


def _compute_position_size(d: Decision, p: PortfolioState) -> tuple[float, float]:
    """Return (position_usd, position_btc).

    Notional sized so that hitting SL costs exactly RISK_PER_TRADE × equity.

        position_usd = risk_usd / SL_distance_pct
    where
        risk_usd = RISK_PER_TRADE * equity
        SL_distance_pct = |entry - SL| / entry

    Returns (0, 0) for skip decisions or invalid geometry.
    """
    if d.decision != "enter" or d.entry_price is None or d.stop_loss is None:
        return 0.0, 0.0
    if d.entry_price <= 0:
        return 0.0, 0.0
    sl_dist_pct = abs(d.entry_price - d.stop_loss) / d.entry_price
    if sl_dist_pct <= 0:
        return 0.0, 0.0
    risk_usd = settings.risk_per_trade * p.equity_usd
    position_usd = risk_usd / sl_dist_pct
    position_btc = position_usd / d.entry_price
    # Reduced size hint shrinks notional by half
    if d.size_hint == "reduced":
        position_usd *= 0.5
        position_btc *= 0.5
    return position_usd, position_btc


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────────────


def evaluate(
    decision: Decision,
    portfolio: PortfolioState,
    scanner_signal: ScannerSignal | None = None,
    *,
    now: datetime | None = None,
) -> RiskVerdict:
    """Run all rules. Return a RiskVerdict describing the outcome.

    Parameters
    ----------
    decision
        Claude's validated Decision.
    portfolio
        Current account state.
    scanner_signal
        Original scanner trigger, used for ATR sanity. Optional.
    now
        Reference time for the news-blackout check. Defaults to UTC now.

    Returns
    -------
    RiskVerdict with approved/rejected + reason + position sizing.
    """
    now = now or datetime.now(timezone.utc)

    # Always compute sizing + r/r for diagnostics, even if we'll reject.
    position_usd, position_btc = _compute_position_size(decision, portfolio)
    rr = decision.risk_reward_ratio()
    rr_safe = 0.0 if rr != rr or rr == float("inf") else rr  # NaN/inf → 0 for storage

    # Skip → fast path, not really a veto but we record reason.
    if decision.decision == "skip":
        return RiskVerdict(
            approved=False,
            reason="Claude decided to skip.",
            veto_codes=["claude_skip"],
            position_size_usd=position_usd,
            position_size_btc=position_btc,
            r_r_ratio=rr_safe,
        )

    # Run all enter-path checks
    checks: list[_Check] = [
        _check_decision_is_enter(decision),
        _check_long_only(decision),
        _check_confidence(decision),
        _check_rr(decision),
        _check_atr_sanity(decision, scanner_signal),
        _check_daily_stop(portfolio),
        _check_max_positions(portfolio),
        _check_news_blackout(now),
    ]
    failed = [c for c in checks if not c.passed]
    if failed:
        codes = [c.code for c in failed]
        reasons = "; ".join(c.detail for c in failed if c.detail)
        return RiskVerdict(
            approved=False,
            reason=f"vetoed: {reasons}"[:512],
            veto_codes=codes,
            position_size_usd=position_usd,
            position_size_btc=position_btc,
            r_r_ratio=rr_safe,
        )

    # All checks passed
    return RiskVerdict(
        approved=True,
        reason=(
            f"approved: confidence={decision.confidence}/10, "
            f"R:R={rr_safe:.2f}, size=${position_usd:.0f} ({position_btc:.6f} BTC)"
        )[:512],
        veto_codes=[],
        position_size_usd=position_usd,
        position_size_btc=position_btc,
        r_r_ratio=rr_safe,
    )
