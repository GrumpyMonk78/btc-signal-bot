"""
Tests for the Claude decider — JSON parsing strictness, budget guard,
and retry on transient failures. The Anthropic SDK is fully mocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bot.llm.decider import (
    BudgetExceededError,
    DeciderError,
    NonJsonResponseError,
    TokenBudget,
    decide,
    parse_decision,
)
from bot.llm.context import assemble_context
from bot.storage.models import (
    DeciderContext,
    Decision,
    PortfolioState,
)
from bot.strategy.scanner import ScannerSignal


# ─────────────────────────────────────────────────────────────────────────────
# parse_decision — strictness
# ─────────────────────────────────────────────────────────────────────────────


_VALID_SKIP = {
    "decision": "skip",
    "direction": None,
    "entry_price": None,
    "stop_loss": None,
    "take_profit": None,
    "confidence": 5,
    "size_hint": "skip",
    "reasoning": "weak setup",
    "key_risks": [],
    "invalidation": "",
}

_VALID_ENTER = {
    "decision": "enter",
    "direction": "long",
    "entry_price": 70000.0,
    "stop_loss": 69500.0,
    "take_profit": 71000.0,
    "confidence": 7,
    "size_hint": "normal",
    "reasoning": "good setup",
    "key_risks": [],
    "invalidation": "below 69500",
}


def test_parse_plain_json_skip():
    d = parse_decision(json.dumps(_VALID_SKIP))
    assert d.decision == "skip"


def test_parse_plain_json_enter():
    d = parse_decision(json.dumps(_VALID_ENTER))
    assert d.decision == "enter"
    assert d.risk_reward_ratio() == pytest.approx(2.0)


def test_parse_strips_code_fences():
    fenced = f"```json\n{json.dumps(_VALID_ENTER)}\n```"
    d = parse_decision(fenced)
    assert d.decision == "enter"


def test_parse_tolerates_minor_prose():
    """Prose before/after the JSON object is tolerated as long as JSON itself is intact."""
    text = f"Here is my decision:\n{json.dumps(_VALID_SKIP)}\nLet me know if you need more."
    d = parse_decision(text)
    assert d.decision == "skip"


def test_parse_rejects_pure_prose():
    with pytest.raises(NonJsonResponseError):
        parse_decision("I think we should probably skip this one because FOMC is soon.")


def test_parse_rejects_malformed_json():
    with pytest.raises(NonJsonResponseError):
        parse_decision('{"decision": "skip", broken')


def test_parse_rejects_invalid_geometry():
    bad = dict(_VALID_ENTER, stop_loss=70500.0)  # SL above entry for long → invalid
    with pytest.raises(DeciderError):
        parse_decision(json.dumps(bad))


def test_parse_rejects_skip_with_trade_fields():
    bad = dict(_VALID_SKIP, entry_price=70000.0)
    with pytest.raises(DeciderError):
        parse_decision(json.dumps(bad))


# ─────────────────────────────────────────────────────────────────────────────
# TokenBudget
# ─────────────────────────────────────────────────────────────────────────────


def test_budget_blocks_when_exhausted():
    b = TokenBudget(daily_limit=1000, spent_today=900)
    assert b.can_afford(50, 60) is False   # 950+60 > 1000
    assert b.can_afford(50, 49) is True    # 900+50+49 < 1000


def test_budget_records_usage():
    b = TokenBudget(daily_limit=10_000)
    b.record(1500, 300)
    assert b.spent_today == 1800


# ─────────────────────────────────────────────────────────────────────────────
# decide() with mocked Anthropic client
# ─────────────────────────────────────────────────────────────────────────────


def _make_context() -> DeciderContext:
    idx = pd.date_range("2026-05-01", periods=30, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {"open": [70000.0]*30, "high": [70100.0]*30, "low": [69900.0]*30,
         "close": [70000.0]*30, "volume": [0.5]*30, "vwap":[70000.0]*30,
         "trade_count":[10]*30},
        index=idx,
    )
    signal = ScannerSignal(
        timestamp=pd.Timestamp("2026-05-01T20:00:00", tz="UTC"),
        filter="ema_pullback",
        price=70000.0,
        context={"ema20": 70000.0, "ema50": 69800.0, "atr14": 420.0,
                 "vol_ma20": 0.5, "h4_uptrend": 1.0},
    )
    return assemble_context(
        instrument="BTC/USD",
        primary_df=df,
        context_df=df,
        trigger=signal,
        portfolio=PortfolioState(
            equity_usd=10_000.0, open_positions=0,
            daily_pnl_pct=0.0, remaining_position_slots=3,
        ),
        as_of=datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc),
        max_primary_bars=10, max_context_bars=10,
    )


def _mock_client(response_text: str, *, input_tokens: int = 1500, output_tokens: int = 200) -> MagicMock:
    """Build a mock anthropic.Anthropic client that returns `response_text`."""
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=response_text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    return client


def test_decide_happy_path_skip():
    ctx = _make_context()
    client = _mock_client(json.dumps(_VALID_SKIP))
    budget = TokenBudget(daily_limit=10_000)

    result = decide(ctx, client=client, budget=budget)

    assert result.decision.decision == "skip"
    assert result.input_tokens == 1500
    assert result.output_tokens == 200
    assert result.attempts == 1
    assert result.prompt_version.startswith("v")
    # Budget was updated
    assert budget.spent_today == 1700


def test_decide_happy_path_enter_with_fences():
    ctx = _make_context()
    client = _mock_client(f"```json\n{json.dumps(_VALID_ENTER)}\n```")
    budget = TokenBudget(daily_limit=10_000)

    result = decide(ctx, client=client, budget=budget)
    assert result.decision.decision == "enter"
    assert result.decision.direction.value == "long"


def test_decide_retries_on_non_json_then_succeeds():
    ctx = _make_context()
    client = MagicMock()
    # First call: prose only; second call: valid JSON.
    client.messages.create.side_effect = [
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text="I cannot output JSON.")],
            usage=SimpleNamespace(input_tokens=1500, output_tokens=100),
        ),
        SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(_VALID_SKIP))],
            usage=SimpleNamespace(input_tokens=1500, output_tokens=150),
        ),
    ]
    budget = TokenBudget(daily_limit=10_000)

    result = decide(ctx, client=client, budget=budget, max_attempts=2)
    assert result.decision.decision == "skip"
    assert result.attempts == 2


def test_decide_does_not_retry_on_validation_error():
    """If Claude's geometry is illegal, retrying just burns more tokens.
    The pipeline should surface the error immediately."""
    ctx = _make_context()
    bad = dict(_VALID_ENTER, stop_loss=70500.0)  # SL > entry for long → invalid
    client = _mock_client(json.dumps(bad))
    budget = TokenBudget(daily_limit=10_000)

    with pytest.raises(DeciderError):
        decide(ctx, client=client, budget=budget)
    # Exactly one call was made (no retry on geometry error)
    assert client.messages.create.call_count == 1


def test_decide_blocks_when_budget_exhausted():
    ctx = _make_context()
    client = _mock_client(json.dumps(_VALID_SKIP))
    # Tiny budget — won't fit a ~1500-token call
    budget = TokenBudget(daily_limit=100)

    with pytest.raises(BudgetExceededError):
        decide(ctx, client=client, budget=budget)
    # No API call should have happened
    client.messages.create.assert_not_called()
