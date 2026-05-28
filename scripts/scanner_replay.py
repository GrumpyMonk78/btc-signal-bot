"""
Historical scanner replay — vsechny aktivni instrumenty.

Stahne historicka data z Alpaca, spusti scanner a zobrazi:
  - kolik triggeru bylo celkem a per filtr
  - signal rate (triggers/tyden)
  - distribuci po dnech (posledni 2 tydny)
  - posledni N triggeru s detaily

Usage
-----
    python -m scripts.scanner_replay                  # vsechny instrumenty, 500 baru
    python -m scripts.scanner_replay --bars 200       # posledních 200 H1 svicek
    python -m scripts.scanner_replay --symbol NVDA    # jen jeden instrument
    python -m scripts.scanner_replay --csv out.csv    # uloz do CSV

Exit codes
----------
    0 -- replay probehl
    1 -- chybejici credentials
    2 -- chyba pri fetchovani dat
"""
from __future__ import annotations

import argparse
import csv
import sys
import traceback
from collections import Counter, defaultdict

from bot.config import settings, get_enabled_instruments, get_instrument


FILTER_NAMES = ["breakout_atr", "ema_pullback", "volume_absorption"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scanner backtest replay")
    p.add_argument("--bars", type=int, default=500,
                   help="H1 baru k replayi (default 500 ~= 3 tydny)")
    p.add_argument("--symbol", default=None,
                   help="Testuj jen tento symbol (napr. NVDA, BTC/USD)")
    p.add_argument("--show", type=int, default=5,
                   help="Kolik poslednich triggeru zobrazit v detailu (default 5)")
    p.add_argument("--csv", default=None,
                   help="Uloz signaly do CSV souboru")
    return p.parse_args()


def _bar(n: int, width: int = 30) -> str:
    filled = min(n, width)
    return "█" * filled + ("" if n <= width else f" +{n - width}")


def replay_instrument(inst, bars: int, show: int) -> list:
    """Spusti replay pro jeden instrument. Vraci seznam ScannerSignal."""
    from bot.data.market import provider_for, BarRequest
    from bot.strategy.scanner import scan

    provider = provider_for(inst)
    primary = provider.fetch_bars(BarRequest(
        symbol=inst.symbol,
        timeframe=inst.timeframe_primary,
        limit=bars,
    ))
    context_limit = max(800, bars // 4 + 80)  # EMA200 na H4 potřebuje min 200 H4 barů
    context = provider.fetch_bars(BarRequest(
        symbol=inst.symbol,
        timeframe=inst.timeframe_context,
        limit=context_limit,
    ))

    if primary.empty:
        print(f"  ! {inst.symbol}: zadna data")
        return []

    signals = scan(primary, context)

    span_days = (primary.index[-1] - primary.index[0]).total_seconds() / 86400 or 1
    rate = len(signals) / span_days * 7

    by_filter = Counter(s.filter for s in signals)

    print(f"  Rozsah dat:  {primary.index[0].strftime('%Y-%m-%d')} "
          f"-> {primary.index[-1].strftime('%Y-%m-%d')}  "
          f"({len(primary)} baru H1, {span_days:.0f} dni)")
    print(f"  Triggery:    {len(signals)} celkem  "
          f"(rate ~{rate:.1f}/tyden)")
    for fn in FILTER_NAMES:
        n = by_filter.get(fn, 0)
        print(f"    {fn:24s}  {n:3d}")

    # Rate hodnoceni
    if rate == 0:
        print(f"  !! PROBLEM: 0 triggeru — scanner je prilis prisny nebo data nestaci")
    elif rate < 1:
        print(f"  !  Nizky rate — mozna prilis prisne filtry")
    elif rate > 20:
        print(f"  !  Vysoky rate — scanner je prilis volny, prilis casto vola Claude")
    else:
        print(f"  OK rate")

    # Distribuce po dnech (posledni 2 tydny)
    per_day: dict[str, int] = defaultdict(int)
    for s in signals:
        per_day[s.timestamp.strftime("%Y-%m-%d")] += 1
    if per_day:
        recent_days = sorted(per_day.keys())[-14:]
        print()
        print(f"  Signaly po dnech (posledni {len(recent_days)} dni):")
        max_n = max(per_day[d] for d in recent_days)
        for d in recent_days:
            n = per_day[d]
            bar_width = int(n / max(max_n, 1) * 20)
            print(f"    {d}  {n:2d}  {'█' * bar_width}")

    # Posledni N triggeru
    if signals and show > 0:
        print()
        print(f"  Poslednich {min(show, len(signals))} triggeru:")
        for s in signals[-show:]:
            c = s.context
            rsi = c.get("rsi14", float("nan"))
            macd_h = c.get("macd_hist", float("nan"))
            print(
                f"    {s.timestamp.strftime('%Y-%m-%d %H:%M')}  "
                f"{s.filter:22s}  "
                f"close={c['close']:.2f}  "
                f"ATR={c['atr14']:.2f}  "
                f"RSI={rsi:.1f}  "
                f"MACD_hist={macd_h:.3f}  "
                f"H4up={'Y' if c['h4_uptrend'] else 'N'}"
            )

    return signals


def main() -> int:
    args = parse_args()

    print("=" * 70)
    print("  AI Signal Bot — Scanner Replay")
    print(f"  H1 baru: {args.bars}  (~{args.bars // 24} dni)")
    print("=" * 70)

    missing = settings.required_for_data()
    if missing:
        print(f"\n  CHYBA: Chybejici env vars: {missing}")
        return 1

    # Vyber instrumenty
    if args.symbol:
        inst = get_instrument(args.symbol)
        if inst is None:
            print(f"  CHYBA: Symbol {args.symbol!r} nenalezen v INSTRUMENTS")
            return 1
        instruments = [inst]
    else:
        instruments = get_enabled_instruments()

    all_signals: dict[str, list] = {}
    failed: list[str] = []

    for inst in instruments:
        print()
        print(f"{'=' * 70}")
        print(f"  [{inst.symbol}]  ({inst.kind})")
        print(f"{'─' * 70}")
        try:
            sigs = replay_instrument(inst, args.bars, args.show)
            all_signals[inst.symbol] = sigs
        except Exception as exc:
            print(f"  CHYBA: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            failed.append(inst.symbol)

    # Souhrnna tabulka
    print()
    print("=" * 70)
    print("  SOUHRNNA TABULKA")
    print("=" * 70)
    print(f"  {'Symbol':<12} {'Triggery':>9} {'Rate/tyden':>11} {'breakout':>9} {'pullback':>9} {'volume':>8}")
    print(f"  {'-'*12} {'-'*9} {'-'*11} {'-'*9} {'-'*9} {'-'*8}")

    total_sigs = 0
    for inst in instruments:
        if inst.symbol in all_signals:
            sigs = all_signals[inst.symbol]
            total_sigs += len(sigs)
            by_f = Counter(s.filter for s in sigs)
            # Odhadni span z prvniho/posledniho signalu nebo jen z poctu baru
            if sigs:
                span = (sigs[-1].timestamp - sigs[0].timestamp).total_seconds() / 86400 or 1
            else:
                span = args.bars / 24
            rate = len(sigs) / max(span, 1) * 7
            print(
                f"  {inst.symbol:<12} {len(sigs):>9} {rate:>10.1f}/w "
                f"{by_f.get('breakout_atr', 0):>9} "
                f"{by_f.get('ema_pullback', 0):>9} "
                f"{by_f.get('volume_absorption', 0):>8}"
            )
        else:
            print(f"  {inst.symbol:<12} {'CHYBA':>9}")

    print(f"  {'-'*12} {'-'*9}")
    print(f"  {'CELKEM':<12} {total_sigs:>9}")

    if total_sigs == 0:
        print()
        print("  !! VSECHNY INSTRUMENTY: 0 triggeru")
        print("     Mozne priciny:")
        print("     - H4 uptrend gate blokuje vse (trh v downtrendu)")
        print("     - Prilis malo dat (zkus --bars 1000)")
        print("     - Filtry jsou prilis prisne")
    elif total_sigs < len(instruments):
        print()
        print("  ! Malo triggeru — uvazuj o uvolneni filtru (viz bot/strategy/scanner.py)")

    # CSV export
    if args.csv:
        try:
            rows = []
            for symbol, sigs in all_signals.items():
                for s in sigs:
                    c = s.context
                    rows.append({
                        "symbol": symbol,
                        "timestamp": s.timestamp.isoformat(),
                        "filter": s.filter,
                        "price": s.price,
                        "ema20": c.get("ema20"),
                        "ema50": c.get("ema50"),
                        "atr14": c.get("atr14"),
                        "rsi14": c.get("rsi14"),
                        "macd_hist": c.get("macd_hist"),
                        "vol_ma20": c.get("vol_ma20"),
                        "h4_uptrend": int(c.get("h4_uptrend", 0)),
                    })
            with open(args.csv, "w", newline="") as f:
                if rows:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
            print(f"\n  CSV ulozen: {args.csv}  ({len(rows)} radku)")
        except OSError as exc:
            print(f"\n  ! CSV se nepodarilo ulozit: {exc}")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
