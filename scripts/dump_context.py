"""
Render the full Claude prompt payload for the current BTC state.

What it does
------------
1. Fetches last ~1000 H1 + matching H4 bars from Alpaca.
2. Picks the most recent scanner trigger if it's fresh (<6h), else
   synthesises a pseudo-trigger from the last H1 bar.
3. Fetches live news + Fear & Greed Index + macro calendar.
4. Assembles a DeciderContext and prints both:
     - the rendered user message that would go to Claude
     - the JSON form (for DB persistence later)

Flags:
    --mock              skip live news / F&G fetches (use stubs)
    --signal-mode {auto,real,latest_bar}
                        which signal to render (auto picks fresh real,
                        else falls back to pseudo)
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone

from bot.config import settings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bars", type=int, default=1000)
    p.add_argument("--max-primary", type=int, default=30, dest="max_primary")
    p.add_argument("--max-context", type=int, default=30, dest="max_context")
    p.add_argument("--mock", action="store_true",
                   help="Use stub news / sentiment instead of live fetches")
    p.add_argument("--signal-mode", choices=["auto", "real", "latest_bar"],
                   default="auto",
                   help="auto = use real if <6h old, else pseudo; "
                        "real = use latest real trigger; "
                        "latest_bar = always pseudo-trigger from last H1 bar")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("-" * 78)
    print(f"  BTC AI Signal Bot - dump_context")
    print(f"  instrument={settings.instrument}")
    print("-" * 78)

    missing = settings.required_for_data()
    if missing:
        print(f"\n  X Missing env vars: {missing}")
        return 1

    try:
        from bot.data.market import default_provider, BarRequest
        from bot.strategy.scanner import scan, ScannerSignal
        from bot.storage.models import PortfolioState
        from bot.llm.context import assemble_context, render_context_for_prompt
        from bot.llm.prompts import active_prompt

        provider = default_provider()
        primary = provider.fetch_bars(
            BarRequest(symbol=settings.instrument,
                       timeframe=settings.timeframe_primary, limit=args.bars)
        )
        context_bars = max(200, args.bars // 4 + 80)
        context = provider.fetch_bars(
            BarRequest(symbol=settings.instrument,
                       timeframe=settings.timeframe_context, limit=context_bars)
        )

        signals = scan(primary, context)
        as_of = datetime.now(timezone.utc)

        # ── Decide which trigger to render ────────────────────────────────
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
                print("\n  X --signal-mode=real but no scanner signals in range.")
                return 0
            last = real_signal
            mode_note = f"real scanner trigger ({last.filter})"
        elif args.signal_mode == "latest_bar":
            last = _pseudo_trigger()
            mode_note = "pseudo-trigger from last H1 bar"
        else:  # auto
            if real_is_fresh:
                last = real_signal
                mode_note = f"real scanner trigger ({last.filter}, fresh)"
            else:
                last = _pseudo_trigger()
                if real_signal is None:
                    stale_note = "no recent scanner trigger"
                else:
                    age = as_of - real_signal.timestamp.to_pydatetime()
                    stale_note = f"last real trigger is {age} old"
                mode_note = f"pseudo-trigger from last H1 bar ({stale_note})"

        print(f"\n  > trigger: {mode_note}")
        print(f"    timestamp={last.timestamp.isoformat()}  filter={last.filter}  price={last.price:.2f}")
        if last.context.get("_pseudo"):
            print("    ! pseudo-trigger: indicators are NaN, h4_uptrend=0")
            print("      Use --signal-mode=real to render the last real scanner trigger")

        # ── External context ──────────────────────────────────────────────
        if args.mock:
            news = []
            sentiment = None
            print("  > mock mode: skipping live news + F&G fetches")
        else:
            try:
                from bot.data.news import fetch_news
                news = fetch_news(hours=24, max_items=10)
                print(f"  > fetched {len(news)} news items")
            except Exception as exc:
                print(f"  ! news fetch failed: {exc} - continuing without news")
                news = []
            try:
                from bot.data.sentiment import fetch_fear_greed
                sentiment = fetch_fear_greed(history_days=7)
                print(f"  > F&G = {sentiment.value} ({sentiment.classification})")
            except Exception as exc:
                print(f"  ! F&G fetch failed: {exc} - continuing without sentiment")
                sentiment = None

        from bot.data import calendar as macro_cal
        macro_recent = macro_cal.recent_within(12, now=as_of)
        macro_upcoming = macro_cal.upcoming_within(12, now=as_of)
        print(f"  > macro: {len(macro_recent)} recent, {len(macro_upcoming)} upcoming")

        portfolio = PortfolioState(
            equity_usd=10_000.0,
            open_positions=0,
            daily_pnl_pct=0.0,
            remaining_position_slots=settings.max_open_positions,
        )

        ctx = assemble_context(
            instrument=settings.instrument,
            primary_df=primary,
            context_df=context,
            trigger=last,
            news=news,
            sentiment=sentiment,
            macro_recent=macro_recent,
            macro_upcoming=macro_upcoming,
            portfolio=portfolio,
            as_of=as_of,
            max_primary_bars=args.max_primary,
            max_context_bars=args.max_context,
        )

        rendered = render_context_for_prompt(ctx)
        version, prompt_text, prompt_h = active_prompt()

        print()
        print("-" * 78)
        print(f"  Active prompt: {version}  (hash={prompt_h})")
        print(f"  Prompt length: {len(prompt_text)} chars")
        print(f"  Rendered user message: {len(rendered)} chars")
        print("-" * 78)
        print()
        print("====== USER MESSAGE (what Claude would see) ======")
        print(rendered)
        print()
        print("====== JSON CONTEXT (for DB persistence) ======")
        print(ctx.model_dump_json(indent=2))

        return 0

    except Exception as exc:
        print(f"\n  X Failed: {exc.__class__.__name__}: {exc}\n")
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
