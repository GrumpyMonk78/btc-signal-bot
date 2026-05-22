"""
Pipeline orchestrator — one full scan→Claude→risk→DB→Telegram pass.

Called by the scheduler once per H1 candle close. Idempotent in the sense
that it logs everything to the DB; the scheduler decides when to run it.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from bot.config import settings
from bot.data.market import BarRequest, MarketDataProvider
from bot.llm.context import assemble_context, render_context_for_prompt
from bot.llm.decider import (
    BudgetExceededError,
    DeciderError,
    DeciderResult,
    decide as claude_decide,
)
from bot.risk.manager import evaluate as risk_evaluate
from bot.storage import db as storage_db
from bot.storage.models import (
    ApprovedSignal,
    Decision,
    PortfolioState,
    RiskVerdict,
)
from bot.strategy.scanner import ScannerSignal, scan as run_scanner

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PipelineResult:
    """What happened during this pass — used by scheduler for logging + daily summary."""

    scan_id: int
    n_signals_in_scanner: int
    triggered: bool
    decision: Decision | None = None
    decider_result: DeciderResult | None = None
    verdict: RiskVerdict | None = None
    signal_id: str | None = None
    telegram_sent: bool = False
    error: str | None = None

    def summary_line(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        if not self.triggered:
            return f"scan_id={self.scan_id}: no fresh trigger ({self.n_signals_in_scanner} historical)"
        if self.decision is None:
            return f"scan_id={self.scan_id}: triggered but no decision"
        if self.verdict and self.verdict.approved:
            return (f"scan_id={self.scan_id}: ENTER conf={self.decision.confidence} "
                    f"→ signal {self.signal_id} (telegram_sent={self.telegram_sent})")
        return (f"scan_id={self.scan_id}: {self.decision.decision} "
                f"conf={self.decision.confidence} → vetoed: {self.verdict.veto_codes if self.verdict else 'n/a'}")


# ─────────────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────────────


# Only a trigger that landed on the most recent closed bar counts as "fresh"
# for the live scheduler. Historical signals from old bars don't get sent
# to Claude — they're stored only in the scan log.
_FRESH_WINDOW = timedelta(hours=1, minutes=5)


def run_once(
    *,
    provider: MarketDataProvider,
    portfolio: PortfolioState,
    conn: sqlite3.Connection,
    bars_primary: int = 200,
    bars_context: int = 200,
    telegram_client: Any | None = None,
    claude_client: Any | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    """One full pipeline pass. Catches errors and returns them in the result
    so the scheduler keeps running even if Claude/Alpaca have a hiccup."""
    now = now or datetime.now(timezone.utc)

    try:
        primary = provider.fetch_bars(BarRequest(
            symbol=settings.instrument,
            timeframe=settings.timeframe_primary,
            limit=bars_primary,
        ))
        context = provider.fetch_bars(BarRequest(
            symbol=settings.instrument,
            timeframe=settings.timeframe_context,
            limit=bars_context,
        ))
    except Exception as exc:
        logger.exception("pipeline: market data fetch failed")
        return PipelineResult(scan_id=-1, n_signals_in_scanner=0, triggered=False,
                              error=f"market data fetch failed: {exc}")

    signals = run_scanner(primary, context)
    last_signal = signals[-1] if signals else None
    latest_ts = last_signal.timestamp.to_pydatetime() if last_signal else None

    # Log the scan regardless of outcome — audit-first
    scan_id = storage_db.insert_scan(
        conn,
        instrument=settings.instrument,
        bars_primary=len(primary),
        bars_context=len(context),
        n_signals=len(signals),
        latest_filter=last_signal.filter if last_signal else None,
        latest_signal_ts=latest_ts,
        notes="live pipeline",
    )

    # ── Is the latest signal "fresh" (i.e. on the most recent closed bar)? ──
    triggered = (
        last_signal is not None
        and (now - latest_ts) < _FRESH_WINDOW
    )

    if not triggered:
        logger.info("pipeline scan_id=%d: no fresh trigger (%d historical signals)",
                    scan_id, len(signals))
        return PipelineResult(scan_id=scan_id, n_signals_in_scanner=len(signals),
                              triggered=False)

    # ── Call Claude ────────────────────────────────────────────────────────
    ctx = assemble_context(
        instrument=settings.instrument,
        primary_df=primary,
        context_df=context,
        trigger=last_signal,
        portfolio=portfolio,
        as_of=now,
        max_primary_bars=30,
        max_context_bars=30,
        # News / sentiment / macro are fetched inside scheduler and passed
        # here later. For phase 1 we keep them empty to keep run_once free
        # of network for testing; scheduler wraps with real data.
    )
    user_text = render_context_for_prompt(ctx)

    try:
        decider_result = claude_decide(ctx, client=claude_client)
    except BudgetExceededError as exc:
        logger.warning("pipeline scan_id=%d: budget exceeded — %s", scan_id, exc)
        return PipelineResult(scan_id=scan_id, n_signals_in_scanner=len(signals),
                              triggered=True, error=f"budget: {exc}")
    except DeciderError as exc:
        logger.exception("pipeline scan_id=%d: Claude failed", scan_id)
        return PipelineResult(scan_id=scan_id, n_signals_in_scanner=len(signals),
                              triggered=True, error=f"claude: {exc}")

    d = decider_result.decision
    call_id = storage_db.insert_claude_call(
        conn,
        scan_id=scan_id,
        model=decider_result.model,
        prompt_version=decider_result.prompt_version,
        prompt_hash=decider_result.prompt_hash,
        user_message=user_text,
        raw_response=decider_result.raw_response,
        input_tokens=decider_result.input_tokens,
        output_tokens=decider_result.output_tokens,
        latency_ms=decider_result.latency_ms,
        attempts=decider_result.attempts,
    )
    decision_id = storage_db.insert_decision(
        conn,
        claude_call_id=call_id,
        instrument=settings.instrument,
        trigger_filter=last_signal.filter,
        trigger_ts=last_signal.timestamp.to_pydatetime(),
        trigger_price=float(last_signal.price),
        decision=d,
    )

    # ── Risk manager ───────────────────────────────────────────────────────
    verdict = risk_evaluate(d, portfolio, last_signal, now=now)
    storage_db.insert_veto(conn, decision_id=decision_id, verdict=verdict)

    if not (verdict.approved and d.decision == "enter" and d.direction is not None):
        logger.info("pipeline scan_id=%d: not approved (%s)", scan_id, verdict.veto_codes)
        return PipelineResult(scan_id=scan_id, n_signals_in_scanner=len(signals),
                              triggered=True, decision=d, decider_result=decider_result,
                              verdict=verdict)

    # ── Approved → log signal + send Telegram ──────────────────────────────
    signal_id = storage_db.insert_signal(
        conn,
        decision_id=decision_id,
        instrument=settings.instrument,
        direction=d.direction.value,
        entry_price=float(d.entry_price),
        stop_loss=float(d.stop_loss),
        take_profit=float(d.take_profit),
        position_usd=verdict.position_size_usd,
        position_btc=verdict.position_size_btc,
        confidence=d.confidence,
        rr_ratio=verdict.r_r_ratio,
        sent_to_telegram=False,
    )

    telegram_sent = False
    try:
        from bot.notify import telegram as tg
        if tg.is_configured():
            approved = ApprovedSignal(
                signal_id=signal_id,
                instrument=settings.instrument,
                direction=d.direction,
                entry_price=float(d.entry_price),
                stop_loss=float(d.stop_loss),
                take_profit=float(d.take_profit),
                position_size_usd=verdict.position_size_usd,
                position_size_btc=verdict.position_size_btc,
                confidence=d.confidence,
                r_r_ratio=verdict.r_r_ratio,
                reasoning=d.reasoning,
                key_risks=d.key_risks,
                invalidation=d.invalidation,
                created_at=now,
            )
            telegram_sent = tg.send_signal(approved, d, client=telegram_client)
            if telegram_sent:
                conn.execute("UPDATE signals SET sent_to_telegram=1 WHERE signal_id=?",
                             (signal_id,))
    except Exception as exc:
        logger.exception("pipeline scan_id=%d: telegram send failed (signal logged anyway)", scan_id)
        # Don't fail the pipeline on Telegram errors

    return PipelineResult(
        scan_id=scan_id,
        n_signals_in_scanner=len(signals),
        triggered=True,
        decision=d,
        decider_result=decider_result,
        verdict=verdict,
        signal_id=signal_id,
        telegram_sent=telegram_sent,
    )
