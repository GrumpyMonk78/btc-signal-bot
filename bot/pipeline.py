"""
Pipeline orchestrator — jeden plný pass: scan→Claude→risk→DB→Telegram.

Voláno schedulerem jednou za H1 candle close, pro každý aktivní instrument
zvlášť. Idempotentní: vše loguje do DB; scheduler rozhoduje kdy to spustit.

Hlavní změna oproti v1:
  run_once() nyní přijímá `instrument: InstrumentConfig` místo fixního
  settings.instrument. Díky tomu scheduler může volat run_once() ve smyčce
  přes všechny symboly ze seznamu INSTRUMENTS v config.py.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from bot.config import InstrumentConfig, get_instrument, settings
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
from bot.execution.alpaca import execute_signal, ExecutionResult
from bot.storage.models import (
    ApprovedSignal,
    Decision,
    PortfolioState,
    RiskVerdict,
)
from bot.strategy.scanner import ScannerSignal, scan as run_scanner

logger = logging.getLogger(__name__)

# Pouze signál na posledním zavřeném baru se považuje za "čerstvý"
_FRESH_WINDOW = timedelta(hours=1, minutes=5)


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    """Co se stalo v tomto passe — scheduler to použije pro logování."""

    instrument: str
    scan_id: int
    n_signals_in_scanner: int
    triggered: bool
    decision: Decision | None = None
    decider_result: DeciderResult | None = None
    verdict: RiskVerdict | None = None
    signal_id: str | None = None
    execution_result: ExecutionResult | None = None
    telegram_sent: bool = False
    error: str | None = None

    def summary_line(self) -> str:
        prefix = f"[{self.instrument}]"
        if self.error:
            return f"{prefix} ERROR: {self.error}"
        if not self.triggered:
            return f"{prefix} scan_id={self.scan_id}: no fresh trigger ({self.n_signals_in_scanner} historical)"
        if self.decision is None:
            return f"{prefix} scan_id={self.scan_id}: triggered but no decision"
        if self.verdict and self.verdict.approved:
            return (
                f"{prefix} scan_id={self.scan_id}: ENTER conf={self.decision.confidence} "
                f"→ signal {self.signal_id} (telegram_sent={self.telegram_sent})"
            )
        return (
            f"{prefix} scan_id={self.scan_id}: {self.decision.decision} "
            f"conf={self.decision.confidence} → vetoed: "
            f"{self.verdict.veto_codes if self.verdict else 'n/a'}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def run_once(
    *,
    provider: MarketDataProvider,
    portfolio: PortfolioState,
    conn: sqlite3.Connection,
    instrument: InstrumentConfig | None = None,
    bars_primary: int = 200,
    bars_context: int = 200,
    telegram_client: Any | None = None,
    claude_client: Any | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    """Jeden plný pipeline pass pro daný instrument.

    Parameters
    ----------
    provider
        MarketDataProvider odpovídající typu instrumentu (crypto/stock).
        Použij bot.data.market.provider_for(instrument) pro automatický výběr.
    portfolio
        Aktuální stav portfolia.
    conn
        SQLite spojení.
    instrument
        Který instrument zpracovat. Pokud None, použije se primární instrument
        ze settings (zpětná kompatibilita).
    bars_primary, bars_context
        Kolik barů stáhnout pro každý timeframe.
    telegram_client, claude_client
        Volitelné mock klienti pro testování.
    now
        Referenční čas (pro testování). Defaults to UTC now.
    """
    now = now or datetime.now(timezone.utc)

    # Resolve instrument — zpětná kompatibilita se starým kódem
    if instrument is None:
        instrument = get_instrument(settings.instrument)
        if instrument is None:
            # Fallback: vytvoř dočasný config z settings
            from bot.config import InstrumentConfig
            instrument = InstrumentConfig(
                symbol=settings.instrument,
                kind="crypto",
                timeframe_primary=settings.timeframe_primary,
                timeframe_context=settings.timeframe_context,
            )

    symbol = instrument.symbol
    tf_primary = instrument.timeframe_primary
    tf_context = instrument.timeframe_context

    # ── Stažení dat ───────────────────────────────────────────────────────
    try:
        primary = provider.fetch_bars(BarRequest(
            symbol=symbol,
            timeframe=tf_primary,
            limit=bars_primary,
        ))
        context_bars = provider.fetch_bars(BarRequest(
            symbol=symbol,
            timeframe=tf_context,
            limit=bars_context,
        ))
    except Exception as exc:
        logger.exception("pipeline [%s]: market data fetch failed", symbol)
        return PipelineResult(
            instrument=symbol, scan_id=-1, n_signals_in_scanner=0,
            triggered=False, error=f"market data fetch failed: {exc}",
        )

    # ── Scanner ───────────────────────────────────────────────────────────
    signals = run_scanner(primary, context_bars)
    last_signal = signals[-1] if signals else None
    latest_ts = last_signal.timestamp.to_pydatetime() if last_signal else None

    scan_id = storage_db.insert_scan(
        conn,
        instrument=symbol,
        bars_primary=len(primary),
        bars_context=len(context_bars),
        n_signals=len(signals),
        latest_filter=last_signal.filter if last_signal else None,
        latest_signal_ts=latest_ts,
        notes=f"live pipeline ({instrument.kind})",
    )

    triggered = (
        last_signal is not None
        and (now - latest_ts) < _FRESH_WINDOW
    )

    if not triggered:
        logger.info("pipeline [%s] scan_id=%d: no fresh trigger (%d historical)",
                    symbol, scan_id, len(signals))
        return PipelineResult(
            instrument=symbol, scan_id=scan_id,
            n_signals_in_scanner=len(signals), triggered=False,
        )

    # ── Sestavení kontextu pro Claude ──────────────────────────────────────
    ctx = assemble_context(
        instrument=symbol,
        primary_df=primary,
        context_df=context_bars,
        trigger=last_signal,
        portfolio=portfolio,
        as_of=now,
        max_primary_bars=30,
        max_context_bars=30,
    )
    user_text = render_context_for_prompt(ctx)

    # ── Volání Claude ──────────────────────────────────────────────────────
    try:
        decider_result = claude_decide(ctx, client=claude_client)
    except BudgetExceededError as exc:
        logger.warning("pipeline [%s] scan_id=%d: budget exceeded — %s", symbol, scan_id, exc)
        return PipelineResult(
            instrument=symbol, scan_id=scan_id, n_signals_in_scanner=len(signals),
            triggered=True, error=f"budget: {exc}",
        )
    except DeciderError as exc:
        logger.exception("pipeline [%s] scan_id=%d: Claude failed", symbol, scan_id)
        return PipelineResult(
            instrument=symbol, scan_id=scan_id, n_signals_in_scanner=len(signals),
            triggered=True, error=f"claude: {exc}",
        )

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
        instrument=symbol,
        trigger_filter=last_signal.filter,
        trigger_ts=last_signal.timestamp.to_pydatetime(),
        trigger_price=float(last_signal.price),
        decision=d,
    )

    # ── Risk manager ───────────────────────────────────────────────────────
    verdict = risk_evaluate(d, portfolio, last_signal, now=now)
    storage_db.insert_veto(conn, decision_id=decision_id, verdict=verdict)

    if not (verdict.approved and d.decision == "enter" and d.direction is not None):
        logger.info("pipeline [%s] scan_id=%d: not approved (%s)",
                    symbol, scan_id, verdict.veto_codes)
        return PipelineResult(
            instrument=symbol, scan_id=scan_id, n_signals_in_scanner=len(signals),
            triggered=True, decision=d, decider_result=decider_result, verdict=verdict,
        )

    # ── Approved → ulož signál + pošli Telegram ────────────────────────────
    # position_size_btc se používá pro naming ale pro akcie je to de facto
    # počet akcií v jednotkách dolaru / cena → počet kusů. Pojmenování
    # "position_btc" je legacy; v DB sloupci je to prostě "quantity".
    signal_id = storage_db.insert_signal(
        conn,
        decision_id=decision_id,
        instrument=symbol,
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

    # ── Execution (shadow=no-op, paper/live=real order) ───────────────────
    approved = ApprovedSignal(
        signal_id=signal_id,
        instrument=symbol,
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
    exec_result = execute_signal(approved)
    logger.info("pipeline [%s] scan_id=%d: %s", symbol, scan_id, exec_result.summary())

    # Uloz order_id do DB pokud byl order odoslan
    if exec_result.submitted and exec_result.order_id:
        conn.execute(
            "UPDATE signals SET order_id=? WHERE signal_id=?",
            (exec_result.order_id, signal_id),
        )

    telegram_sent = False
    try:
        from bot.notify import telegram as tg
        if tg.is_configured():
            telegram_sent = tg.send_signal(approved, d, client=telegram_client)
            if telegram_sent:
                conn.execute(
                    "UPDATE signals SET sent_to_telegram=1 WHERE signal_id=?",
                    (signal_id,),
                )
    except Exception:
        logger.exception(
            "pipeline [%s] scan_id=%d: telegram send failed (signal logged anyway)",
            symbol, scan_id,
        )

    return PipelineResult(
        instrument=symbol,
        scan_id=scan_id,
        n_signals_in_scanner=len(signals),
        triggered=True,
        decision=d,
        decider_result=decider_result,
        verdict=verdict,
        signal_id=signal_id,
        execution_result=exec_result,
        telegram_sent=telegram_sent,
    )
