"""
main.py — Liquidity Sweep Bot entry point

Usage:
    python main.py              # run bot continuously (scheduler)
    python main.py --once       # run one scan cycle and exit
    python main.py --backtest   # run scanner replay on BTC historical data
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import SCAN_INTERVAL_MINUTES, LOG_LEVEL, LOG_FILE
from bot.pipeline import run_pipeline
from bot.notification.telegram_notify import notify_bot_started

# ── Logging setup ──────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run_once() -> None:
    """Single scan cycle — useful for testing."""
    logger.info("Running single scan cycle...")
    run_pipeline()
    logger.info("Done.")


def run_scheduler() -> None:
    """Start continuous scheduler."""
    logger.info(
        f"Starting Liquidity Sweep Bot "
        f"(scan every {SCAN_INTERVAL_MINUTES} min) "
        f"— {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    notify_bot_started()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_pipeline,
        trigger=IntervalTrigger(minutes=SCAN_INTERVAL_MINUTES),
        id="liquidity_scan",
        name="Liquidity sweep scanner",
        replace_existing=True,
    )

    # Run immediately on start, then on schedule
    run_pipeline()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user")
        scheduler.shutdown()


def run_backtest() -> None:
    """Quick scanner replay on BTC/USD historical data."""
    from bot.execution.alpaca_client import fetch_bars
    from bot.strategy.scanner import replay_scanner

    logger.info("Fetching historical data for backtest...")
    df_1h  = fetch_bars("BTC/USD", "crypto", "1Hour",  n_bars=500)
    df_15m = fetch_bars("BTC/USD", "crypto", "15Min",  n_bars=500)

    if df_1h.empty or df_15m.empty:
        logger.error("No data fetched — check Alpaca credentials in .env")
        return

    signals = replay_scanner(df_1h, df_15m, symbol="BTC/USD")

    logger.info(f"\n{'═'*50}")
    logger.info(f"Backtest complete — {len(signals)} signals found")
    for i, sig in enumerate(signals[-10:], 1):  # show last 10
        logger.info(
            f"  [{i}] {sig.timestamp.strftime('%Y-%m-%d %H:%M')} "
            f"{sig.direction.upper()} | RR={sig.risk_reward:.2f} | "
            f"Conf={sig.confidence:.0%}"
        )
    logger.info(f"{'═'*50}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Liquidity Sweep Bot")
    parser.add_argument("--once",      action="store_true", help="Run one scan and exit")
    parser.add_argument("--backtest",  action="store_true", help="Run scanner backtest on BTC")
    args = parser.parse_args()

    if args.once:
        run_once()
    elif args.backtest:
        run_backtest()
    else:
        run_scheduler()
