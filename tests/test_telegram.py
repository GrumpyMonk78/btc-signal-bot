"""
Tests for the Telegram notifier. We mock httpx.Client so no real network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from bot.notify import telegram as tg
from bot.storage.models import ApprovedSignal, Decision, DecisionDirection


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _signal(**over) -> ApprovedSignal:
    base = dict(
        signal_id="abc-123",
        instrument="BTC/USD",
        direction=DecisionDirection.LONG,
        entry_price=70_000.0,
        stop_loss=69_300.0,
        take_profit=71_500.0,
        position_size_usd=10_000.0,
        position_size_btc=0.142857,
        confidence=7,
        r_r_ratio=2.14,
        reasoning="strong setup with confirmed H4 uptrend and ETF inflows",
        key_risks=["FOMC at 18:00", "thin Asian liquidity"],
        invalidation="close below 69300 on H1",
        created_at=datetime(2026, 5, 21, 6, 0, tzinfo=timezone.utc),
    )
    base.update(over)
    return ApprovedSignal(**base)


def _decision() -> Decision:
    return Decision(
        decision="enter", direction="long",
        entry_price=70_000.0, stop_loss=69_300.0, take_profit=71_500.0,
        confidence=7, size_hint="normal", reasoning="x",
    )


def _ok_response() -> SimpleNamespace:
    resp = SimpleNamespace()
    resp.status_code = 200
    resp.json = lambda: {"ok": True, "result": {"message_id": 1}}
    resp.text = '{"ok": true}'
    return resp


def _err_response(code: int = 500, body: str = "Internal Server Error") -> SimpleNamespace:
    resp = SimpleNamespace()
    resp.status_code = code
    resp.json = lambda: {"ok": False, "error_code": code, "description": body}
    resp.text = body
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────


def test_format_signal_message_contains_key_fields():
    msg = tg.format_signal_message(_signal(), _decision())
    assert "LONG BTC/USD" in msg
    assert "70,000.00" in msg
    assert "69,300.00" in msg
    assert "71,500.00" in msg
    assert "Confidence:" in msg and "7/10" in msg
    assert "abc-123" in msg
    assert "FOMC" in msg


def test_format_signal_escapes_html():
    sig = _signal(reasoning="setup with <script>alert(1)</script> + special & chars")
    msg = tg.format_signal_message(sig, _decision())
    # Tags must be escaped — no raw <script> leaks
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
    # Ampersand must be escaped to &amp;
    assert "&amp;" in msg
    assert "special &amp; chars" in msg


def test_format_signal_truncates_overlong():
    # Reasoning is capped at 2000 chars by the Decision/ApprovedSignal models.
    # To exercise the renderer's own truncation, build a signal with the max
    # legal reasoning AND many long key_risks — the combined message exceeds 4000.
    sig = _signal(
        reasoning="x" * 1900,
        key_risks=["y" * 300 for _ in range(10)],
        invalidation="z" * 400,
    )
    msg = tg.format_signal_message(sig, _decision())
    assert len(msg) <= 4000
    assert "(truncated)" in msg


def test_format_error_message():
    msg = tg.format_error_message("something broke")
    assert "Bot error" in msg
    assert "something broke" in msg


# ─────────────────────────────────────────────────────────────────────────────
# _send_message
# ─────────────────────────────────────────────────────────────────────────────


def test_send_message_returns_true_on_200():
    client = MagicMock()
    client.post.return_value = _ok_response()
    ok = tg._send_message("hi", chat_id="123", token="abc:def", client=client)
    assert ok is True
    client.post.assert_called_once()
    # Verify the API URL and payload shape
    args, kwargs = client.post.call_args
    assert args[0].endswith("/sendMessage")
    assert kwargs["json"]["chat_id"] == "123"
    assert kwargs["json"]["text"] == "hi"


def test_send_message_returns_false_when_no_token():
    ok = tg._send_message("hi", chat_id="123", token="", client=MagicMock())
    assert ok is False


def test_send_message_returns_false_when_no_chat_id():
    ok = tg._send_message("hi", chat_id="", token="abc:def", client=MagicMock())
    assert ok is False


def test_send_message_retries_on_5xx_then_succeeds():
    client = MagicMock()
    client.post.side_effect = [_err_response(503), _ok_response()]
    ok = tg._send_message("hi", chat_id="123", token="abc:def", client=client)
    assert ok is True
    assert client.post.call_count == 2


def test_send_message_returns_false_after_all_retries_fail():
    client = MagicMock()
    client.post.return_value = _err_response(500)
    ok = tg._send_message("hi", chat_id="123", token="abc:def", client=client)
    assert ok is False
    assert client.post.call_count == 2


def test_send_message_handles_network_error_gracefully():
    client = MagicMock()
    client.post.side_effect = httpx.ConnectError("network down")
    ok = tg._send_message("hi", chat_id="123", token="abc:def", client=client)
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# send_signal / send_error
# ─────────────────────────────────────────────────────────────────────────────


def test_send_signal_posts_formatted_message(monkeypatch):
    # settings.telegram_* are empty in the test env — patch them so _send_message
    # actually performs the (mocked) HTTP call.
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "abc:def")
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123")

    client = MagicMock()
    client.post.return_value = _ok_response()
    ok = tg.send_signal(_signal(), _decision(), client=client)
    assert ok is True
    posted_text = client.post.call_args[1]["json"]["text"]
    assert "LONG BTC/USD" in posted_text


def test_send_error_posts_error_message(monkeypatch):
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "abc:def")
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123")

    client = MagicMock()
    client.post.return_value = _ok_response()
    ok = tg.send_error("disk full", client=client)
    assert ok is True
    posted = client.post.call_args[1]["json"]["text"]
    assert "Bot error" in posted
    assert "disk full" in posted


# ─────────────────────────────────────────────────────────────────────────────
# discover_chat_id
# ─────────────────────────────────────────────────────────────────────────────


def test_discover_chat_id_parses_updates():
    updates_payload = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {
                "message_id": 10,
                "chat": {"id": 12345, "type": "private", "first_name": "Joe", "last_name": "T"},
                "text": "hello bot"}},
            # duplicate chat → should be deduplicated
            {"update_id": 2, "message": {
                "message_id": 11,
                "chat": {"id": 12345, "type": "private", "first_name": "Joe"},
                "text": "again"}},
            # different chat
            {"update_id": 3, "message": {
                "message_id": 12,
                "chat": {"id": -987, "type": "group", "title": "Trading group"},
                "text": "yo"}},
        ],
    }
    client = MagicMock()
    resp = SimpleNamespace()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json = lambda: updates_payload
    client.get.return_value = resp

    chats = tg.discover_chat_id(token="abc:def", client=client)
    chat_ids = sorted([c["chat_id"] for c in chats])
    assert chat_ids == [-987, 12345]




def test_discover_chat_id_raises_without_token(monkeypatch):
    # Patch settings so the function genuinely has no token to fall back to.
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "")
    with pytest.raises(RuntimeError):
        tg.discover_chat_id(token="")


# ─────────────────────────────────────────────────────────────────────────────
# is_configured
# ─────────────────────────────────────────────────────────────────────────────


def test_is_configured_reflects_settings(monkeypatch):
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "")
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "")
    assert tg.is_configured() is False
    monkeypatch.setattr(tg.settings, "telegram_bot_token", "abc:def")
    monkeypatch.setattr(tg.settings, "telegram_chat_id", "123")
    assert tg.is_configured() is True
