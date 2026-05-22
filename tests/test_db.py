"""
Tests for the SQLite storage layer.

Uses :memory: DB — fast, no file I/O, fully isolated per test.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bot.storage import db
from bot.storage.models import Decision, RiskVerdict


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def conn():
    c = db.init_memory_db()
    yield c
    c.close()


def _valid_decision_enter() -> Decision:
    return Decision(
        decision="enter",
        direction="long",
        entry_price=70_000.0,
        stop_loss=69_300.0,
        take_profit=71_500.0,
        confidence=7,
        size_hint="normal",
        reasoning="good setup",
        key_risks=["fed risk", "thin liquidity"],
        invalidation="below 69300",
    )


def _valid_decision_skip() -> Decision:
    return Decision(
        decision="skip",
        confidence=4,
        size_hint="skip",
        reasoning="meh",
    )


def _verdict_approved() -> RiskVerdict:
    return RiskVerdict(
        approved=True,
        reason="ok",
        veto_codes=[],
        position_size_usd=10_000.0,
        position_size_btc=0.142857,
        r_r_ratio=2.14,
    )


def _verdict_vetoed() -> RiskVerdict:
    return RiskVerdict(
        approved=False,
        reason="low confidence",
        veto_codes=["low_confidence"],
        position_size_usd=0,
        position_size_btc=0,
        r_r_ratio=0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schema + migration
# ─────────────────────────────────────────────────────────────────────────────


def test_schema_version_recorded(conn):
    assert db.schema_version(conn) == db.SCHEMA_VERSION


def test_schema_creates_all_tables(conn):
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    expected = {"schema_version", "scans", "claude_calls", "decisions",
                "veto_log", "signals", "outcomes"}
    assert expected.issubset(tables)


def test_migrate_is_idempotent(conn):
    # Re-run migrations on existing DB — should not duplicate schema_version row
    db._migrate(conn)
    rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
    assert rows["n"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# Inserts
# ─────────────────────────────────────────────────────────────────────────────


def test_insert_scan(conn):
    scan_id = db.insert_scan(
        conn,
        instrument="BTC/USD",
        bars_primary=200,
        bars_context=200,
        n_signals=3,
        latest_filter="ema_pullback",
        latest_signal_ts=datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc),
        notes="test",
    )
    assert scan_id > 0
    row = conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    assert row["instrument"] == "BTC/USD"
    assert row["n_signals"] == 3
    assert row["latest_filter"] == "ema_pullback"


def test_full_chain_scan_call_decision_veto_signal(conn):
    """End-to-end: scan → claude_call → decision → veto (approved) → signal."""
    scan_id = db.insert_scan(
        conn, instrument="BTC/USD", bars_primary=200, bars_context=200,
        n_signals=1, latest_filter="breakout_atr",
        latest_signal_ts=datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc),
    )
    call_id = db.insert_claude_call(
        conn, scan_id=scan_id, model="claude-sonnet-4-6",
        prompt_version="v1.0.0", prompt_hash="abc123def456",
        user_message="<context>...</context>",
        raw_response='{"decision":"enter",...}',
        input_tokens=4500, output_tokens=400, latency_ms=11500, attempts=1,
    )
    decision = _valid_decision_enter()
    decision_id = db.insert_decision(
        conn, claude_call_id=call_id, instrument="BTC/USD",
        trigger_filter="breakout_atr",
        trigger_ts=datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc),
        trigger_price=70_000.0,
        decision=decision,
    )
    verdict = _verdict_approved()
    veto_id = db.insert_veto(conn, decision_id=decision_id, verdict=verdict)
    signal_id = db.insert_signal(
        conn, decision_id=decision_id,
        instrument="BTC/USD", direction="long",
        entry_price=70_000.0, stop_loss=69_300.0, take_profit=71_500.0,
        position_usd=10_000.0, position_btc=0.142857,
        confidence=7, rr_ratio=2.14,
    )

    # All rows exist
    assert call_id > 0 and decision_id > 0 and veto_id > 0
    assert isinstance(signal_id, str) and len(signal_id) == 36  # UUID

    # FK integrity: get decision, follow to claude_call, follow to scan
    row = conn.execute(
        """SELECT d.confidence, c.model, s.instrument
           FROM decisions d
           JOIN claude_calls c ON d.claude_call_id = c.id
           JOIN scans s ON c.scan_id = s.id
           WHERE d.id=?""",
        (decision_id,),
    ).fetchone()
    assert row["confidence"] == 7
    assert row["model"] == "claude-sonnet-4-6"
    assert row["instrument"] == "BTC/USD"


def test_insert_decision_skip_stores_nulls(conn):
    call_id = db.insert_claude_call(
        conn, scan_id=None, model="claude-sonnet-4-6",
        prompt_version="v1.0.0", prompt_hash="abc",
        user_message="x", raw_response='{"decision":"skip",...}',
        input_tokens=4000, output_tokens=300, latency_ms=10000, attempts=1,
    )
    decision = _valid_decision_skip()
    decision_id = db.insert_decision(
        conn, claude_call_id=call_id, instrument="BTC/USD",
        trigger_filter=None, trigger_ts=None, trigger_price=None,
        decision=decision,
    )
    row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["decision"] == "skip"
    assert row["direction"] is None
    assert row["entry_price"] is None
    assert row["confidence"] == 4
    assert json.loads(row["key_risks_json"]) == []


def test_insert_veto_vetoed_no_signal(conn):
    """When risk manager vetoes, we insert veto_log but NOT a signal."""
    call_id = db.insert_claude_call(
        conn, scan_id=None, model="x", prompt_version="v1", prompt_hash="h",
        user_message="x", raw_response="x",
        input_tokens=1, output_tokens=1, latency_ms=1, attempts=1,
    )
    decision_id = db.insert_decision(
        conn, claude_call_id=call_id, instrument="BTC/USD",
        trigger_filter=None, trigger_ts=None, trigger_price=None,
        decision=_valid_decision_enter(),
    )
    db.insert_veto(conn, decision_id=decision_id, verdict=_verdict_vetoed())
    # No signals table row should exist
    n = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    assert n == 0


def test_insert_outcome_states(conn):
    # Need a signal first
    call_id = db.insert_claude_call(
        conn, scan_id=None, model="x", prompt_version="v1", prompt_hash="h",
        user_message="x", raw_response="x",
        input_tokens=1, output_tokens=1, latency_ms=1, attempts=1,
    )
    decision_id = db.insert_decision(
        conn, claude_call_id=call_id, instrument="BTC/USD",
        trigger_filter=None, trigger_ts=None, trigger_price=None,
        decision=_valid_decision_enter(),
    )
    signal_id = db.insert_signal(
        conn, decision_id=decision_id, instrument="BTC/USD", direction="long",
        entry_price=70_000.0, stop_loss=69_300.0, take_profit=71_500.0,
        position_usd=10_000.0, position_btc=0.14,
        confidence=7, rr_ratio=2.14,
    )
    db.insert_outcome(conn, signal_id=signal_id, price=70_500.0, state="open")
    db.insert_outcome(conn, signal_id=signal_id, price=71_500.0, state="tp_hit", pnl_pct=0.0214)
    rows = conn.execute(
        "SELECT state FROM outcomes WHERE signal_id=? ORDER BY id", (signal_id,)
    ).fetchall()
    assert [r["state"] for r in rows] == ["open", "tp_hit"]


def test_insert_outcome_invalid_state_raises(conn):
    call_id = db.insert_claude_call(
        conn, scan_id=None, model="x", prompt_version="v1", prompt_hash="h",
        user_message="x", raw_response="x",
        input_tokens=1, output_tokens=1, latency_ms=1, attempts=1,
    )
    decision_id = db.insert_decision(
        conn, claude_call_id=call_id, instrument="BTC/USD",
        trigger_filter=None, trigger_ts=None, trigger_price=None,
        decision=_valid_decision_enter(),
    )
    signal_id = db.insert_signal(
        conn, decision_id=decision_id, instrument="BTC/USD", direction="long",
        entry_price=70_000.0, stop_loss=69_300.0, take_profit=71_500.0,
        position_usd=1.0, position_btc=0.0001,
        confidence=7, rr_ratio=2.14,
    )
    with pytest.raises(ValueError):
        db.insert_outcome(conn, signal_id=signal_id, price=70_000, state="invalid_state")


# ─────────────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────────────


def test_decisions_summary_aggregates(conn):
    # Initially empty
    s = db.decisions_summary(conn)
    assert s["claude_calls"] == 0 and s["decisions"] == 0
    assert s["tokens_in_total"] == 0

    # Insert one full chain
    call_id = db.insert_claude_call(
        conn, scan_id=None, model="x", prompt_version="v1", prompt_hash="h",
        user_message="x", raw_response="x",
        input_tokens=4000, output_tokens=400, latency_ms=10000, attempts=1,
    )
    db.insert_decision(
        conn, claude_call_id=call_id, instrument="BTC/USD",
        trigger_filter=None, trigger_ts=None, trigger_price=None,
        decision=_valid_decision_skip(),
    )

    s = db.decisions_summary(conn)
    assert s["claude_calls"] == 1
    assert s["decisions"] == 1
    assert s["enters"] == 0   # we inserted a skip
    assert s["tokens_in_total"] == 4000
    assert s["tokens_out_total"] == 400


def test_signals_by_confidence_groups(conn):
    # Insert two approved signals with different confidence levels
    for conf in (7, 7, 8):
        call_id = db.insert_claude_call(
            conn, scan_id=None, model="x", prompt_version="v1", prompt_hash="h",
            user_message="x", raw_response="x",
            input_tokens=1, output_tokens=1, latency_ms=1, attempts=1,
        )
        d = Decision(
            decision="enter", direction="long",
            entry_price=70_000.0, stop_loss=69_300.0, take_profit=71_500.0,
            confidence=conf, size_hint="normal", reasoning="x",
        )
        decision_id = db.insert_decision(
            conn, claude_call_id=call_id, instrument="BTC/USD",
            trigger_filter=None, trigger_ts=None, trigger_price=None,
            decision=d,
        )
        db.insert_signal(
            conn, decision_id=decision_id, instrument="BTC/USD", direction="long",
            entry_price=70_000.0, stop_loss=69_300.0, take_profit=71_500.0,
            position_usd=10_000.0, position_btc=0.14,
            confidence=conf, rr_ratio=2.14,
        )

    buckets = db.signals_by_confidence(conn)
    by_conf = {b["confidence"]: b["n_signals"] for b in buckets}
    assert by_conf == {7: 2, 8: 1}
