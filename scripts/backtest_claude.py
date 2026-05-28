"""
Claude backtest — pro každý historický trigger zavolá Claude API a simuluje outcome.

Pro každý trigger:
  1. Sestaví kontext TAK JAK HO BOT VIDĚL V TEN MOMENT (data pouze do triggeru)
  2. Zavolá Claude API → decision (enter/skip, confidence, SL, TP)
  3. Simuluje outcome bar-by-bar: první zasažení SL nebo TP ukončí trade
  4. Vypočítá PnL pokud Claude řekl "enter"

Výstup: tabulka v terminálu + CSV soubor.

Usage
-----
    python -m scripts.backtest_claude --bars 500
    python -m scripts.backtest_claude --bars 500 --symbol BTC/USD
    python -m scripts.backtest_claude --bars 500 --csv backtest_out.csv
    python -m scripts.backtest_claude --bars 500 --dry-run   # bez Claude API, jen triggery
    python -m scripts.backtest_claude --bars 500 --max-bars 48  # SL/TP timeout po N barech

Exit codes
----------
    0 -- backtest proběhl
    1 -- chybějící credentials
    2 -- chyba při fetchování dat
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Optional

import pandas as pd

from bot.config import settings, get_enabled_instruments, get_instrument
from bot.data.market import provider_for, BarRequest
from bot.strategy.scanner import scan as run_scanner, ScannerSignal
from bot.llm.context import assemble_context
from bot.llm.decider import decide as claude_decide, DeciderError
from bot.storage.models import PortfolioState, Decision


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

TradeResult = Literal["hit_tp", "hit_sl", "timeout"]


@dataclass
class TradeOutcome:
    """Výsledek simulace tradu bar-by-bar."""
    result: TradeResult        # hit_tp / hit_sl / timeout
    exit_price: float
    exit_bar: Optional[pd.Timestamp]
    exit_hours: Optional[float]  # kolik hodin od triggeru
    pnl_pct: float             # kladné = zisk, záporné = ztráta

    def is_win(self) -> bool:
        return self.result == "hit_tp"


@dataclass
class BacktestRow:
    symbol: str
    trigger_ts: pd.Timestamp
    filter_name: str
    trigger_price: float
    decision: str          # "enter" / "skip" / "error" / "dry-run"
    confidence: Optional[int]
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    reasoning_short: str
    outcome: Optional[TradeResult]   # hit_tp / hit_sl / timeout / None
    exit_price: Optional[float]
    exit_hours: Optional[float]
    pnl_pct: Optional[float]
    tokens_in: int
    tokens_out: int


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _stub_portfolio() -> PortfolioState:
    return PortfolioState(
        equity_usd=10_000.0,
        open_positions=0,
        daily_pnl_pct=0.0,
        remaining_position_slots=5,
    )


def simulate_trade_outcome(
    primary_df: pd.DataFrame,
    trigger_ts: pd.Timestamp,
    entry: float,
    sl: float,
    tp: float,
    direction: str = "long",
    max_bars: int = 48,
) -> TradeOutcome:
    """Simuluje výsledek tradu bar-by-bar po triggeru.

    Pro každý H1 bar po triggeru zkontroluje high/low:
      - long: high >= TP → win, low <= SL → loss
      - short: low <= TP → win, high >= SL → loss
    První zasažení ukončí trade. Po max_bars barech → timeout (exit na close).

    Parameters
    ----------
    primary_df  : kompletní H1 data (včetně budoucnosti)
    trigger_ts  : čas triggeru
    entry       : vstupní cena
    sl          : stop-loss cena
    tp          : take-profit cena
    direction   : "long" nebo "short"
    max_bars    : maximální počet barů čekání (default 48 = 2 dny)
    """
    future = primary_df[primary_df.index > trigger_ts].head(max_bars)

    for bar_ts, bar in future.iterrows():
        high = float(bar["high"])
        low  = float(bar["low"])
        hours_elapsed = (bar_ts - trigger_ts).total_seconds() / 3600

        if direction == "long":
            # Předpokládáme: v rámci baru může nastat obojí → konzerv. SL má přednost
            if low <= sl:
                return TradeOutcome(
                    result="hit_sl",
                    exit_price=sl,
                    exit_bar=bar_ts,
                    exit_hours=hours_elapsed,
                    pnl_pct=(sl - entry) / entry * 100,
                )
            if high >= tp:
                return TradeOutcome(
                    result="hit_tp",
                    exit_price=tp,
                    exit_bar=bar_ts,
                    exit_hours=hours_elapsed,
                    pnl_pct=(tp - entry) / entry * 100,
                )
        else:  # short
            if high >= sl:
                return TradeOutcome(
                    result="hit_sl",
                    exit_price=sl,
                    exit_bar=bar_ts,
                    exit_hours=hours_elapsed,
                    pnl_pct=(entry - sl) / entry * 100,
                )
            if low <= tp:
                return TradeOutcome(
                    result="hit_tp",
                    exit_price=tp,
                    exit_bar=bar_ts,
                    exit_hours=hours_elapsed,
                    pnl_pct=(entry - tp) / entry * 100,
                )

    # Timeout — exit na close posledního dostupného baru
    if future.empty:
        exit_p = entry
        exit_ts = None
        exit_h = None
    else:
        exit_p = float(future.iloc[-1]["close"])
        exit_ts = future.index[-1]
        exit_h = (exit_ts - trigger_ts).total_seconds() / 3600

    if direction == "long":
        pnl = (exit_p - entry) / entry * 100
    else:
        pnl = (entry - exit_p) / entry * 100

    return TradeOutcome(
        result="timeout",
        exit_price=exit_p,
        exit_bar=exit_ts,
        exit_hours=exit_h,
        pnl_pct=pnl,
    )


def _truncate_at(df: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    """Vrať pouze bary do (včetně) triggeru — simuluje co bot viděl v ten moment."""
    return df[df.index <= ts]


# ─────────────────────────────────────────────────────────────────────────────
# Jeden instrument
# ─────────────────────────────────────────────────────────────────────────────

def backtest_instrument(inst, bars: int, dry_run: bool, max_bars: int = 48) -> list[BacktestRow]:
    provider = provider_for(inst)

    # Stáhni KOMPLETNÍ historická data (potřebujeme i budoucnost pro outcome)
    primary_full = provider.fetch_bars(BarRequest(
        symbol=inst.symbol,
        timeframe=inst.timeframe_primary,
        limit=bars,
    ))
    context_limit = max(800, bars // 4 + 80)
    context_full = provider.fetch_bars(BarRequest(
        symbol=inst.symbol,
        timeframe=inst.timeframe_context,
        limit=context_limit,
    ))

    if primary_full.empty:
        print(f"  ! {inst.symbol}: žádná data")
        return []

    # Najdi všechny triggery
    signals = run_scanner(primary_full, context_full)
    if not signals:
        print(f"  ! {inst.symbol}: žádné triggery v {bars} barech")
        return []

    print(f"  {inst.symbol}: {len(signals)} triggerů, volám Claude...")

    rows: list[BacktestRow] = []
    portfolio = _stub_portfolio()

    for i, sig in enumerate(signals):
        print(f"    [{i+1}/{len(signals)}] {sig.timestamp.strftime('%Y-%m-%d %H:%M')} "
              f"{sig.filter} @ {sig.price:.2f}", end="", flush=True)

        if dry_run:
            print(" [dry-run, skip Claude]")
            rows.append(BacktestRow(
                symbol=inst.symbol,
                trigger_ts=sig.timestamp,
                filter_name=sig.filter,
                trigger_price=sig.price,
                decision="dry-run",
                confidence=None,
                entry_price=None,
                stop_loss=None,
                take_profit=None,
                reasoning_short="dry-run",
                outcome=None,
                exit_price=None,
                exit_hours=None,
                pnl_pct=None,
                tokens_in=0,
                tokens_out=0,
            ))
            continue

        # Kontext TAK JAK BOT VIDĚL V TEN MOMENT (truncate na trigger timestamp)
        primary_at_trigger = _truncate_at(primary_full, sig.timestamp)
        context_at_trigger = _truncate_at(context_full, sig.timestamp)

        try:
            ctx = assemble_context(
                instrument=inst.symbol,
                primary_df=primary_at_trigger,
                context_df=context_at_trigger,
                trigger=sig,
                portfolio=portfolio,
                as_of=sig.timestamp.to_pydatetime(),
                max_primary_bars=30,
                max_context_bars=30,
            )
            result = claude_decide(ctx)
            d = result.decision

            trade: Optional[TradeOutcome] = None
            if d.decision == "enter" and d.entry_price and d.stop_loss and d.take_profit:
                direction = d.direction.value if d.direction else "long"
                trade = simulate_trade_outcome(
                    primary_df=primary_full,
                    trigger_ts=sig.timestamp,
                    entry=float(d.entry_price),
                    sl=float(d.stop_loss),
                    tp=float(d.take_profit),
                    direction=direction,
                    max_bars=max_bars,
                )

            reasoning_short = (d.reasoning or "")[:80].replace("\n", " ")
            trade_info = ""
            if trade:
                trade_info = f" [{trade.result} {trade.pnl_pct:+.2f}% in {trade.exit_hours:.0f}h]"
            print(f" → {d.decision.upper()} conf={d.confidence}"
                  f"{trade_info} tokens={result.input_tokens}+{result.output_tokens}")

            rows.append(BacktestRow(
                symbol=inst.symbol,
                trigger_ts=sig.timestamp,
                filter_name=sig.filter,
                trigger_price=sig.price,
                decision=d.decision,
                confidence=d.confidence,
                entry_price=float(d.entry_price) if d.entry_price else None,
                stop_loss=float(d.stop_loss) if d.stop_loss else None,
                take_profit=float(d.take_profit) if d.take_profit else None,
                reasoning_short=reasoning_short,
                outcome=trade.result if trade else None,
                exit_price=trade.exit_price if trade else None,
                exit_hours=trade.exit_hours if trade else None,
                pnl_pct=trade.pnl_pct if trade else None,
                tokens_in=result.input_tokens,
                tokens_out=result.output_tokens,
            ))

        except DeciderError as exc:
            print(f" → ERROR: {exc}")
            rows.append(BacktestRow(
                symbol=inst.symbol,
                trigger_ts=sig.timestamp,
                filter_name=sig.filter,
                trigger_price=sig.price,
                decision="error",
                confidence=None,
                entry_price=None,
                stop_loss=None,
                take_profit=None,
                reasoning_short=str(exc)[:80],
                outcome=None,
                exit_price=None,
                exit_hours=None,
                pnl_pct=None,
                tokens_in=0,
                tokens_out=0,
            ))

        # Rate limit ochrana — Anthropic má limity na RPM
        time.sleep(0.5)

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Výstup
# ─────────────────────────────────────────────────────────────────────────────

def _print_summary(rows: list[BacktestRow]) -> None:
    enters = [r for r in rows if r.decision == "enter"]
    skips  = [r for r in rows if r.decision == "skip"]
    errors = [r for r in rows if r.decision == "error"]

    print()
    print("=" * 80)
    print("  BACKTEST VÝSLEDKY")
    print("=" * 80)
    print(f"  Celkem triggerů:  {len(rows)}")
    print(f"  Enter:            {len(enters)}  ({len(enters)/max(len(rows),1)*100:.0f}%)")
    print(f"  Skip:             {len(skips)}  ({len(skips)/max(len(rows),1)*100:.0f}%)")
    print(f"  Error:            {len(errors)}")

    tokens_in  = sum(r.tokens_in for r in rows)
    tokens_out = sum(r.tokens_out for r in rows)
    cost_est = (tokens_in / 1_000_000 * 3.0) + (tokens_out / 1_000_000 * 15.0)
    print(f"  Tokeny:           {tokens_in}+{tokens_out} (~${cost_est:.2f})")

    # PnL přehled pro enter signály s SL/TP simulací
    enters_with_trade = [r for r in enters if r.pnl_pct is not None]
    if enters_with_trade:
        wins    = [r for r in enters_with_trade if r.outcome == "hit_tp"]
        losses  = [r for r in enters_with_trade if r.outcome == "hit_sl"]
        timeout = [r for r in enters_with_trade if r.outcome == "timeout"]

        avg_pnl   = sum(r.pnl_pct for r in enters_with_trade) / len(enters_with_trade)
        avg_win   = sum(r.pnl_pct for r in wins)   / max(len(wins), 1)
        avg_loss  = sum(r.pnl_pct for r in losses) / max(len(losses), 1)
        avg_hours = sum(r.exit_hours for r in enters_with_trade if r.exit_hours) / max(len(enters_with_trade), 1)

        print()
        print(f"  SL/TP simulace (bar-by-bar, long, max 48h timeout):")
        print(f"    Trades:         {len(enters_with_trade)}")
        print(f"    TP zasaženo:    {len(wins)}  ({len(wins)/max(len(enters_with_trade),1)*100:.0f}%)")
        print(f"    SL zasaženo:    {len(losses)}  ({len(losses)/max(len(enters_with_trade),1)*100:.0f}%)")
        print(f"    Timeout:        {len(timeout)}")
        print(f"    Průměrný PnL:   {avg_pnl:+.2f}%")
        print(f"    Průměrný win:   {avg_win:+.2f}%")
        print(f"    Průměrná loss:  {avg_loss:+.2f}%")
        print(f"    Průměrný čas:   {avg_hours:.1f}h")

        # Expectancy = win_rate * avg_win + loss_rate * avg_loss
        win_rate = len(wins) / len(enters_with_trade)
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
        print(f"    Expectancy:     {expectancy:+.2f}%  "
              f"({'✓ pozitivní' if expectancy > 0 else '✗ negativní'})")

    # Detail tabulka
    print()
    print(f"  {'Čas':<17} {'Symbol':<7} {'Filtr':<20} {'Cena':>8} "
          f"{'Dec':<6} {'C':>2} {'Outcome':<9} {'PnL':>7} {'Čas':>5}")
    print(f"  {'-'*17} {'-'*7} {'-'*20} {'-'*8} {'-'*6} {'-'*2} {'-'*9} {'-'*7} {'-'*5}")
    for r in rows:
        pnl_str     = f"{r.pnl_pct:+.2f}%"  if r.pnl_pct     is not None else "    N/A"
        conf_str    = str(r.confidence)       if r.confidence  is not None else " -"
        outcome_str = r.outcome or "-"        if r.decision == "enter" else "-"
        hours_str   = f"{r.exit_hours:.0f}h"  if r.exit_hours is not None else "  -"
        print(f"  {r.trigger_ts.strftime('%Y-%m-%d %H:%M'):<17} "
              f"{r.symbol:<7} {r.filter_name:<20} {r.trigger_price:>8.2f} "
              f"{r.decision:<6} {conf_str:>2} {outcome_str:<9} {pnl_str:>7} {hours_str:>5}")


def _save_csv(rows: list[BacktestRow], path: str) -> None:
    fields = [
        "symbol", "trigger_ts", "filter_name", "trigger_price",
        "decision", "confidence", "entry_price", "stop_loss", "take_profit",
        "outcome", "exit_price", "exit_hours", "pnl_pct",
        "tokens_in", "tokens_out", "reasoning_short",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({
                "symbol": r.symbol,
                "trigger_ts": r.trigger_ts.isoformat(),
                "filter_name": r.filter_name,
                "trigger_price": r.trigger_price,
                "decision": r.decision,
                "confidence": r.confidence,
                "entry_price": r.entry_price,
                "stop_loss": r.stop_loss,
                "take_profit": r.take_profit,
                "outcome": r.outcome,
                "exit_price": r.exit_price,
                "exit_hours": r.exit_hours,
                "pnl_pct": r.pnl_pct,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "reasoning_short": r.reasoning_short,
            })
    print(f"\n  CSV uložen: {path}  ({len(rows)} řádků)")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Claude backtest na historických triggerech")
    p.add_argument("--bars", type=int, default=500,
                   help="Počet H1 barů k backtestování (default 500 ~= 3 týdny)")
    p.add_argument("--symbol", default=None,
                   help="Testuj jen tento symbol (např. BTC/USD, NVDA)")
    p.add_argument("--csv", default=None,
                   help="Ulož výsledky do CSV souboru")
    p.add_argument("--dry-run", action="store_true",
                   help="Bez Claude API — jen zobraz triggery a outcome")
    p.add_argument("--max-bars", type=int, default=48,
                   help="Max H1 barů pro SL/TP simulaci (default 48 = 2 dny)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 72)
    print("  AI Signal Bot — Claude Backtest")
    print(f"  H1 barů: {args.bars}  |  dry-run: {args.dry_run}")
    print("=" * 72)

    # Ověř credentials
    missing = settings.required_for_data()
    if missing:
        print(f"\n  CHYBA: Chybějící env vars: {missing}")
        return 1
    if not args.dry_run and not settings.anthropic_api_key:
        print("\n  CHYBA: ANTHROPIC_API_KEY chybí (použij --dry-run pro test bez Claude)")
        return 1

    # Vyber instrumenty
    if args.symbol:
        inst = get_instrument(args.symbol)
        if inst is None:
            print(f"  CHYBA: Symbol {args.symbol!r} nenalezen")
            return 1
        instruments = [inst]
    else:
        instruments = get_enabled_instruments()

    all_rows: list[BacktestRow] = []

    for inst in instruments:
        print()
        print(f"{'=' * 72}")
        print(f"  [{inst.symbol}]  ({inst.kind})")
        print(f"{'─' * 72}")
        try:
            rows = backtest_instrument(inst, args.bars, args.dry_run, args.max_bars)
            all_rows.extend(rows)
        except Exception as exc:
            print(f"  CHYBA: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    if not all_rows:
        print("\n  Žádné výsledky.")
        return 2

    _print_summary(all_rows)

    if args.csv:
        _save_csv(all_rows, args.csv)

    return 0


if __name__ == "__main__":
    sys.exit(main())
