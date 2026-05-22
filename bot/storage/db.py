"""
SQLite storage layer (synchronous, stdlib sqlite3).

We deliberately use sync sqlite3 instead of aiosqlite for phase 1:
  - the bot does at most a few inserts per hour
  - sync code is easier to reason about and test
  - SQLite is single-writer anyway; async helps only when contention is real
The scheduler can wrap a call in `asyncio.to_thread` if it ever needs to.

Tables (forward-only migrations)
--------------------------------
    schema_version   single-row table tracking current migration level
    scans            every scanner pass (rate kept low, but logged regardless)
    claude_calls     raw input/output to/from Claude, tokens, model, hash
    decisions        validated Decision JSON, FK to claude_calls
    veto_log         risk manager verdict, FK to decisions
    signals          approved signals (passed Claude AND risk manager)
    outcomes         periodic price checks vs SL/TP per signal_id

Public API
----------
    init_db(path) -> sqlite3.Connection
    Connection wrappers:
        insert_scan(conn, ...)
        insert_claude_call(conn, ...)
        insert_decision(conn, ...)
        insert_veto(conn, ...)
        insert_signal(conn, ...)
        insert_outcome(conn, ...)
    Query helpers:
        recent_signals(conn, limit=20)
        signals_by_confidence(conn)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Current migration level. Bump when you add tables / alter schema.
SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# DDL
# ─────────────────────────────────────────────────────────────────────────────


_DDL_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS scans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc            TEXT    NOT NULL,            -- when the scanner ran
    instrument        TEXT    NOT NULL,
    bars_primary      INTEGER NOT NULL,
    bars_context      INTEGER NOT NULL,
    n_signals         INTEGER NOT NULL,
    latest_filter     TEXT,                        -- name of last fired filter or NULL
    latest_signal_ts  TEXT,                        -- ISO timestamp of last signal or NULL
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(ts_utc);

CREATE TABLE IF NOT EXISTS claude_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL,
    scan_id         INTEGER REFERENCES scans(id),
    model           TEXT    NOT NULL,
    prompt_version  TEXT    NOT NULL,
    prompt_hash     TEXT    NOT NULL,
    user_message    TEXT    NOT NULL,              -- the full rendered user message
    raw_response    TEXT    NOT NULL,              -- exactly what Claude returned
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    latency_ms      INTEGER NOT NULL,
    attempts        INTEGER NOT NULL,
    error           TEXT                           -- non-null if the call failed
);
CREATE INDEX IF NOT EXISTS idx_claude_calls_ts ON claude_calls(ts_utc);
CREATE INDEX IF NOT EXISTS idx_claude_calls_prompt_hash ON claude_calls(prompt_hash);

CREATE TABLE IF NOT EXISTS decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc           TEXT    NOT NULL,
    claude_call_id   INTEGER NOT NULL REFERENCES claude_calls(id),
    instrument       TEXT    NOT NULL,
    trigger_filter   TEXT,                         -- which scanner filter fired
    trigger_ts       TEXT,                         -- ISO timestamp of scanner trigger
    trigger_price    REAL,
    decision         TEXT    NOT NULL,             -- 'enter' or 'skip'
    direction        TEXT,                         -- 'long' / 'short' / NULL
    entry_price      REAL,
    stop_loss        REAL,
    take_profit      REAL,
    confidence       INTEGER NOT NULL,
    size_hint        TEXT    NOT NULL,
    rr_ratio         REAL,
    reasoning        TEXT,
    key_risks_json   TEXT,                         -- JSON array of strings
    invalidation     TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts_utc);
CREATE INDEX IF NOT EXISTS idx_decisions_confidence ON decisions(confidence);

CREATE TABLE IF NOT EXISTS veto_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL,
    decision_id     INTEGER NOT NULL REFERENCES decisions(id),
    approved        INTEGER NOT NULL,              -- 0/1
    reason          TEXT    NOT NULL,
    veto_codes_json TEXT    NOT NULL,              -- JSON array of strings
    position_usd    REAL    NOT NULL,
    position_btc    REAL    NOT NULL,
    rr_ratio        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_veto_ts ON veto_log(ts_utc);

CREATE TABLE IF NOT EXISTS signals (
    signal_id        TEXT    PRIMARY KEY,           -- UUID, also used in Telegram message
    ts_utc           TEXT    NOT NULL,
    decision_id      INTEGER NOT NULL REFERENCES decisions(id),
    instrument       TEXT    NOT NULL,
    direction        TEXT    NOT NULL,
    entry_price      REAL    NOT NULL,
    stop_loss        REAL    NOT NULL,
    take_profit      REAL    NOT NULL,
    position_usd     REAL    NOT NULL,
    position_btc     REAL    NOT NULL,
    confidence       INTEGER NOT NULL,
    rr_ratio         REAL    NOT NULL,
    sent_to_telegram INTEGER NOT NULL DEFAULT 0    -- 0/1
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts_utc);

CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      TEXT    NOT NULL,
    signal_id   TEXT    NOT NULL REFERENCES signals(signal_id),
    price       REAL    NOT NULL,
    state       TEXT    NOT NULL,                  -- 'open' / 'sl_hit' / 'tp_hit' / 'time_stopped'
    pnl_pct     REAL,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_signal ON outcomes(signal_id);
"""


# ─────────────────────────────────────────────────────────────────────────────
# Connection + migrations
# ─────────────────────────────────────────────────────────────────────────────


def init_db(path: str | Path) -> sqlite3.Connection:
    """Open (or create) the SQLite DB at `path` and apply migrations.

    Returns a Connection with foreign keys enabled and row factory set.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # better concurrency for read-while-write
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def init_memory_db() -> sqlite3.Connection:
    """In-memory DB for tests."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply schema migrations idempotently."""
    conn.executescript(_DDL_V1)
    # Record current version
    cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    elif row["version"] < SCHEMA_VERSION:
        # No-op for V1; placeholder for future migrations
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    return int(row["version"]) if row else 0


# ─────────────────────────────────────────────────────────────────────────────
# Insert helpers
# ─────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_scan(
    conn: sqlite3.Connection,
    *,
    instrument: str,
    bars_primary: int,
    bars_context: int,
    n_signals: int,
    latest_filter: str | None = None,
    latest_signal_ts: datetime | None = None,
    notes: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO scans
           (ts_utc, instrument, bars_primary, bars_context, n_signals,
            latest_filter, latest_signal_ts, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), instrument, bars_primary, bars_context, n_signals,
         latest_filter, latest_signal_ts.isoformat() if latest_signal_ts else None, notes),
    )
    return int(cur.lastrowid)


def insert_claude_call(
    conn: sqlite3.Connection,
    *,
    scan_id: int | None,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    user_message: str,
    raw_response: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    attempts: int,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO claude_calls
           (ts_utc, scan_id, model, prompt_version, prompt_hash,
            user_message, raw_response, input_tokens, output_tokens,
            latency_ms, attempts, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), scan_id, model, prompt_version, prompt_hash,
         user_message, raw_response, input_tokens, output_tokens,
         latency_ms, attempts, error),
    )
    return int(cur.lastrowid)


def insert_decision(
    conn: sqlite3.Connection,
    *,
    claude_call_id: int,
    instrument: str,
    trigger_filter: str | None,
    trigger_ts: datetime | None,
    trigger_price: float | None,
    decision,                  # bot.storage.models.Decision
) -> int:
    direction = decision.direction.value if decision.direction else None
    cur = conn.execute(
        """INSERT INTO decisions
           (ts_utc, claude_call_id, instrument, trigger_filter, trigger_ts, trigger_price,
            decision, direction, entry_price, stop_loss, take_profit,
            confidence, size_hint, rr_ratio, reasoning, key_risks_json, invalidation)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), claude_call_id, instrument, trigger_filter,
         trigger_ts.isoformat() if trigger_ts else None,
         trigger_price,
         decision.decision, direction,
         decision.entry_price, decision.stop_loss, decision.take_profit,
         decision.confidence, decision.size_hint,
         decision.risk_reward_ratio() if decision.decision == "enter" else None,
         decision.reasoning,
         json.dumps(decision.key_risks),
         decision.invalidation),
    )
    return int(cur.lastrowid)


def insert_veto(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    verdict,                   # bot.storage.models.RiskVerdict
) -> int:
    cur = conn.execute(
        """INSERT INTO veto_log
           (ts_utc, decision_id, approved, reason, veto_codes_json,
            position_usd, position_btc, rr_ratio)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now(), decision_id, int(verdict.approved), verdict.reason,
         json.dumps(verdict.veto_codes),
         verdict.position_size_usd, verdict.position_size_btc, verdict.r_r_ratio),
    )
    return int(cur.lastrowid)


def insert_signal(
    conn: sqlite3.Connection,
    *,
    decision_id: int,
    instrument: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    position_usd: float,
    position_btc: float,
    confidence: int,
    rr_ratio: float,
    sent_to_telegram: bool = False,
) -> str:
    """Insert an approved signal and return its UUID (signal_id)."""
    signal_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO signals
           (signal_id, ts_utc, decision_id, instrument, direction,
            entry_price, stop_loss, take_profit,
            position_usd, position_btc, confidence, rr_ratio,
            sent_to_telegram)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (signal_id, _now(), decision_id, instrument, direction,
         entry_price, stop_loss, take_profit,
         position_usd, position_btc, confidence, rr_ratio,
         int(sent_to_telegram)),
    )
    return signal_id


def insert_outcome(
    conn: sqlite3.Connection,
    *,
    signal_id: str,
    price: float,
    state: str,
    pnl_pct: float | None = None,
    notes: str = "",
) -> int:
    if state not in ("open", "sl_hit", "tp_hit", "time_stopped"):
        raise ValueError(f"invalid outcome state: {state!r}")
    cur = conn.execute(
        """INSERT INTO outcomes (ts_utc, signal_id, price, state, pnl_pct, notes)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (_now(), signal_id, price, state, pnl_pct, notes),
    )
    return int(cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────


def recent_signals(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY ts_utc DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def signals_by_confidence(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """For each confidence bucket: count of approved signals + outcome stats."""
    rows = conn.execute(
        """SELECT confidence,
                  COUNT(*)                AS n_signals,
                  SUM(CASE WHEN sent_to_telegram=1 THEN 1 ELSE 0 END) AS n_sent
           FROM signals
           GROUP BY confidence
           ORDER BY confidence DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def decisions_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Quick health snapshot for ad-hoc inspection."""
    n_decisions = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    n_enter = conn.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE decision='enter'"
    ).fetchone()["n"]
    n_signals = conn.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"]
    n_calls = conn.execute("SELECT COUNT(*) AS n FROM claude_calls").fetchone()["n"]
    tok_in = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0) AS t FROM claude_calls"
    ).fetchone()["t"]
    tok_out = conn.execute(
        "SELECT COALESCE(SUM(output_tokens),0) AS t FROM claude_calls"
    ).fetchone()["t"]
    return {
        "claude_calls": n_calls,
        "decisions": n_decisions,
        "enters": n_enter,
        "approved_signals": n_signals,
        "tokens_in_total": tok_in,
        "tokens_out_total": tok_out,
    }
