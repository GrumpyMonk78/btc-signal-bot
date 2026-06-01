"""
Position monitor — hourly time-exit enforcement.

Runs every hour (HH:05:00 UTC, 5 min after pipeline) and checks all
open positions on Alpaca against the signals table in the DB.

Time-exit rule (mirrors the rule written into Claude's invalidation field):
  If a position has been open >= TIME_EXIT_HOURS AND price has NOT moved
  at least PROGRESS_THRESHOLD of the distance toward TP, close at market.

Weekend / market-closed handling:
  - Crypto (BTC/USD): trades 24/7, monitor always runs.
  - Stocks (NVDA, TSLA, IONQ): US market open Mon-Fri 09:30-16:00 ET
    (14:30-21:00 UTC). If market is closed the monitor skips stock positions
    to avoid sending orders that would queue / execute at open unexpectedly.
    The 12h clock is paused during closed hours — it only counts market hours.

Public API
----------
    monitor_positions(conn) -> list[MonitorAction]
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from bot.config import get_instrument, settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tunable parameters
# ─────────────────────────────────────────────────────────────────────────────

TIME_EXIT_HOURS = 12          # close if no progress after this many hours
PROGRESS_THRESHOLD = 0.30     # 30% of TP distance must be covered
MARKET_OPEN_UTC_HOUR = 14     # US stocks open ~14:30 UTC
MARKET_CLOSE_UTC_HOUR = 21    # US stocks close ~21:00 UTC


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MonitorAction:
    symbol: str
    signal_id: str
    action: Literal["time_exit", "skipped_market_closed", "skipped_progress_ok", "error"]
    reason: str
    entry_price: float = 0.0
    current_price: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    hours_open: float = 0.0
    progress_pct: float = 0.0

    def summary(self) -> str:
        return (
            f"[{self.symbol}] {self.action}: {self.reason} "
            f"(open {self.hours_open:.1f}h, progress {self.progress_pct:.0%})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Market-hours helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_us_market_open(now: datetime) -> bool:
    """True if US stock market is currently open (approximate, UTC-based).

    Ignores US holidays — good enough for a safety guard.
    """
    # Weekends
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    # Approximate market hours: 14:30–21:00 UTC
    market_open = now.replace(hour=MARKET_OPEN_UTC_HOUR, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE_UTC_HOUR, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def _is_crypto(symbol: str) -> bool:
    inst = get_instrument(symbol)
    return inst is not None and inst.kind == "crypto"


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_signal_info(conn: sqlite3.Connection, symbol: str) -> list[dict]:
    """Return open signals for this symbol (most recent first)."""
    rows = conn.execute(
        """
        SELECT signal_id, instrument, direction, entry_price, stop_loss,
               take_profit, ts_utc, order_id
        FROM signals
        WHERE instrument = ?
        ORDER BY ts_utc DESC
        LIMIT 5
        """,
        (symbol,),
    ).fetchall()
    return [dict(r) for r in rows] if rows else []


# ─────────────────────────────────────────────────────────────────────────────
# Progress calculation
# ─────────────────────────────────────────────────────────────────────────────

def _progress_toward_tp(
    direction: str,
    entry: float,
    current: float,
    tp: float,
) -> float:
    """How far has price moved toward TP as a fraction of total TP distance.

    Returns value in [0, 1+] — 0 = no progress, 1 = at TP, >1 = past TP.
    Negative = moved away from TP (adverse).
    """
    tp_dist = abs(tp - entry)
    if tp_dist == 0:
        return 1.0
    if direction == "long":
        moved = current - entry
    else:  # short
        moved = entry - current
    return moved / tp_dist


# ─────────────────────────────────────────────────────────────────────────────
# Core monitor logic
# ─────────────────────────────────────────────────────────────────────────────

def monitor_positions(
    conn: sqlite3.Connection,
    now: datetime | None = None,
) -> list[MonitorAction]:
    """Check all open Alpaca positions and apply time-exit rule if needed.

    Parameters
    ----------
    conn
        SQLite connection — used to look up signal metadata (entry time, SL, TP).
    now
        Reference time (UTC). Defaults to datetime.now(UTC).

    Returns
    -------
    List of MonitorAction describing what was done for each position.
    """
    now = now or datetime.now(timezone.utc)
    actions: list[MonitorAction] = []

    # Import here to avoid circular imports at module load
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    except ImportError:
        logger.error("position_monitor: alpaca-py not installed")
        return actions

    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        logger.warning("position_monitor: Alpaca keys not configured — skipping")
        return actions

    # Shadow mode: log only, no real orders
    shadow = settings.mode.value == "shadow"

    try:
        tc = TradingClient(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            paper=settings.alpaca_paper,
        )
        positions = tc.get_all_positions()
    except Exception:
        logger.exception("position_monitor: failed to fetch positions from Alpaca")
        return actions

    if not positions:
        logger.info("position_monitor: no open positions")
        return actions

    logger.info("position_monitor: checking %d open position(s)", len(positions))

    for pos in positions:
        symbol = pos.symbol
        current_price = float(pos.current_price)
        direction = "long" if float(pos.qty) > 0 else "short"

        # Market-hours guard for stocks
        if not _is_crypto(symbol) and not _is_us_market_open(now):
            msg = f"market closed (weekday={now.weekday()}, hour={now.hour}UTC)"
            logger.info("position_monitor [%s]: skipped — %s", symbol, msg)
            actions.append(MonitorAction(
                symbol=symbol, signal_id="", action="skipped_market_closed",
                reason=msg, current_price=current_price,
            ))
            continue

        # Look up signal metadata from DB
        signal_rows = _fetch_signal_info(conn, symbol)
        if not signal_rows:
            logger.warning("position_monitor [%s]: no signal found in DB — skipping", symbol)
            actions.append(MonitorAction(
                symbol=symbol, signal_id="", action="error",
                reason="no signal record in DB", current_price=current_price,
            ))
            continue

        # Use most recent signal
        sig = signal_rows[0]
        signal_id = sig["signal_id"]
        entry_price = float(sig["entry_price"])
        sl = float(sig["stop_loss"])
        tp = float(sig["take_profit"])
        sig_direction = sig["direction"] or direction

        # Parse signal timestamp
        try:
            ts_str = sig["ts_utc"]
            # Handle both ISO formats
            opened_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
        except Exception as e:
            logger.warning("position_monitor [%s]: bad ts_utc %r — %s", symbol, sig["ts_utc"], e)
            actions.append(MonitorAction(
                symbol=symbol, signal_id=signal_id, action="error",
                reason=f"bad timestamp: {e}", current_price=current_price,
            ))
            continue

        hours_open = (now - opened_at).total_seconds() / 3600
        progress = _progress_toward_tp(sig_direction, entry_price, current_price, tp)

        logger.info(
            "position_monitor [%s]: open %.1fh, entry=%.4f current=%.4f tp=%.4f "
            "progress=%.0f%% (need %.0f%%)",
            symbol, hours_open, entry_price, current_price, tp,
            progress * 100, PROGRESS_THRESHOLD * 100,
        )

        # Check time-exit condition
        if hours_open < TIME_EXIT_HOURS:
            actions.append(MonitorAction(
                symbol=symbol, signal_id=signal_id, action="skipped_progress_ok",
                reason=f"only {hours_open:.1f}h open (< {TIME_EXIT_HOURS}h)",
                entry_price=entry_price, current_price=current_price, tp=tp, sl=sl,
                hours_open=hours_open, progress_pct=progress,
            ))
            continue

        if progress >= PROGRESS_THRESHOLD:
            actions.append(MonitorAction(
                symbol=symbol, signal_id=signal_id, action="skipped_progress_ok",
                reason=f"progress {progress:.0%} >= threshold {PROGRESS_THRESHOLD:.0%}",
                entry_price=entry_price, current_price=current_price, tp=tp, sl=sl,
                hours_open=hours_open, progress_pct=progress,
            ))
            continue

        # Time exit triggered — cancel bracket orders then market close
        reason = (
            f"time exit: {hours_open:.1f}h open, progress only {progress:.0%} "
            f"(< {PROGRESS_THRESHOLD:.0%} of TP distance)"
        )
        logger.warning("position_monitor [%s]: TIME EXIT — %s", symbol, reason)

        if not shadow:
            try:
                # Cancel all open orders for this symbol first (SL/TP bracket)
                tc.cancel_orders_for_symbol(symbol)
                logger.info("position_monitor [%s]: bracket orders cancelled", symbol)

                # Market close order
                close_side = OrderSide.SELL if sig_direction == "long" else OrderSide.BUY
                qty = abs(float(pos.qty))
                order = tc.submit_order(MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=close_side,
                    time_in_force=TimeInForce.DAY,
                ))
                logger.info(
                    "position_monitor [%s]: market close submitted — order_id=%s",
                    symbol, order.id,
                )
            except Exception:
                logger.exception("position_monitor [%s]: failed to submit close order", symbol)
                actions.append(MonitorAction(
                    symbol=symbol, signal_id=signal_id, action="error",
                    reason="close order submission failed",
                    entry_price=entry_price, current_price=current_price, tp=tp, sl=sl,
                    hours_open=hours_open, progress_pct=progress,
                ))
                continue
        else:
            logger.info("position_monitor [%s]: shadow mode — close order NOT sent", symbol)

        # Telegram notification
        try:
            from bot.notify import telegram as tg
            if tg.is_configured():
                pnl_pct = float(pos.unrealized_plpc) * 100
                pnl_usd = float(pos.unrealized_pl)
                tg._send_message(
                    f"<b>⏱ Time Exit</b> — {symbol}\n"
                    f"Pozice otevřena {hours_open:.1f}h bez dostatečného pohybu k TP.\n"
                    f"Pohyb k TP: {progress:.0%} (potřeba {PROGRESS_THRESHOLD:.0%})\n"
                    f"Entry: ${entry_price:.4f} | Current: ${current_price:.4f}\n"
                    f"PnL: {pnl_pct:+.2f}% ({pnl_usd:+.2f} USD)\n"
                    f"{'[SHADOW — order neposlán]' if shadow else 'Market close order odeslán.'}"
                )
        except Exception:
            logger.exception("position_monitor [%s]: telegram notify failed", symbol)

        actions.append(MonitorAction(
            symbol=symbol, signal_id=signal_id, action="time_exit",
            reason=reason,
            entry_price=entry_price, current_price=current_price, tp=tp, sl=sl,
            hours_open=hours_open, progress_pct=progress,
        ))

    return actions
