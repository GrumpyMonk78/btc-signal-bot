"""
Async scheduler — runs the pipeline once per H1 candle close.

Uses APScheduler's AsyncIOScheduler with a cron trigger. Crypto trades 24/7
so we don't gate on market hours; just run at every full hour + 30s buffer
(to give Alpaca's feed time to publish the closed bar).

Jobs:
  - main_loop:    HH:00:30 UTC, every hour — full pipeline pass
  - daily_summary: 00:05:00 UTC, every day — Telegram summary of yesterday
  - heartbeat:    on startup — Telegram "bot started" message

Graceful shutdown on SIGINT/SIGTERM.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import settings
from bot.data.market import MarketDataProvider
from bot.pipeline import PipelineResult, run_once
from bot.storage import db as storage_db
from bot.storage.models import PortfolioState

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Job wrappers — these are what the scheduler actually calls
# ─────────────────────────────────────────────────────────────────────────────


def _run_pipeline_job(
    *, provider: MarketDataProvider, conn: sqlite3.Connection,
    portfolio: PortfolioState,
) -> None:
    """Synchronous job — APScheduler runs it in a thread executor."""
    try:
        result = run_once(provider=provider, portfolio=portfolio, conn=conn)
        logger.info("pipeline: %s", result.summary_line())
    except Exception:
        logger.exception("pipeline: unhandled exception in run_once")


def _daily_summary_job(*, conn: sqlite3.Connection) -> None:
    """Send a brief summary of yesterday's activity to Telegram."""
    try:
        summary = storage_db.decisions_summary(conn)
        text = (
            f"Daily summary (UTC midnight)\n"
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


def _heartbeat() -> None:
    """One-shot startup notification."""
    try:
        from bot.notify import telegram as tg
        if tg.is_configured():
            tg._send_message(
                f"<b>BTC AI Signal Bot</b> started\n"
                f"instrument: {settings.instrument}\n"
                f"model: {settings.anthropic_model}\n"
                f"mode: {settings.mode.value}\n"
                f"started at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
            )
    except Exception:
        logger.exception("heartbeat: failed")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


async def run_scheduler(
    provider: MarketDataProvider,
    conn: sqlite3.Connection,
    portfolio: PortfolioState,
) -> None:
    """Start the scheduler and block until SIGINT/SIGTERM."""
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Main pipeline trigger: every hour at HH:00:30 UTC
    scheduler.add_job(
        _run_pipeline_job,
        CronTrigger(minute=0, second=30, timezone="UTC"),
        kwargs={"provider": provider, "conn": conn, "portfolio": portfolio},
        id="main_loop",
        name="pipeline.run_once (H1 close)",
        max_instances=1,    # don't overlap if one run takes >1h
        coalesce=True,      # missed runs collapse to one
    )

    # Daily summary at 00:05 UTC
    scheduler.add_job(
        _daily_summary_job,
        CronTrigger(hour=0, minute=5, second=0, timezone="UTC"),
        kwargs={"conn": conn},
        id="daily_summary",
        name="daily summary to Telegram",
    )

    # Startup heartbeat
    _heartbeat()
    scheduler.start()
    logger.info("scheduler started — main_loop fires every hour at HH:00:30 UTC")

    # Wait forever (or until cancelled)
    stop_event = asyncio.Event()

    def _stop(*_args):
        logger.info("scheduler: shutdown signal received")
        stop_event.set()

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler on the event loop
                signal.signal(sig, lambda *_: _stop())
    except Exception:
        logger.exception("scheduler: could not install signal handlers")

    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
