"""
End-to-end assembly + rendering of a DeciderContext.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from bot.llm.context import (
    assemble_context,
    bars_from_dataframe,
    render_context_for_prompt,
    scanner_signal_to_trigger,
)
from bot.llm.prompts import active_prompt, prompt_hash
from bot.storage.models import (
    MacroEvent,
    NewsItem,
    PortfolioState,
    SentimentSnapshot,
)
from bot.strategy.scanner import ScannerSignal


def _ohlc_df(n: int = 30, price: float = 70_000.0) -> pd.DataFrame:
    idx = pd.date_range("2026-05-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [price] * n,
            "high": [price + 100] * n,
            "low": [price - 100] * n,
            "close": [price] * n,
            "volume": [0.5] * n,
            "vwap": [price] * n,
            "trade_count": [50] * n,
        },
        index=idx,
    )


def _signal() -> ScannerSignal:
    return ScannerSignal(
        timestamp=pd.Timestamp("2026-05-01T05:00:00", tz="UTC"),
        filter="ema_pullback",
        price=70_000.0,
        context={"ema20": 69_900.0, "ema50": 69_500.0, "atr14": 420.0,
                 "vol_ma20": 0.5, "h4_uptrend": 1.0},
    )


def test_bars_from_dataframe_caps_count():
    df = _ohlc_df(100)
    bars = bars_from_dataframe(df, max_bars=20)
    assert len(bars) == 20
    # Oldest first
    assert bars[0].timestamp < bars[-1].timestamp


def test_scanner_signal_to_trigger_preserves_filter():
    t = scanner_signal_to_trigger(_signal())
    assert t.filter == "ema_pullback"
    assert t.price == 70_000.0
    assert "ema20" in t.notes


def test_assemble_context_end_to_end():
    primary = _ohlc_df(60)
    context = _ohlc_df(40, price=69_500.0)
    portfolio = PortfolioState(
        equity_usd=10_000.0,
        open_positions=0,
        daily_pnl_pct=0.0,
        remaining_position_slots=3,
    )
    news = [
        NewsItem(
            timestamp=datetime(2026, 5, 1, 4, 0, tzinfo=timezone.utc),
            source="CoinDesk",
            title="Spot BTC ETF inflows hit $400M",
        )
    ]
    sentiment = SentimentSnapshot(value=58, classification="Greed",
                                  trend_7d=[50, 52, 53, 55, 56, 57, 58])
    macro_upcoming = [
        MacroEvent(
            timestamp=datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc),
            name="FOMC Rate Decision",
        )
    ]

    ctx = assemble_context(
        instrument="BTC/USD",
        primary_df=primary,
        context_df=context,
        trigger=_signal(),
        news=news,
        sentiment=sentiment,
        macro_recent=[],
        macro_upcoming=macro_upcoming,
        portfolio=portfolio,
        as_of=datetime(2026, 5, 1, 5, 0, tzinfo=timezone.utc),
        max_primary_bars=30,
        max_context_bars=30,
    )

    assert ctx.instrument == "BTC/USD"
    assert len(ctx.bars_primary) == 30
    assert len(ctx.bars_context) == 30
    assert ctx.trigger.filter == "ema_pullback"
    assert ctx.sentiment is not None
    assert ctx.sentiment.value == 58
    assert len(ctx.macro_upcoming) == 1


def test_render_context_produces_compact_string():
    primary = _ohlc_df(60)
    context = _ohlc_df(40, price=69_500.0)
    portfolio = PortfolioState(
        equity_usd=10_000.0, open_positions=0,
        daily_pnl_pct=-0.005, remaining_position_slots=3,
    )
    ctx = assemble_context(
        instrument="BTC/USD",
        primary_df=primary,
        context_df=context,
        trigger=_signal(),
        portfolio=portfolio,
        as_of=datetime(2026, 5, 1, 5, 0, tzinfo=timezone.utc),
        max_primary_bars=10,
        max_context_bars=10,
    )
    rendered = render_context_for_prompt(ctx)
    # Sanity: must include the trigger and the portfolio block
    assert "ema_pullback" in rendered
    assert "equity_usd" in rendered
    assert "scanner_trigger" in rendered
    # And it should be compact — under ~3k chars with 10+10 bars
    assert len(rendered) < 3000


def test_active_prompt_hash_is_stable():
    v1, txt1, h1 = active_prompt()
    v2, txt2, h2 = active_prompt()
    assert v1 == v2
    assert h1 == h2
    # The hash must change if you edit the text
    assert prompt_hash(txt1 + " ") != h1
