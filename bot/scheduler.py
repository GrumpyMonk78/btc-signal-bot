"""
Async scheduler — spouští pipeline pro každý aktivní instrument.

Jobs:
  - main_loop:      HH:00:30 UTC, každou hodinu
                    → iteruje přes všechny enabled instrumenty v INSTRUMENTS
  - daily_summary:  00:05:00 UTC každý den → Telegram souhrn
  - heartbeat:      jednorázově při startu → "bot started" zpráva

Přidání/odebrání instrumentu: edituj seznam INSTRUMENTS v bot/config.py.
Scheduler sám o sobě nevyžaduje žádnou změnu.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import get_enabled_instruments, settings
from bot.data.market import MarketDataProvider, provider_for
from bot.execution.portfolio import fetch_portfolio_state
from bot.pipeline import PipelineResult, run_once
from bot.storage import db as storage_db

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Job: hlavní smyčka přes všechny instrumenty
# ─────────────────────────────────────────────────────────────────────────────

def _run_all_instruments_job(
    *,
    conn: sqlite3.Connection,
) -> None:
    """Synchronní job — APScheduler ho spustí v thread executoru.

    Pro každý aktivní instrument:
      1. Fetchne aktualni stav portfolia z Alpaca (nebo stub v shadow rezimu)
      2. Vybere správný provider (crypto vs. stock)
      3. Spustí run_once()
      4. Zaloguje výsledek

    Chyba jednoho instrumentu neblokuje ostatní.
    """
    # Portfolio se fetchuje na zacatku kazdeho kola — vzdycky aktualni stav
    portfolio = fetch_portfolio_state()
    logger.info(
        "scheduler: portfolio equity=$%.2f open=%d daily_pnl=%+.2f%%",
        portfolio.equity_usd, portfolio.open_positions, portfolio.daily_pnl_pct * 100,
    )

    instruments = get_enabled_instruments()
    if not instruments:
        logger.warning("scheduler: žádné aktivní instrumenty v INSTRUMENTS seznamu")
        return

    logger.info("scheduler: spouštím pipeline pro %d instrumentů: %s",
                len(instruments), [i.symbol for i in instruments])

    results: list[PipelineResult] = []
    for inst in instruments:
        try:
            prov = provider_for(inst)
            result = run_once(
                provider=prov,
                portfolio=portfolio,
                conn=conn,
                instrument=inst,
            )
            results.append(result)
            logger.info("pipeline: %s", result.summary_line())
        except Exception:
            logger.exception(
                "scheduler: unhandled exception pro instrument %s", inst.symbol
            )

    # Stručný souhrn kola
    n_triggered = sum(1 for r in results if r.triggered)
    n_approved = sum(1 for r in results if r.verdict and r.verdict.approved)
    logger.info(
        "scheduler: kolo hotové — %d/%d instrumentů s triggerem, %d schválených signálů",
        n_triggered, len(results), n_approved,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Job: denní souhrn
# ─────────────────────────────────────────────────────────────────────────────

def _daily_summary_job(*, conn: sqlite3.Connection) -> None:
    """Pošle stručný přehled včerejší aktivity na Telegram."""
    try:
        summary = storage_db.decisions_summary(conn)
        instruments = get_enabled_instruments()
        instrument_list = ", ".join(i.symbol for i in instruments)
        text = (
            f"<b>Denní souhrn</b> (UTC midnight)\n"
            f"Instrumenty: {instrument_list}\n"
            f"  claude_calls={summary['claude_calls']}\n"
            f"  decisions={summary['decisions']} (enter={summary['enters']})\n"
            f"  approved_signals={summary['approved_signals']}\n"
            f"  tokens={summary['tokens_in_total']}+{summary['tokens_out_total']}"
        )
        from bot.notify import telegram as tg
        if tg.is_configured():
            tg._send_message(text)
        else:
            logger.info("daily_summary (telegram not configured):\n%s", text)
    except Exception:
        logger.exception("daily_summary: failed")


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat při startu
# ─────────────────────────────────────────────────────────────────────────────

def _heartbeat() -> None:
    """Jednorázová zpráva při spuštění bota."""
    try:
        instruments = get_enabled_instruments()
        instrument_list = "\n".join(
            f"  • {i.symbol} ({i.kind}, {i.timeframe_primary}/{i.timeframe_context})"
            for i in instruments
        )
        from bot.notify import telegram as tg
        if tg.is_configured():
            tg._send_message(
                f"<b>AI Trading Bot</b> spuštěn\n"
                f"model: {settings.anthropic_model}\n"
                f"mode: {settings.mode.value}\n"
                f"instrumenty ({len(instruments)}):\n{instrument_list}\n"
                f"čas: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
            )
        else:
            logger.info(
                "heartbeat: bot spuštěn, %d instrumentů: %s",
                len(instruments), [i.symbol for i in instruments],
            )
    except Exception:
        logger.exception("heartbeat: failed")


# ─────────────────────────────────────────────────────────────────────────────
# Veřejný vstupní bod
# ─────────────────────────────────────────────────────────────────────────────

async def run_scheduler(
    conn: sqlite3.Connection,
    # provider zachovan pro zpetnou kompatibilitu, ignorovan
    provider: MarketDataProvider | None = None,
) -> None:
    """Spustí scheduler a blokuje dokud nepřijde SIGINT/SIGTERM."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Hlavní loop: každou hodinu v HH:00:30 UTC
    scheduler.add_job(
        _run_all_instruments_job,
        CronTrigger(minute=0, second=30, timezone="UTC"),
        kwargs={"conn": conn},
        id="main_loop",
        name="pipeline — všechny instrumenty (H1 close)",
        max_instances=1,   # nepřekrývat pokud trvá >1h
        coalesce=True,     # zmeškaná volání = jedno
    )

    # Denní souhrn
    scheduler.add_job(
        _daily_summary_job,
        CronTrigger(hour=0, minute=5, second=0, timezone="UTC"),
        kwargs={"conn": conn},
        id="daily_summary",
        name="daily summary to Telegram",
    )

    _heartbeat()
    scheduler.start()

    instruments = get_enabled_instruments()
    logger.info(
        "scheduler spuštěn — main_loop každou hodinu v HH:00:30 UTC, "
        "%d aktivních instrumentů: %s",
        len(instruments), [i.symbol for i in instruments],
    )

    # Čekej na shutdown
    stop_event = asyncio.Event()

    def _stop(*_args):
        logger.info("scheduler: shutdown signal přijat")
        stop_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                signal.signal(sig, lambda *_: _stop())
    except Exception:
        logger.exception("scheduler: nepodařilo se nainstalovat signal handlers")

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        logger.info("scheduler zastaven")
