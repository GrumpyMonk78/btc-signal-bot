"""
One-shot Claude call on the current BTC/USD state.

What it does
------------
1. Fetches the last ~200 H1 and H4 bars from Alpaca.
2. Picks the most recent scanner trigger if one is fresh (<6h old). If not,
   synthesises a pseudo-trigger from the last H1 bar so we can still ask
   Claude what it thinks of the current state.
3. Fetches live news + Fear & Greed Index. Uses a stub portfolio.
4. Calls Claude (model from .env, default claude-sonnet-4-6).
5. Prints the validated Decision, reasoning, key risks, and the cost.

Run:
    python -m scripts.ask_claude

Flags:
    --mock-llm        do not call Claude; print the payload only
    --signal-mode {auto,real,latest_bar}   same semantics as dump_context.py
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone

from bot.config import settings


# Sonnet 4.6 price (USD / 1M tokens) — approximate, public list.
# We use this only to print an estimated cost. Real billing comes from
# the Anthropic console.
_PRICE_PER_M_TOKENS = {
    # model_prefix: (input, output)
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4":   (15.0, 75.0),
    "claude-haiku-4":  (0.8, 4.0),
}


def _price(model: str, input_tokens: int, output_tokens: int) -> float:
    for prefix, (pin, pout) in _PRICE_PER_M_TOKENS.items():
        if model.startswith(prefix):
            return (input_tokens * pin + output_tokens * pout) / 1_000_000
    return 0.0  # unknown model — skip cost print


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bars", type=int, default=200,
                   help="H1 bars to fetch for the scanner (default 200)")
    p.add_argument("--max-primary", type=int, default=30, dest="max_primary")
    p.add_argument("--max-context", type=int, default=30, dest="max_context")
    p.add_argument("--mock-llm", action="store_true",
                   help="Skip the Claude call; just print the assembled payload")
    p.add_argument("--signal-mode", choices=["auto", "real", "latest_bar"],
                   default="auto")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("─" * 78)
    print("  BTC AI Signal Bot — ask_claude (one-shot live call)")
    print(f"  instrument={settings.instrument}  model={settings.anthropic_model}  mode={settings.mode.value}")
    print("─" * 78)

    # ── Secrets ───────────────────────────────────────────────────────────
    missing_data = settings.required_for_data()
    missing_llm = [] if args.mock_llm or settings.anthropic_api_key else ["ANTHROPIC_API_KEY"]
    missing = missing_data + missing_llm
    if missing:
        print(f"\n  ✗ Missing env vars: {missing}")
        return 1

    try:
        from bot.data.market import default_provider, BarRequest
        from bot.strategy.scanner import scan, ScannerSignal
        from bot.storage.models import PortfolioState
        from bot.llm.context import assemble_context, render_context_for_prompt
        from bot.llm.prompts import active_prompt

        # ── Market data ──────────────────────────────────────────────────
        provider = default_provider()
        primary = provider.fetch_bars(
            BarRequest(symbol=settings.instrument,
                       timeframe=settings.timeframe_primary, limit=args.bars)
        )
        context = provider.fetch_bars(
            BarRequest(symbol=settings.instrument,
                       timeframe=settings.timeframe_context, limit=max(200, args.bars // 4 + 80))
        )
        print(f"  ▸ fetched {len(primary)} H1 + {len(context)} H4 bars  "
              f"(last close = {primary['close'].iloc[-1]:.2f})")

        # ── Signal selection ─────────────────────────────────────────────
        signals = scan(primary, context)
        as_of = datetime.now(timezone.utc)

        real_signal = signals[-1] if signals else None
        real_is_fresh = (
            real_signal is not None
            and (as_of - real_signal.timestamp.to_pydatetime()) < timedelta(hours=6)
        )

        def _pseudo_trigger() -> ScannerSignal:
            last_bar = primary.iloc[-1]
            return ScannerSignal(
                timestamp=primary.index[-1],
                filter="ema_pullback",
                price=float(last_bar["close"]),
                context={
                    "close": float(last_bar["close"]),
                    "open": float(last_bar["open"]),
                    "high": float(last_bar["high"]),
                    "low": float(last_bar["low"]),
                    "volume": float(last_bar["volume"]),
                    "ema20": float("nan"),
                    "ema50": float("nan"),
                    "atr14": float("nan"),
                    "vol_ma20": float("nan"),
                    "h4_uptrend": 0.0,
                    "_pseudo": 1.0,
                },
            )

        if args.signal_mode == "real":
            if real_signal is None:
                print("\n  ✗ --signal-mode=real but no scanner signals in range.")
                return 0
            sig = real_signal
            kind = "real"
        elif args.signal_mode == "latest_bar":
            sig = _pseudo_trigger()
            kind = "pseudo"
        else:
            sig = real_signal if real_is_fresh else _pseudo_trigger()
            kind = "real (fresh)" if real_is_fresh else "pseudo (no fresh trigger)"

        print(f"  ▸ trigger: {kind}  ts={sig.timestamp.isoformat()}  "
              f"filter={sig.filter}  price={sig.price:.2f}")

        # ── External context ─────────────────────────────────────────────
        try:
            from bot.data.news import fetch_news
            news = fetch_news(hours=24, max_items=10)
            print(f"  ▸ fetched {len(news)} news items")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! news fetch failed: {exc} — continuing without news")
            news = []
        try:
            from bot.data.sentiment import fetch_fear_greed
            sentiment = fetch_fear_greed(history_days=7)
            print(f"  ▸ F&G = {sentiment.value} ({sentiment.classification})")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! F&G fetch failed: {exc} — continuing without sentiment")
            sentiment = None

        from bot.data import calendar as macro_cal
        macro_recent = macro_cal.recent_within(12, now=as_of)
        macro_upcoming = macro_cal.upcoming_within(12, now=as_of)
        print(f"  ▸ macro: {len(macro_recent)} recent, {len(macro_upcoming)} upcoming")

        portfolio = PortfolioState(
            equity_usd=10_000.0, open_positions=0,
            daily_pnl_pct=0.0, remaining_position_slots=settings.max_open_positions,
        )

        # ── Assemble ─────────────────────────────────────────────────────
        ctx = assemble_context(
            instrument=settings.instrument,
            primary_df=primary, context_df=context,
            trigger=sig,
            news=news, sentiment=sentiment,
            macro_recent=macro_recent, macro_upcoming=macro_upcoming,
            portfolio=portfolio,
            as_of=as_of,
            max_primary_bars=args.max_primary,
            max_context_bars=args.max_context,
        )

        prompt_version, system_text, prompt_h = active_prompt()
        user_text = render_context_for_prompt(ctx)
        print()
        print(f"  ▸ prompt v={prompt_version}  hash={prompt_h}  "
              f"system={len(system_text)}ch  user={len(user_text)}ch")

        # ── Mock mode: stop here ─────────────────────────────────────────
        if args.mock_llm:
            print("\n  --mock-llm: payload assembled, skipping Claude call.")
            return 0

        # ── Open DB (creates + migrates on first run) ────────────────────
        from bot.storage import db as storage_db
        from bot.risk.manager import evaluate as risk_evaluate
        conn = storage_db.init_db(settings.db_path)
        print(f"  ▸ DB ready at {settings.db_path}")

        # Log the scan (even before Claude call — audit-first)
        scan_id = storage_db.insert_scan(
            conn,
            instrument=settings.instrument,
            bars_primary=len(primary),
            bars_context=len(context),
            n_signals=len(signals),
            latest_filter=(real_signal.filter if real_signal else None),
            latest_signal_ts=(real_signal.timestamp.to_pydatetime() if real_signal else None),
            notes=f"mode={args.signal_mode}; used={kind}",
        )

        # ── Live call ────────────────────────────────────────────────────
        from bot.llm.decider import decide
        print()
        print("  ▸ calling Claude…")
        result = decide(ctx)

        # ── Print decision ───────────────────────────────────────────────
        d = result.decision
        print()
        print("═" * 78)
        print(f"  Decision: {d.decision.upper()}"
              + (f"  {d.direction.value.upper()}" if d.direction else "")
              + f"  confidence={d.confidence}/10  size_hint={d.size_hint}")
        if d.decision == "enter":
            rr = d.risk_reward_ratio()
            print(f"  entry={d.entry_price:.2f}  SL={d.stop_loss:.2f}  TP={d.take_profit:.2f}"
                  f"  R:R={rr:.2f}")
        print("═" * 78)
        print(f"\n  Reasoning:\n    {d.reasoning}")
        if d.key_risks:
            print("\n  Key risks:")
            for r in d.key_risks:
                print(f"    - {r}")
        if d.invalidation:
            print(f"\n  Invalidation: {d.invalidation}")

        # ── Persist Claude call + decision ───────────────────────────────
        call_id = storage_db.insert_claude_call(
            conn,
            scan_id=scan_id,
            model=result.model,
            prompt_version=result.prompt_version,
            prompt_hash=result.prompt_hash,
            user_message=user_text,
            raw_response=result.raw_response,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
        )
        decision_id = storage_db.insert_decision(
            conn,
            claude_call_id=call_id,
            instrument=settings.instrument,
            trigger_filter=sig.filter,
            trigger_ts=sig.timestamp.to_pydatetime(),
            trigger_price=float(sig.price),
            decision=d,
        )

        # ── Run risk manager ─────────────────────────────────────────────
        verdict = risk_evaluate(d, portfolio, sig, now=as_of)
        storage_db.insert_veto(conn, decision_id=decision_id, verdict=verdict)

        # If approved → insert into signals table, then send to Telegram
        signal_id: str | None = None
        telegram_sent = False
        if verdict.approved and d.decision == "enter" and d.direction is not None:
            signal_id = storage_db.insert_signal(
                conn,
                decision_id=decision_id,
                instrument=settings.instrument,
                direction=d.direction.value,
                entry_price=float(d.entry_price),
                stop_loss=float(d.stop_loss),
                take_profit=float(d.take_profit),
                position_usd=verdict.position_size_usd,
                position_btc=verdict.position_size_btc,
                confidence=d.confidence,
                rr_ratio=verdict.r_r_ratio,
                sent_to_telegram=False,
            )

            # ── Send to Telegram (best-effort) ──────────────────────────
            from bot.notify import telegram as tg
            if tg.is_configured():
                from bot.storage.models import ApprovedSignal
                approved = ApprovedSignal(
                    signal_id=signal_id,
                    instrument=settings.instrument,
                    direction=d.direction,
                    entry_price=float(d.entry_price),
                    stop_loss=float(d.stop_loss),
                    take_profit=float(d.take_profit),
                    position_size_usd=verdict.position_size_usd,
                    position_size_btc=verdict.position_size_btc,
                    confidence=d.confidence,
                    r_r_ratio=verdict.r_r_ratio,
                    reasoning=d.reasoning,
                    key_risks=d.key_risks,
                    invalidation=d.invalidation,
                    created_at=as_of,
                )
                telegram_sent = tg.send_signal(approved, d)
                if telegram_sent:
                    conn.execute(
                        "UPDATE signals SET sent_to_telegram=1 WHERE signal_id=?",
                        (signal_id,),
                    )
            else:
                print("  ! Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
                print("    Signal logged to DB but not sent. Run scripts/telegram_test.py to set up.")

        # ── Print verdict ────────────────────────────────────────────────
        print()
        print("─" * 78)
        print(f"  Risk verdict: {'APPROVED' if verdict.approved else 'REJECTED'}")
        print(f"  Reason: {verdict.reason}")
        if verdict.veto_codes:
            print(f"  Veto codes: {', '.join(verdict.veto_codes)}")
        if verdict.position_size_usd > 0:
            print(f"  Position size: ${verdict.position_size_usd:,.2f} "
                  f"({verdict.position_size_btc:.6f} BTC)")
        if signal_id:
            print(f"  ✓ Signal logged: {signal_id}")
            if telegram_sent:
                print(f"  ✓ Sent to Telegram")
            elif verdict.approved:
                print(f"  ! Signal NOT sent to Telegram (token/chat_id missing or send failed)")
        print("─" * 78)

        # ── Metadata ─────────────────────────────────────────────────────
        cost = _price(result.model, result.input_tokens, result.output_tokens)
        print()
        print("─" * 78)
        print(f"  model={result.model}  attempts={result.attempts}  "
              f"latency={result.latency_ms} ms")
        print(f"  tokens: in={result.input_tokens}  out={result.output_tokens}"
              + (f"  est. cost ≈ ${cost:.4f}" if cost else ""))
        print(f"  DB: {settings.db_path}  (scan_id={scan_id}, call_id={call_id}, decision_id={decision_id})")
        # DB summary
        summary = storage_db.decisions_summary(conn)
        print(f"  totals so far: {summary['claude_calls']} calls, "
              f"{summary['decisions']} decisions ({summary['enters']} enter), "
              f"{summary['approved_signals']} approved signals, "
              f"{summary['tokens_in_total']}+{summary['tokens_out_total']} tokens")
        print("─" * 78)

        conn.close()
        return 0

    except Exception as exc:  # noqa: BLE001
        print(f"\n  ✗ Failed: {exc.__class__.__name__}: {exc}\n")
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
