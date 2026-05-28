"""
telegram_notify.py — Telegram notifications for Liquidity Sweep Bot
"""

import logging
import requests
from datetime import datetime, timezone

from bot.config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from bot.strategy.scanner import Signal

logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def _send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured (missing TOKEN or CHAT_ID)")
        return False
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def notify_signal(signal: Signal) -> bool:
    """Send trading signal notification."""
    direction_emoji = "🟢 LONG" if signal.direction == "long" else "🔴 SHORT"
    conf_bar = "█" * int(signal.confidence * 10) + "░" * (10 - int(signal.confidence * 10))

    msg = (
        f"⚡ <b>LIQUIDITY SWEEP SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{signal.symbol}</b> — {direction_emoji}\n\n"
        f"🎯 Entry:       <code>{signal.entry:.4f}</code>\n"
        f"🛑 Stop Loss:   <code>{signal.stop_loss:.4f}</code>\n"
        f"✅ Take Profit: <code>{signal.take_profit:.4f}</code>\n"
        f"📐 R:R ratio:   <b>{signal.risk_reward:.2f}</b>\n\n"
        f"💡 Confidence:  {conf_bar} {signal.confidence:.0%}\n"
        f"📏 Sweep size:  {signal.sweep_candle_size:.4f} ({signal.sweep_candle_size/signal.avg_atr:.1f}× ATR)\n\n"
        f"📝 {signal.reasoning}\n\n"
        f"🕐 {signal.timestamp.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Paper trading signal — verify before live execution</i>"
    )
    return _send(msg)


def notify_order_placed(symbol: str, direction: str, entry: float,
                        sl: float, tp: float, order_id: str) -> bool:
    """Confirm that an order was actually placed on Alpaca."""
    emoji = "🟢" if direction == "long" else "🔴"
    msg = (
        f"{emoji} <b>ORDER PLACED</b> — {symbol}\n"
        f"Direction: {direction.upper()}\n"
        f"Entry: <code>{entry:.4f}</code> | SL: <code>{sl:.4f}</code> | TP: <code>{tp:.4f}</code>\n"
        f"Order ID: <code>{order_id}</code>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    return _send(msg)


def notify_order_failed(symbol: str, reason: str) -> bool:
    """Notify if order placement failed."""
    msg = (
        f"❌ <b>ORDER FAILED</b> — {symbol}\n"
        f"Reason: {reason}"
    )
    return _send(msg)


def notify_bot_started() -> bool:
    """Send startup message."""
    msg = (
        f"🤖 <b>Liquidity Sweep Bot started</b>\n"
        f"Mode: Paper Trading\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    return _send(msg)


def notify_error(context: str, error: str) -> bool:
    """Send error notification."""
    msg = (
        f"⚠️ <b>BOT ERROR</b>\n"
        f"Context: {context}\n"
        f"Error: <code>{error}</code>\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    return _send(msg)
