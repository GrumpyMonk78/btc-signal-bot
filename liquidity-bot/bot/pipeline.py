"""
pipeline.py — Main scan-and-execute pipeline for Liquidity Sweep Bot

For each instrument:
1. Fetch bars (1H trend + 15min entry)
2. Run scanner
3. If signal found → place bracket order on Alpaca
4. Notify on Telegram
"""

import logging
from bot.config import INSTRUMENTS, TREND_TIMEFRAME, ENTRY_TIMEFRAME, LOOKBACK_BARS
from bot.strategy.scanner import scan
from bot.execution.alpaca_client import fetch_bars, place_bracket_order, get_account_info
from bot.notification.telegram_notify import (
    notify_signal,
    notify_order_placed,
    notify_order_failed,
    notify_error,
)

logger = logging.getLogger(__name__)


def run_pipeline() -> None:
    """Execute one full scan cycle across all instruments."""
    logger.info("═" * 50)
    logger.info("Pipeline cycle started")

    account = get_account_info()
    if account:
        logger.info(
            f"Account: equity={account.get('equity', 0):.2f} "
            f"cash={account.get('cash', 0):.2f}"
        )

    for instrument in INSTRUMENTS:
        symbol      = instrument["symbol"]
        asset_class = instrument["asset_class"]
        qty         = instrument["qty"]

        logger.info(f"── Scanning {symbol} ({asset_class}) ──")

        try:
            # ── Fetch data ─────────────────────────────────────────────────────
            df_1h  = fetch_bars(symbol, asset_class, TREND_TIMEFRAME,  n_bars=LOOKBACK_BARS)
            df_15m = fetch_bars(symbol, asset_class, ENTRY_TIMEFRAME,  n_bars=LOOKBACK_BARS)

            if df_1h.empty or df_15m.empty:
                logger.warning(f"{symbol}: empty bar data, skipping")
                continue

            # ── Run scanner ────────────────────────────────────────────────────
            signal = scan(symbol, df_1h, df_15m)

            if signal is None:
                logger.info(f"{symbol}: no signal")
                continue

            # ── Notify on Telegram first ───────────────────────────────────────
            notify_signal(signal)

            # ── Place order on Alpaca ──────────────────────────────────────────
            result = place_bracket_order(
                symbol=symbol,
                asset_class=asset_class,
                direction=signal.direction,
                qty=qty,
                take_profit=signal.take_profit,
                stop_loss=signal.stop_loss,
            )

            if result and result.status != "error":
                notify_order_placed(
                    symbol=symbol,
                    direction=signal.direction,
                    entry=signal.entry,
                    sl=signal.stop_loss,
                    tp=signal.take_profit,
                    order_id=result.order_id,
                )
            elif result:
                notify_order_failed(symbol, result.message)

        except Exception as e:
            logger.error(f"Pipeline error for {symbol}: {e}", exc_info=True)
            notify_error(f"pipeline:{symbol}", str(e))

    logger.info("Pipeline cycle finished")
    logger.info("═" * 50)
