"""
Smoke test for the Alpaca data layer -- all enabled instruments.

What it does
------------
1. Loads credentials from .env.
2. For each enabled instrument in config.INSTRUMENTS:
   - Creates the right provider (crypto or stock)
   - Fetches primary (H1) and context (H4) bars
   - Prints rows, date range, last close
3. Prints PASS / FAIL summary.

Run
---
    python -m scripts.smoke_fetch

Flags
-----
    --symbol BTC/USD   test only one symbol
    --bars N           how many bars to fetch (default 50)
    -v / --verbose     print full traceback on failure

Exit codes
----------
    0 -- all fetches succeeded
    1 -- credentials missing
    2 -- at least one fetch failed
"""
from __future__ import annotations

import argparse
import sys
import traceback

from bot.config import settings, get_enabled_instruments, get_instrument


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Data layer smoke test")
    p.add_argument("--symbol", default=None,
                   help="Test only this symbol (e.g. BTC/USD or NVDA)")
    p.add_argument("--bars", type=int, default=50,
                   help="Number of bars to fetch per timeframe (default 50)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print full traceback on failure")
    return p.parse_args()


def _print_df_summary(label: str, tf: str, df) -> None:
    if df.empty:
        print(f"      {label} ({tf}): <empty>")
        return
    last = df.iloc[-1]
    print(
        f"      {label} ({tf}): {len(df)} bars  "
        f"{df.index[0].date()} -> {df.index[-1].date()}  "
        f"last close={last['close']:.4f}"
    )


def main() -> int:
    args = parse_args()

    print("=" * 70)
    print("  AI Signal Bot -- data layer smoke test")
    print(f"  alpaca_paper={settings.alpaca_paper}")
    print("=" * 70)

    missing = settings.required_for_data()
    if missing:
        print()
        print("  ERROR: Missing env vars:")
        for name in missing:
            print(f"    - {name}")
        print()
        print("  Fix: copy .env.example to .env and fill in Alpaca paper keys.")
        print("       https://app.alpaca.markets/ -> Paper Trading -> API Keys")
        return 1

    # Pick instruments to test
    if args.symbol:
        inst = get_instrument(args.symbol)
        if inst is None:
            print(f"  ERROR: Symbol {args.symbol!r} not found in INSTRUMENTS list.")
            return 1
        instruments = [inst]
    else:
        instruments = get_enabled_instruments()

    if not instruments:
        print("  ERROR: No enabled instruments. Check bot/config.py.")
        return 1

    from bot.data.market import provider_for

    failed: list[str] = []
    for inst in instruments:
        print()
        print(f"  [{inst.symbol}]  kind={inst.kind}")
        try:
            provider = provider_for(inst)
            primary, context = provider.fetch_primary_and_context(
                inst.symbol,
                limit_primary=args.bars,
                limit_context=args.bars,
                timeframe_primary=inst.timeframe_primary,
                timeframe_context=inst.timeframe_context,
            )
            _print_df_summary("primary", inst.timeframe_primary, primary)
            _print_df_summary("context", inst.timeframe_context, context)
            print("      PASS")
        except Exception as exc:
            print(f"      FAIL: {exc.__class__.__name__}: {exc}")
            if args.verbose:
                traceback.print_exc()
            failed.append(inst.symbol)

    print()
    print("=" * 70)
    total = len(instruments)
    passed = total - len(failed)
    if failed:
        print(f"  RESULT: {passed}/{total} passed   FAILED: {', '.join(failed)}")
        return 2
    else:
        print(f"  RESULT: {passed}/{total} passed   Data layer OK")
        return 0


if __name__ == "__main__":
    sys.exit(main())
