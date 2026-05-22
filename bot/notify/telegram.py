"""
Telegram notifier — send approved signals to the user.

Uses the Telegram Bot HTTP API directly via httpx (no SDK wrapper).
The API is simple enough that a wrapper just adds dependencies.

Public API
----------
    send_signal(signal: ApprovedSignal, decision: Decision, *, client=None) -> bool
    send_error(text: str, *, client=None) -> bool
    discover_chat_id(*, client=None) -> list[dict]      # for setup
    format_signal_message(signal, decision) -> str       # for tests / preview

`send_*` return True on success, False on failure (best-effort — never raises
to caller; failures are logged but do not block the pipeline).

`client` parameter is for testing — pass a mock httpx.Client to avoid network.

Setup
-----
1. Open Telegram, search for @BotFather
2. /newbot, follow prompts, copy the token  → TELEGRAM_BOT_TOKEN in .env
3. Open chat with your new bot, send any message
4. Run:  python -m scripts.telegram_test --discover-chat-id
   It prints your chat_id. Copy to .env as TELEGRAM_CHAT_ID.
5. Run:  python -m scripts.telegram_test
   Sends a test message. If you see it on your phone, you're set.
"""
from __future__ import annotations

import html
import logging
from typing import Any

import httpx

from bot.config import settings
from bot.storage.models import ApprovedSignal, Decision

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_DEFAULT_TIMEOUT = 10.0


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────


def _esc(text: str) -> str:
    """Escape HTML special chars (Telegram parse_mode=HTML)."""
    return html.escape(text, quote=False)


def format_signal_message(signal: ApprovedSignal, decision: Decision) -> str:
    """Render the full Telegram message for an approved signal.

    Uses parse_mode=HTML. Keep under ~4000 chars (Telegram message limit).
    """
    direction_emoji = "🟢" if signal.direction.value == "long" else "🔴"
    arrow = "▲" if signal.direction.value == "long" else "▼"

    sl_dist = abs(signal.entry_price - signal.stop_loss)
    sl_pct = sl_dist / signal.entry_price * 100
    tp_dist = abs(signal.take_profit - signal.entry_price)
    tp_pct = tp_dist / signal.entry_price * 100

    lines = [
        f"{direction_emoji} <b>{signal.direction.value.upper()} {_esc(signal.instrument)}</b> @ {signal.entry_price:,.2f}",
        "",
        f"<b>SL:</b> {signal.stop_loss:,.2f}   ({arrow}{sl_dist:.2f}, {sl_pct:.2f}%)",
        f"<b>TP:</b> {signal.take_profit:,.2f}   ({arrow}{tp_dist:.2f}, {tp_pct:.2f}%)",
        f"<b>R:R:</b> 1 : {signal.r_r_ratio:.2f}",
        "",
        f"<b>Confidence:</b> {signal.confidence}/10",
        f"<b>Size:</b> ${signal.position_size_usd:,.0f} ({signal.position_size_btc:.6f} BTC)",
        "",
        "<b>Reasoning:</b>",
        _esc(signal.reasoning),
    ]

    if signal.key_risks:
        lines.append("")
        lines.append("<b>Key risks:</b>")
        for risk in signal.key_risks:
            lines.append(f"• {_esc(risk)}")

    if signal.invalidation:
        lines.append("")
        lines.append(f"<b>Invalidation:</b> {_esc(signal.invalidation)}")

    lines.append("")
    lines.append(f"<code>setup_id: {signal.signal_id}</code>")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        # Telegram hard limit is 4096; we cap at 4000 to leave margin
        msg = msg[:3950] + "\n\n... (truncated)"
    return msg


def format_error_message(text: str) -> str:
    """Render a short ops-style error message."""
    return f"⚠️ <b>Bot error</b>\n<pre>{_esc(text[:1500])}</pre>"


# ─────────────────────────────────────────────────────────────────────────────
# Send (sync; uses httpx.Client)
# ─────────────────────────────────────────────────────────────────────────────


def _api_url(method: str, token: str) -> str:
    return _API_BASE.format(token=token, method=method)


def _send_message(
    text: str,
    *,
    chat_id: str | None = None,
    token: str | None = None,
    client: httpx.Client | None = None,
    parse_mode: str = "HTML",
) -> bool:
    """Low-level send. Returns True on success, False on any failure."""
    token = token or settings.telegram_bot_token
    chat_id = chat_id or settings.telegram_chat_id
    if not token or not chat_id:
        logger.warning("telegram._send_message: token or chat_id missing — message dropped")
        return False

    url = _api_url("sendMessage", token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    owned = False
    if client is None:
        client = httpx.Client(timeout=_DEFAULT_TIMEOUT)
        owned = True
    try:
        for attempt in (1, 2):
            try:
                resp = client.post(url, json=payload)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return True
                logger.warning(
                    "telegram._send_message: HTTP %d on attempt %d: %s",
                    resp.status_code, attempt, resp.text[:200],
                )
            except httpx.HTTPError as exc:
                logger.warning("telegram._send_message: %s on attempt %d", exc, attempt)
            if attempt == 1:
                import time
                time.sleep(1.0)
        return False
    finally:
        if owned:
            client.close()


def send_signal(signal: ApprovedSignal, decision: Decision, *,
                client: httpx.Client | None = None) -> bool:
    """Send an approved signal to the configured chat."""
    text = format_signal_message(signal, decision)
    return _send_message(text, client=client)


def send_error(text: str, *, client: httpx.Client | None = None) -> bool:
    """Send a short error notification."""
    return _send_message(format_error_message(text), client=client)


# ─────────────────────────────────────────────────────────────────────────────
# Setup utilities
# ─────────────────────────────────────────────────────────────────────────────


def discover_chat_id(*, token: str | None = None,
                     client: httpx.Client | None = None) -> list[dict[str, Any]]:
    """Return list of chat_ids the bot has recently received messages from.

    Telegram's getUpdates returns recent updates. We extract unique chat_ids
    and meta (chat title, username) so the user can pick the right one.

    Send any message to your bot first, then call this — otherwise empty.
    """
    token = token or settings.telegram_bot_token
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

    url = _api_url("getUpdates", token)
    owned = False
    if client is None:
        client = httpx.Client(timeout=_DEFAULT_TIMEOUT)
        owned = True
    try:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates returned not-ok: {data}")
        updates = data.get("result", [])
        seen: dict[int, dict[str, Any]] = {}
        for u in updates:
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            if chat_id is None or chat_id in seen:
                continue
            seen[chat_id] = {
                "chat_id": chat_id,
                "type": chat.get("type"),
                "title": chat.get("title") or chat.get("username")
                         or f"{chat.get('first_name','')} {chat.get('last_name','')}".strip(),
                "last_message": (msg.get("text") or "")[:80],
            }
        return list(seen.values())
    finally:
        if owned:
            client.close()


def is_configured() -> bool:
    """True if both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    return bool(settings.telegram_bot_token) and bool(settings.telegram_chat_id)
