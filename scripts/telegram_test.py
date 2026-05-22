"""
Telegram bot smoke test.

Two modes:

    python -m scripts.telegram_test
        Sends a single "hello" message to the configured chat_id.

    python -m scripts.telegram_test --discover-chat-id
        Lists chat_ids the bot has recently received messages from. Useful
        right after you create the bot — send any message to it, then run
        this to find your chat_id, then paste it into .env.

Exit codes
----------
    0 — message sent / chat_id discovered
    1 — missing TELEGRAM_BOT_TOKEN (or chat_id for the send path)
    2 — API call failed
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone

from bot.config import settings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--discover-chat-id", action="store_true",
        help="List chat_ids the bot has recently received messages from",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("-" * 70)
    print("  Telegram smoke test")
    print(f"  token configured: {'yes' if settings.telegram_bot_token else 'NO'}")
    print(f"  chat_id configured: {'yes' if settings.telegram_chat_id else 'NO'}")
    print("-" * 70)

    if not settings.telegram_bot_token:
        print("\n  X TELEGRAM_BOT_TOKEN missing in .env")
        print("    Create a bot:")
        print("      1. Open https://t.me/BotFather")
        print("      2. Send /newbot, choose a name (anything) and username (must end in 'bot')")
        print("      3. BotFather returns a token like '123456:ABC-DEF...' — paste it into .env")
        return 1

    try:
        from bot.notify import telegram as tg
    except Exception as exc:
        print(f"\n  X import failed: {exc}")
        traceback.print_exc()
        return 2

    if args.discover_chat_id:
        try:
            chats = tg.discover_chat_id()
        except Exception as exc:
            print(f"\n  X getUpdates failed: {exc}")
            return 2
        if not chats:
            print("\n  (no recent messages — open your bot in Telegram, send any message, then re-run)")
            return 0
        print()
        print("  Recently active chats:")
        for c in chats:
            print(f"    chat_id={c['chat_id']:<15}  type={c['type']:<10}  "
                  f"title={c['title']!r}  last={c['last_message']!r}")
        print()
        print("  → Copy the chat_id you want into .env as TELEGRAM_CHAT_ID")
        return 0

    # Default: send a hello message
    if not settings.telegram_chat_id:
        print("\n  X TELEGRAM_CHAT_ID missing in .env")
        print("    Run with --discover-chat-id first to find it.")
        return 1

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = (
        "<b>BTC AI Signal Bot</b>\n"
        f"Telegram smoke test @ {now}\n"
        f"model: {settings.anthropic_model}\n"
        f"mode: {settings.mode.value}\n"
        "If you see this, your bot is wired correctly."
    )
    ok = tg._send_message(text)
    if ok:
        print("\n  ✓ message sent — check your Telegram chat")
        return 0
    print("\n  X message send failed — check token / chat_id and logs above")
    return 2


if __name__ == "__main__":
    sys.exit(main())
