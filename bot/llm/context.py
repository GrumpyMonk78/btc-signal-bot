"""
Context assembly — turn a scanner trigger + raw data sources into a
strongly-typed `DeciderContext`. Also provides a compact text renderer
used in the actual Claude user message.

The renderer is deliberately terse — token budget matters. We don't
serialise 200 bars as JSON; we use a compact OHLCV table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pandas as pd

from bot.storage.models import (
    Bar,
    DeciderContext,
    MacroEvent,
    NewsItem,
    PortfolioState,
    ScannerTrigger,
    SentimentSnapshot,
)
from bot.strategy.scanner import ScannerSignal


# ─────────────────────────────────────────────────────────────────────────────
# Assembly
# ─────────────────────────────────────────────────────────────────────────────


def bars_from_dataframe(df: pd.DataFrame, max_bars: int = 30) -> list[Bar]:
    """Take the last `max_bars` rows of an OHLCV DataFrame and convert
    them to `Bar` instances (oldest first)."""
    if df.empty:
        return []
    df = df.tail(max_bars)
    bars: list[Bar] = []
    for ts, row in df.iterrows():
        bars.append(Bar(
            timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        ))
    return bars


def scanner_signal_to_trigger(sig: ScannerSignal) -> ScannerTrigger:
    """Adapt a ScannerSignal dataclass to the pydantic ScannerTrigger."""
    return ScannerTrigger(
        filter=sig.filter,
        timestamp=sig.timestamp.to_pydatetime() if hasattr(sig.timestamp, "to_pydatetime") else sig.timestamp,
        price=float(sig.price),
        notes={k: float(v) for k, v in sig.context.items()},
    )


def assemble_context(
    *,
    instrument: str,
    primary_df: pd.DataFrame,
    context_df: pd.DataFrame,
    trigger: ScannerSignal,
    news: Iterable[NewsItem] = (),
    sentiment: SentimentSnapshot | None = None,
    macro_recent: Iterable[MacroEvent] = (),
    macro_upcoming: Iterable[MacroEvent] = (),
    portfolio: PortfolioState,
    as_of: datetime | None = None,
    max_primary_bars: int = 30,
    max_context_bars: int = 30,
) -> DeciderContext:
    """Bundle everything into a validated DeciderContext."""
    return DeciderContext(
        instrument=instrument,
        as_of=as_of or datetime.now(timezone.utc),
        bars_primary=bars_from_dataframe(primary_df, max_primary_bars),
        bars_context=bars_from_dataframe(context_df, max_context_bars),
        trigger=scanner_signal_to_trigger(trigger),
        news_last_24h=list(news),
        sentiment=sentiment,
        macro_recent=list(macro_recent),
        macro_upcoming=list(macro_upcoming),
        portfolio=portfolio,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Compact text renderer for the prompt
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_bar_row(b: Bar) -> str:
    return (
        f"{b.timestamp.strftime('%m-%dT%H:%MZ')} "
        f"O={b.open:.2f} H={b.high:.2f} L={b.low:.2f} C={b.close:.2f} V={b.volume:.4f}"
    )


def _fmt_news(items: list[NewsItem]) -> str:
    if not items:
        return "(none)"
    return "\n".join(
        f"  - {it.timestamp.strftime('%m-%dT%H:%MZ')} [{it.source}] {it.title}"
        for it in items[:10]
    )


def _fmt_events(events: list[MacroEvent], label: str) -> str:
    if not events:
        return f"  ({label}: none)"
    return "\n".join(
        f"  - {ev.timestamp.strftime('%m-%dT%H:%MZ')} {ev.name} [{ev.region}]"
        for ev in events[:5]
    )


def render_context_for_prompt(ctx: DeciderContext) -> str:
    """Produce the user message string that goes to Claude.

    Designed to be:
      - compact (tokens cost money)
      - LLM-friendly (clear sections, consistent format)
      - lossless on the fields that actually drive the decision
    """
    lines: list[str] = []

    lines.append("<context>")
    lines.append(f"instrument: {ctx.instrument}")
    lines.append(f"as_of: {ctx.as_of.isoformat()}")
    lines.append("")

    # ── Scanner trigger ──────────────────────────────────────────────────
    t = ctx.trigger
    lines.append("scanner_trigger:")
    lines.append(f"  filter: {t.filter}")
    lines.append(f"  timestamp: {t.timestamp.isoformat()}")
    lines.append(f"  price: {t.price:.2f}")
    if t.notes:
        key_notes = {k: t.notes[k] for k in ("ema20", "ema50", "atr14", "h4_uptrend", "vol_ma20")
                     if k in t.notes}
        for k, v in key_notes.items():
            lines.append(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")

    # ── Bars ─────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"bars_primary (last {len(ctx.bars_primary)} H1, oldest first):")
    for b in ctx.bars_primary:
        lines.append("  " + _fmt_bar_row(b))

    lines.append("")
    lines.append(f"bars_context (last {len(ctx.bars_context)} H4, oldest first):")
    for b in ctx.bars_context:
        lines.append("  " + _fmt_bar_row(b))

    # ── News ─────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("news_last_24h:")
    lines.append(_fmt_news(ctx.news_last_24h))

    # ── Sentiment ────────────────────────────────────────────────────────
    lines.append("")
    if ctx.sentiment:
        lines.append(
            f"sentiment: F&G={ctx.sentiment.value} "
            f"({ctx.sentiment.classification}), "
            f"7d trend={ctx.sentiment.trend_7d}"
        )
    else:
        lines.append("sentiment: (unavailable)")

    # ── Macro ────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("macro_recent:")
    lines.append(_fmt_events(list(ctx.macro_recent), "recent"))
    lines.append("macro_upcoming:")
    lines.append(_fmt_events(list(ctx.macro_upcoming), "upcoming"))

    # ── Portfolio ────────────────────────────────────────────────────────
    p = ctx.portfolio
    lines.append("")
    lines.append("portfolio:")
    lines.append(f"  equity_usd: {p.equity_usd:.2f}")
    lines.append(f"  open_positions: {p.open_positions}")
    lines.append(f"  daily_pnl_pct: {p.daily_pnl_pct:+.4f}")
    lines.append(f"  remaining_position_slots: {p.remaining_position_slots}")

    lines.append("</context>")
    return "\n".join(lines)
