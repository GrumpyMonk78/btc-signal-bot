"""
Strict-validation tests for pydantic models, especially Decision.

If Claude returns garbage, the Decision model is the firewall — it must
reject any combination that the risk manager can't trust.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from bot.storage.models import (
    Bar,
    Decision,
    DecisionDirection,
    PortfolioState,
    SentimentSnapshot,
)


# ─────────────────────────────────────────────────────────────────────────────
# Decision — the most critical model
# ─────────────────────────────────────────────────────────────────────────────


def _valid_long_decision(**overrides) -> dict:
    base = {
        "decision": "enter",
        "direction": "long",
        "entry_price": 70000.0,
        "stop_loss": 69500.0,
        "take_profit": 71000.0,
        "confidence": 7,
        "size_hint": "normal",
        "reasoning": "test",
        "key_risks": [],
        "invalidation": "",
    }
    base.update(overrides)
    return base


def test_decision_valid_long():
    d = Decision(**_valid_long_decision())
    assert d.decision == "enter"
    assert d.direction == DecisionDirection.LONG
    assert d.risk_reward_ratio() == pytest.approx(2.0)


def test_decision_valid_skip():
    d = Decision(
        decision="skip",
        confidence=5,
        size_hint="skip",
        reasoning="setup unclear",
    )
    assert d.decision == "skip"
    assert d.direction is None
    assert d.entry_price is None


def test_decision_skip_with_trade_fields_rejected():
    # SKIP must have all trade fields null.
    with pytest.raises(ValidationError) as exc:
        Decision(
            decision="skip",
            direction="long",   # ← not allowed with skip
            confidence=5,
            size_hint="skip",
            reasoning="x",
        )
    assert "skip" in str(exc.value).lower()


def test_decision_enter_missing_fields_rejected():
    with pytest.raises(ValidationError) as exc:
        Decision(
            decision="enter",
            direction="long",
            entry_price=70000.0,
            # stop_loss missing
            take_profit=71000.0,
            confidence=7,
            reasoning="x",
        )
    assert "stop_loss" in str(exc.value)


def test_decision_long_with_sl_above_entry_rejected():
    with pytest.raises(ValidationError) as exc:
        Decision(**_valid_long_decision(stop_loss=70500.0))
    assert "stop_loss" in str(exc.value)


def test_decision_long_with_tp_below_entry_rejected():
    with pytest.raises(ValidationError) as exc:
        Decision(**_valid_long_decision(take_profit=69900.0))
    assert "take_profit" in str(exc.value)


def test_decision_short_geometry():
    # Risk manager rejects shorts in phase 1, but the model itself supports
    # short geometry to keep things sane.
    d = Decision(
        decision="enter",
        direction="short",
        entry_price=70000.0,
        stop_loss=70500.0,
        take_profit=69000.0,
        confidence=7,
        size_hint="normal",
        reasoning="x",
    )
    assert d.risk_reward_ratio() == pytest.approx(2.0)


def test_decision_confidence_bounds():
    with pytest.raises(ValidationError):
        Decision(**_valid_long_decision(confidence=0))
    with pytest.raises(ValidationError):
        Decision(**_valid_long_decision(confidence=11))


def test_decision_size_hint_skip_only_when_skip():
    with pytest.raises(ValidationError):
        Decision(**_valid_long_decision(size_hint="skip"))


# ─────────────────────────────────────────────────────────────────────────────
# Bar
# ─────────────────────────────────────────────────────────────────────────────


def test_bar_rejects_high_below_low():
    with pytest.raises(ValidationError):
        Bar(
            timestamp=datetime.now(timezone.utc),
            open=100, high=99, low=101, close=100, volume=1,
        )


def test_bar_rejects_close_outside_range():
    with pytest.raises(ValidationError):
        Bar(
            timestamp=datetime.now(timezone.utc),
            open=100, high=101, low=99, close=102, volume=1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Sentiment
# ─────────────────────────────────────────────────────────────────────────────


def test_sentiment_value_bounds():
    SentimentSnapshot(value=0, classification="Extreme Fear")
    SentimentSnapshot(value=100, classification="Extreme Greed")
    with pytest.raises(ValidationError):
        SentimentSnapshot(value=-1, classification="x")
    with pytest.raises(ValidationError):
        SentimentSnapshot(value=101, classification="x")


def test_sentiment_trend_must_stay_in_range():
    with pytest.raises(ValidationError):
        SentimentSnapshot(value=50, classification="Neutral", trend_7d=[50, 50, 200])


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio
# ─────────────────────────────────────────────────────────────────────────────


def test_portfolio_equity_must_be_positive():
    with pytest.raises(ValidationError):
        PortfolioState(
            equity_usd=0, open_positions=0,
            daily_pnl_pct=0.0, remaining_position_slots=3,
        )
