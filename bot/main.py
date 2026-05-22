"""
Entry point — run with:
    python -m bot.main

What it does
------------
1. Configure logging (stderr + file)
2. Verify required secrets
3. Open SQLite DB (create + migrate if needed)
4. Build market data provider (Alpaca)
5. Build a portfolio stub (will be replaced by real Alpaca account query later)
6. Start the async scheduler
7. Block until SIGINT/SIGTERM

On Ubuntu, run as a systemd service — see deploy/btc-signal-bot.service.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from bot.config import settings


def _setup_logging() -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stderr),
        RotatingFileHandler(
            settings.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8",
        ),
    ]
    logging.basicConfig(
        level=settings.log_level,
        format=fmt,
        handlers=handlers,
        force=True,
    )
    # Quiet down noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)


def _ensure_secrets() -> int:
    """Return 0 if OK, non-zero if missing required secrets."""
    missing_data = settings.required_for_data()
    if missing_data:
        print(f"X missing data env vars: {missing_data}", file=sys.stderr)
        return 1
    if not settings.anthropic_api_key:
        print("X ANTHROPIC_API_KEY missing", file=sys.stderr)
        return 1
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        # Warn but don't block — bot can run in shadow mode without Telegram
        print("! Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).",
              file=sys.stderr)
        print("  Signals will be logged to DB but not sent.", file=sys.stderr)
    return 0


async def _amain() -> int:
    log = logging.getLogger(__name__)

    from bot.data.market import default_provider
    from bot.scheduler import run_scheduler
    from bot.storage import db as storage_db
    from bot.storage.models import PortfolioState

    log.info("BTC AI Signal Bot starting (mode=%s, model=%s)",
             settings.mode.value, settings.anthropic_model)

    provider = default_provider()
    log.info("market data provider ready (Alpaca, paper=%s)", settings.alpaca_paper)

    conn = storage_db.init_db(settings.db_path)
    log.info("DB ready at %s (schema_version=%d)",
             settings.db_path, storage_db.schema_version(conn))

    # Phase 1: hardcoded portfolio stub. Phase 2 will query the Alpaca account.
    portfolio = PortfolioState(
        equity_usd=10_000.0,
        open_positions=0,
        daily_pnl_pct=0.0,
        remaining_position_slots=settings.max_open_positions,
    )
    log.info("portfolio stub: equity=$%.2f, open=%d, daily_pnl=%+.2f%%",
             portfolio.equity_usd, portfolio.open_positions, portfolio.daily_pnl_pct * 100)

    await run_scheduler(provider=provider, conn=conn, portfolio=portfolio)
    return 0


def main() -> int:
    _setup_logging()
    rc = _ensure_secrets()
    if rc:
        return rc
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
