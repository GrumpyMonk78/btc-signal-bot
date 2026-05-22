"""
Historical replay of the scanner.

Pulls the last ~1000 H1 bars (and matching H4 context) from Alpaca,
runs the scanner exactly as the live bot would, and prints a report:

  * total signals + counts per filter
  * distribution over time (signals per day, last 14 days)
  * sample of the most recent triggers with their numeric context

Writes the full signal list to data/scanner_replay.csv for further
inspection.

Usage
-----
    python -m scripts.scanner_replay
    python -m scripts.scanner_replay --bars 2000

Exit codes
----------
    0 — replay ran end-to-end
    1 — missing credentials
    2 — fetch / run failure
"""
from __future__ import annotations

import argparse
import csv
import sys
import traceback
from collections import Counter, defaultdict

from bot.config import settings


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--bars", type=int, default=1000,
        help="Number of H1 bars to replay (default: 1000 ≈ 42 days)",
    )
    p.add_argument(
        "--show", type=int, default=10,
        help="How many most-recent triggers to print in detail (default: 10)",
    )
    p.add_argument(
        "--csv", default="data/scanner_replay.csv",
        help="Output CSV path (default: data/scanner_replay.csv)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("─" * 78)
    print(f"  BTC AI Signal Bot — scanner replay")
    print(f"  instrument={settings.instrument}  H1 bars requested={args.bars}")
    print("─" * 78)

    missing = settings.required_for_data()
    if missing:
        print()
        print("  ✗ Missing env vars:", ", ".join(missing))
        print("    Fill them in .env (Alpaca paper keys work).")
        return 1

    try:
        from bot.data.market import default_provider, BarRequest
        from bot.strategy.scanner import scan

        provider = default_provider()
        primary = provider.fetch_bars(
            BarRequest(symbol=settings.instrument, timeframe=settings.timeframe_primary, limit=args.bars)
        )
        # For H4 context, give ourselves ample lookback. H4 = 4×H1, so
        # we need at least bars/4 H4 candles to fully cover the H1 range,
        # plus headroom for the H4 EMA50 seed.
        context_bars = max(200, args.bars // 4 + 80)
        context = provider.fetch_bars(
            BarRequest(symbol=settings.instrument, timeframe=settings.timeframe_context, limit=context_bars)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ✗ Fetch failed: {exc.__class__.__name__}: {exc}\n")
        traceback.print_exc()
        return 2

    if primary.empty:
        print("  ✗ Got zero primary bars from Alpaca — cannot replay.")
        return 2

    print(f"  ▸ fetched primary={len(primary)} rows  "
          f"[{primary.index[0].isoformat()} .. {primary.index[-1].isoformat()}]")
    print(f"  ▸ fetched context={len(context)} rows")

    try:
        signals = scan(primary, context)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ✗ Scanner crashed: {exc.__class__.__name__}: {exc}\n")
        traceback.print_exc()
        return 2

    # ── Report ────────────────────────────────────────────────────────────
    print()
    print(f"  ━━━ Results ━━━")
    print(f"  total signals: {len(signals)}")
    by_filter = Counter(s.filter for s in signals)
    for name in ("breakout_atr", "ema_pullback", "volume_absorption"):
        print(f"    {name:22s} {by_filter.get(name, 0)}")

    # Frequency per day (last 14 days of the range)
    per_day: dict[str, int] = defaultdict(int)
    for s in signals:
        per_day[s.timestamp.strftime("%Y-%m-%d")] += 1
    if per_day:
        all_days = sorted(per_day.keys())
        recent = all_days[-14:]
        print()
        print(f"  signals per day (last {len(recent)} day(s) with data):")
        for d in recent:
            bar = "█" * per_day[d]
            print(f"    {d}  {per_day[d]:>2}  {bar}")

    # Sample most recent triggers
    if signals:
        print()
        print(f"  ━━━ Last {min(args.show, len(signals))} triggers ━━━")
        for s in signals[-args.show:]:
            ctx = s.context
            print(
                f"    {s.timestamp.isoformat()}  {s.filter:22s} "
                f"close={ctx['close']:.2f}  ATR={ctx['atr14']:.2f}  "
                f"EMA20={ctx['ema20']:.2f}  vol={ctx['volume']:.4f}  "
                f"H4↑={'Y' if ctx['h4_uptrend'] else 'N'}"
            )

    # ── CSV dump ──────────────────────────────────────────────────────────
    try:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "filter", "price", "open", "high", "low",
                        "volume", "ema20", "ema50", "atr14", "vol_ma20", "h4_uptrend"])
            for s in signals:
                c = s.context
                w.writerow([
                    s.timestamp.isoformat(), s.filter, s.price,
                    c["open"], c["high"], c["low"], c["volume"],
                    c["ema20"], c["ema50"], c["atr14"], c["vol_ma20"],
                    int(c["h4_uptrend"]),
                ])
        print()
        print(f"  ✓ wrote {args.csv}")
    except OSError as exc:
        print(f"\n  ! could not write CSV ({exc}) — continuing")

    # ── Sanity summary ────────────────────────────────────────────────────
    span_days = (primary.index[-1] - primary.index[0]).total_seconds() / 86400 or 1
    rate = len(signals) / span_days * 7
    print()
    print(f"  ▸ effective signal rate ≈ {rate:.1f} per week (over {span_days:.1f} days)")
    if rate > 20:
        print("    ⚠ very high rate — scanner is too loose; expect heavy Claude usage")
    elif rate < 1:
        print("    ⚠ very low rate — scanner may be too tight; tune parameters")
    else:
        print("    ✓ rate looks sustainable")

    return 0


if __name__ == "__main__":
    sys.exit(main())
