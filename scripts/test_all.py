"""
Kompletní lokální test bota — projde každý aktivní instrument a otestuje:

  1. CONFIG       — načtení instrumentů ze seznamu
  2. DATA         — stažení OHLCV dat z Alpaca (crypto i stock)
  3. INDICATORS   — výpočet EMA, ATR, RSI na reálných datech
  4. SCANNER      — technické filtry, počet triggerů
  5. NEWS         — stažení zpráv a filtrování dle klíčových slov instrumentu
  6. SENTIMENT    — Fear & Greed Index (jen pro crypto, ale zobrazíme vždy)
  7. CLAUDE       — sestavení kontextu + volání Claude API (volitelné)
  8. RISK         — risk manager na fiktivním signálu

Nic se neposílá na Telegram. Nic se neprovede v Alpaca.
Vhodné pro lokální ověření před deploym na server.

Použití
-------
    # Základní test (bez Claude API — šetříme tokeny):
    python -m scripts.test_all

    # Plný test včetně Claude pro první instrument:
    python -m scripts.test_all --claude

    # Plný test Claude pro všechny instrumenty:
    python -m scripts.test_all --claude --all-claude

    # Jen jeden konkrétní symbol:
    python -m scripts.test_all --symbol NVDA

    # Verbose — zobrazí detaily každého kroku:
    python -m scripts.test_all --verbose
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Terminálové barvy (fungují na Linux i Windows 10+)
# ─────────────────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

OK   = f"{GREEN}✓{RESET}"
WARN = f"{YELLOW}!{RESET}"
FAIL = f"{RED}✗{RESET}"
INFO = f"{CYAN}▸{RESET}"


def ok(msg: str) -> str:   return f"  {OK}  {msg}"
def warn(msg: str) -> str: return f"  {WARN}  {msg}"
def fail(msg: str) -> str: return f"  {FAIL}  {msg}"
def info(msg: str) -> str: return f"  {INFO}  {msg}"


def section(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * max(0, 60 - len(title))}{RESET}")


def header(title: str) -> None:
    width = 70
    print()
    print(f"{BOLD}{'═' * width}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{BOLD}{'═' * width}{RESET}")


# ─────────────────────────────────────────────────────────────────────────────
# Výsledky
# ─────────────────────────────────────────────────────────────────────────────

class Results:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.checks: list[tuple[str, bool, str]] = []  # (name, passed, detail)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append((name, passed, detail))
        icon = OK if passed else FAIL
        line = f"  {icon}  {name}"
        if detail:
            line += f"  — {detail}"
        print(line)

    def add_warn(self, name: str, detail: str = "") -> None:
        self.checks.append((name, True, detail))
        line = f"  {WARN}  {name}"
        if detail:
            line += f"  — {detail}"
        print(line)

    @property
    def passed(self) -> int:
        return sum(1 for _, p, _ in self.checks if p)

    @property
    def failed(self) -> int:
        return sum(1 for _, p, _ in self.checks if not p)


# ─────────────────────────────────────────────────────────────────────────────
# Test kroky
# ─────────────────────────────────────────────────────────────────────────────

def test_data(inst, verbose: bool) -> tuple[Any, Any, Results]:
    """Stáhne OHLCV data pro daný instrument. Vrátí (primary_df, context_df, results)."""
    from bot.data.market import provider_for, BarRequest

    res = Results(inst.symbol)
    section(f"DATA — {inst.symbol} ({inst.kind})")

    primary = context = None
    try:
        prov = provider_for(inst)
        primary = prov.fetch_bars(BarRequest(
            symbol=inst.symbol,
            timeframe=inst.timeframe_primary,
            limit=200,
        ))
        context = prov.fetch_bars(BarRequest(
            symbol=inst.symbol,
            timeframe=inst.timeframe_context,
            limit=200,
        ))
    except Exception as exc:
        res.add("Fetch bars", False, f"{exc.__class__.__name__}: {exc}")
        if verbose:
            traceback.print_exc()
        return primary, context, res

    # Primary timeframe
    if primary is not None and not primary.empty:
        last_close = primary["close"].iloc[-1]
        date_from = primary.index[0].strftime("%Y-%m-%d")
        date_to = primary.index[-1].strftime("%Y-%m-%d")
        res.add(
            f"Primary {inst.timeframe_primary}",
            True,
            f"{len(primary)} barů  [{date_from} → {date_to}]  last_close={last_close:.4f}",
        )
        if verbose:
            print(f"\n  Poslední 3 bary ({inst.timeframe_primary}):")
            for ts, row in primary.tail(3).iterrows():
                print(f"    {ts.strftime('%Y-%m-%d %H:%M')}  "
                      f"O={row['open']:.4f}  H={row['high']:.4f}  "
                      f"L={row['low']:.4f}  C={row['close']:.4f}  "
                      f"V={row['volume']:.2f}")
    else:
        res.add(f"Primary {inst.timeframe_primary}", False, "prázdný DataFrame")

    # Context timeframe
    if context is not None and not context.empty:
        res.add(
            f"Context {inst.timeframe_context}",
            True,
            f"{len(context)} barů  last_close={context['close'].iloc[-1]:.4f}",
        )
    else:
        res.add(f"Context {inst.timeframe_context}", False, "prázdný DataFrame")

    return primary, context, res


def test_indicators(primary_df, inst, verbose: bool) -> Results:
    """Spočítá indikátory na reálných datech."""
    from bot.strategy import indicators as ind

    res = Results(inst.symbol)
    section(f"INDICATORS — {inst.symbol}")

    if primary_df is None or primary_df.empty:
        res.add("Indikátory", False, "žádná data — přeskočeno")
        return res

    try:
        ema20 = ind.ema(primary_df["close"], 20)
        ema50 = ind.ema(primary_df["close"], 50)
        atr14 = ind.atr(primary_df, 14)
        vol_ma20 = ind.volume_ma(primary_df["volume"], 20)

        last_close = primary_df["close"].iloc[-1]
        last_ema20 = ema20.iloc[-1]
        last_ema50 = ema50.iloc[-1]
        last_atr   = atr14.iloc[-1]
        last_volma = vol_ma20.iloc[-1]

        res.add("EMA20", True, f"{last_ema20:.4f}  (close {'>' if last_close > last_ema20 else '<'} EMA20)")
        res.add("EMA50", True, f"{last_ema50:.4f}  (EMA20 {'>' if last_ema20 > last_ema50 else '<'} EMA50)")
        atr_pct = (last_atr / last_close * 100) if last_close else 0
        res.add("ATR14", True, f"{last_atr:.4f}  ({atr_pct:.2f}% of price)")
        res.add("VolMA20", True, f"{last_volma:.2f}")

    except Exception as exc:
        res.add("Indikátory", False, f"{exc.__class__.__name__}: {exc}")
        if verbose:
            traceback.print_exc()

    return res


def test_scanner(primary_df, context_df, inst, verbose: bool) -> tuple[Any, Results]:
    """Spustí scanner filtry a vrátí (last_signal, results)."""
    from bot.strategy.scanner import scan

    res = Results(inst.symbol)
    section(f"SCANNER — {inst.symbol}")

    last_signal = None
    if primary_df is None or primary_df.empty or context_df is None or context_df.empty:
        res.add("Scanner", False, "žádná data — přeskočeno")
        return last_signal, res

    try:
        signals = scan(primary_df, context_df)
        last_signal = signals[-1] if signals else None
        as_of = datetime.now(timezone.utc)

        res.add("Scanner spuštěn", True, f"celkem {len(signals)} historických triggerů")

        if last_signal:
            age = as_of - last_signal.timestamp.to_pydatetime()
            age_h = age.total_seconds() / 3600
            fresh = age_h < 1.5
            freshness = f"{'ČERSTVÝ' if fresh else 'starý'} ({age_h:.1f}h)"
            res.add(
                f"Poslední signál",
                True,
                f"filter={last_signal.filter}  price={last_signal.price:.4f}  {freshness}",
            )
            if verbose:
                print(f"\n  Kontext signálu:")
                for k, v in last_signal.context.items():
                    print(f"    {k}: {v:.4f}" if isinstance(v, float) else f"    {k}: {v}")
        else:
            res.add_warn("Poslední signál", "žádné triggery v datech (scanner nevidí vhodný setup)")

    except Exception as exc:
        res.add("Scanner", False, f"{exc.__class__.__name__}: {exc}")
        if verbose:
            traceback.print_exc()

    return last_signal, res


def test_news(inst, verbose: bool) -> tuple[list, Results]:
    """Stáhne zprávy a filtruje dle klíčových slov instrumentu."""
    res = Results(inst.symbol)
    section(f"NEWS — {inst.symbol}")

    news_items = []
    try:
        from bot.data.news import fetch_news, FEEDS

        # Stáhneme zprávy — ale filtrujeme dle klíčových slov TOHOTO instrumentu
        # (ne jen BTC klíčová slova jako v původním news.py)
        import feedparser
        from bot.storage.models import NewsItem
        from bot.data.news import _entry_timestamp

        keywords = inst.news_keywords
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

        # Pro stock instrumenty zkusíme i Yahoo Finance RSS
        feeds_to_check = list(FEEDS)
        if inst.kind == "stock":
            ticker = inst.symbol.replace("/", "-")
            feeds_to_check += [
                ("Yahoo Finance", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"),
                ("Seeking Alpha", f"https://seekingalpha.com/api/sa/combined/{ticker}.xml"),
            ]

        raw_items: list[NewsItem] = []
        feed_results: list[str] = []

        for source_name, url in feeds_to_check:
            try:
                parsed = feedparser.parse(url)
                count_before = len(raw_items)
                for entry in parsed.entries:
                    ts = _entry_timestamp(entry)
                    if ts is None or ts < cutoff:
                        continue
                    title = getattr(entry, "title", "").strip()
                    summary = getattr(entry, "summary", "").strip()
                    url_ = getattr(entry, "link", "").strip()
                    if not title:
                        continue

                    # Filtr dle klíčových slov instrumentu
                    blob = f" {title.lower()} {summary.lower()} "
                    if keywords and not any(kw.lower() in blob for kw in keywords):
                        continue

                    raw_items.append(NewsItem(
                        timestamp=ts,
                        source=source_name,
                        title=title[:256],
                        summary=summary[:512],
                        url=url_[:512],
                    ))
                found = len(raw_items) - count_before
                feed_results.append(f"{source_name}: {found}")
            except Exception as exc:
                feed_results.append(f"{source_name}: chyba ({exc.__class__.__name__})")

        raw_items.sort(key=lambda n: n.timestamp, reverse=True)
        news_items = raw_items[:15]

        res.add(
            "Feeds otestovány",
            True,
            "  |  ".join(feed_results),
        )
        res.add(
            "Relevantní zprávy (48h)",
            len(news_items) > 0,
            f"{len(news_items)} zpráv dle klíčových slov {list(keywords[:3])}{'...' if len(keywords) > 3 else ''}",
        )

        if verbose and news_items:
            print(f"\n  Poslední zprávy:")
            for item in news_items[:5]:
                print(f"    [{item.source}] {item.title[:80]}")

    except Exception as exc:
        res.add("News fetch", False, f"{exc.__class__.__name__}: {exc}")
        if verbose:
            traceback.print_exc()

    return news_items, res


def test_sentiment(verbose: bool) -> tuple[Any, Results]:
    """Stáhne Fear & Greed Index."""
    res = Results("global")
    section("SENTIMENT — Fear & Greed Index")

    sentiment = None
    try:
        from bot.data.sentiment import fetch_fear_greed
        sentiment = fetch_fear_greed(history_days=7)

        # Trend: rostoucí nebo klesající?
        trend_dir = ""
        if len(sentiment.trend_7d) >= 2:
            delta = sentiment.trend_7d[-1] - sentiment.trend_7d[0]
            trend_dir = f"  trend 7d: {'↑' if delta > 0 else '↓'} ({delta:+d})"

        res.add(
            "Fear & Greed",
            True,
            f"hodnota={sentiment.value}  klasifikace='{sentiment.classification}'{trend_dir}",
        )
        if verbose:
            print(f"  Trend (7 dní): {sentiment.trend_7d}")

    except Exception as exc:
        res.add_warn("Fear & Greed", f"nedostupný: {exc.__class__.__name__}: {exc}")

    return sentiment, res


def test_claude(inst, primary_df, context_df, last_signal, news_items, sentiment, verbose: bool) -> Results:
    """Sestaví kontext a zavolá Claude API."""
    from bot.config import settings

    res = Results(inst.symbol)
    section(f"CLAUDE — {inst.symbol}")

    if not settings.anthropic_api_key:
        res.add("Anthropic API key", False, "chybí ANTHROPIC_API_KEY v .env")
        return res

    if primary_df is None or primary_df.empty:
        res.add("Claude kontext", False, "žádná data — přeskočeno")
        return res

    try:
        from bot.storage.models import PortfolioState
        from bot.llm.context import assemble_context, render_context_for_prompt
        from bot.llm.prompts import active_prompt
        from bot.strategy.scanner import ScannerSignal
        from bot.data import calendar as macro_cal

        as_of = datetime.now(timezone.utc)

        # Použij reálný signál nebo pseudo-trigger z posledního baru
        if last_signal and (as_of - last_signal.timestamp.to_pydatetime()) < timedelta(hours=6):
            sig = last_signal
            sig_kind = "reálný"
        else:
            last_bar = primary_df.iloc[-1]
            sig = ScannerSignal(
                timestamp=primary_df.index[-1],
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
            sig_kind = "pseudo (žádný čerstvý trigger)"

        res.add("Trigger", True, f"{sig_kind}  filter={sig.filter}  price={sig.price:.4f}")

        macro_recent = macro_cal.recent_within(12, now=as_of)
        macro_upcoming = macro_cal.upcoming_within(12, now=as_of)

        portfolio = PortfolioState(
            equity_usd=10_000.0, open_positions=0,
            daily_pnl_pct=0.0, remaining_position_slots=settings.max_open_positions,
        )

        ctx = assemble_context(
            instrument=inst.symbol,
            primary_df=primary_df,
            context_df=context_df,
            trigger=sig,
            news=news_items,
            sentiment=sentiment,
            macro_recent=macro_recent,
            macro_upcoming=macro_upcoming,
            portfolio=portfolio,
            as_of=as_of,
            max_primary_bars=30,
            max_context_bars=30,
        )

        prompt_version, system_text, prompt_h = active_prompt()
        user_text = render_context_for_prompt(ctx)
        res.add(
            "Kontext sestaven",
            True,
            f"prompt v={prompt_version}  system={len(system_text)}ch  user={len(user_text)}ch  "
            f"news={len(news_items)}  macro_recent={len(macro_recent)}",
        )

        # Volání Claude
        print(info("Volám Claude API…"))
        from bot.llm.decider import decide
        result = decide(ctx)
        d = result.decision

        # Cena
        prices = {
            "claude-sonnet-4": (3.0, 15.0),
            "claude-opus-4":   (15.0, 75.0),
            "claude-haiku-4":  (0.8, 4.0),
        }
        cost = 0.0
        for prefix, (pi, po) in prices.items():
            if result.model.startswith(prefix):
                cost = (result.input_tokens * pi + result.output_tokens * po) / 1_000_000
                break

        decision_str = d.decision.upper()
        if d.direction:
            decision_str += f" {d.direction.value.upper()}"

        res.add(
            "Claude rozhodnutí",
            True,
            f"{decision_str}  confidence={d.confidence}/10  "
            f"latency={result.latency_ms}ms  "
            f"tokens={result.input_tokens}in+{result.output_tokens}out  "
            f"cost≈${cost:.4f}",
        )

        if verbose or True:  # vždy zobrazit reasoning
            print(f"\n  {BOLD}Reasoning:{RESET}")
            print(f"    {d.reasoning}")
            if d.key_risks:
                print(f"\n  {BOLD}Key risks:{RESET}")
                for r in d.key_risks:
                    print(f"    - {r}")
            if d.decision == "enter":
                rr = d.risk_reward_ratio()
                print(f"\n  {BOLD}Trade parametry:{RESET}")
                print(f"    Entry={d.entry_price:.4f}  SL={d.stop_loss:.4f}  TP={d.take_profit:.4f}  R:R={rr:.2f}")

        # Risk manager
        from bot.risk.manager import evaluate as risk_evaluate
        verdict = risk_evaluate(d, portfolio, sig, now=as_of)
        res.add(
            "Risk manager",
            True,
            f"{'SCHVÁLENO' if verdict.approved else 'ZAMÍTNUTO'}  {verdict.reason[:80]}",
        )

    except Exception as exc:
        res.add("Claude", False, f"{exc.__class__.__name__}: {exc}")
        if verbose:
            traceback.print_exc()

    return res


# ─────────────────────────────────────────────────────────────────────────────
# Hlavní funkce
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Kompletní lokální test bota pro všechny instrumenty",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--symbol", "-s", default=None,
                   help="Testuj jen tento symbol (např. NVDA)")
    p.add_argument("--claude", action="store_true",
                   help="Zavolej Claude API pro první instrument (platí tokeny!)")
    p.add_argument("--all-claude", action="store_true",
                   help="Zavolej Claude API pro VŠECHNY instrumenty")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Zobraz detaily (poslední bary, news titulky, …)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    header("AI Trading Bot — kompletní lokální test")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  verbose={'ano' if args.verbose else 'ne'}")

    # ── CONFIG ────────────────────────────────────────────────────────────
    section("CONFIG")
    from bot.config import get_enabled_instruments, get_instrument, settings

    print(info(f"Mode: {settings.mode.value}"))
    print(info(f"Model: {settings.anthropic_model}"))
    print(info(f"Alpaca paper: {settings.alpaca_paper}"))

    # Secrets
    missing_data = settings.required_for_data()
    if missing_data:
        print(fail(f"Chybí klíče pro Alpaca: {missing_data}"))
        print(f"\n  Nastav je v .env a zkus znovu.")
        return 1
    else:
        print(ok("Alpaca API klíče: nalezeny"))

    if settings.anthropic_api_key:
        print(ok("Anthropic API klíč: nalezen"))
    else:
        print(warn("Anthropic API klíč: chybí (Claude testy přeskočeny, ostatní proběhnou)"))

    # Instrumenty
    all_instruments = get_enabled_instruments()
    if args.symbol:
        inst_filter = get_instrument(args.symbol)
        if inst_filter is None:
            print(fail(f"Symbol '{args.symbol}' nenalezen v INSTRUMENTS seznamu"))
            return 1
        instruments = [inst_filter]
    else:
        instruments = all_instruments

    print(ok(f"Aktivní instrumenty: {len(all_instruments)} celkem, testujeme {len(instruments)}"))
    for i in instruments:
        print(f"      {'●' if i.enabled else '○'}  {i.symbol:10s}  {i.kind:6s}  "
              f"{i.timeframe_primary}/{i.timeframe_context}  "
              f"keywords: {', '.join(i.news_keywords[:3])}{'…' if len(i.news_keywords) > 3 else ''}")

    # ── SENTIMENT (jednou pro všechny) ────────────────────────────────────
    sentiment, sent_res = test_sentiment(args.verbose)

    # ── PER-INSTRUMENT ────────────────────────────────────────────────────
    all_results: list[Results] = []
    claude_called = 0

    for idx, inst in enumerate(instruments):
        header(f"Instrument {idx+1}/{len(instruments)}: {inst.symbol} ({inst.kind.upper()})")

        # Data
        primary_df, context_df, data_res = test_data(inst, args.verbose)
        all_results.append(data_res)

        # Indikátory
        ind_res = test_indicators(primary_df, inst, args.verbose)
        all_results.append(ind_res)

        # Scanner
        last_signal, scan_res = test_scanner(primary_df, context_df, inst, args.verbose)
        all_results.append(scan_res)

        # News
        news_items, news_res = test_news(inst, args.verbose)
        all_results.append(news_res)

        # Claude (volitelné)
        call_claude = (
            settings.anthropic_api_key
            and (
                args.all_claude
                or (args.claude and claude_called == 0)
            )
        )
        if call_claude:
            claude_res = test_claude(
                inst, primary_df, context_df, last_signal, news_items, sentiment, args.verbose
            )
            all_results.append(claude_res)
            claude_called += 1
        else:
            if not settings.anthropic_api_key:
                reason = "chybí ANTHROPIC_API_KEY"
            elif not args.claude and not args.all_claude:
                reason = "přidej --claude pro test Claude API"
            else:
                reason = "přidej --all-claude pro test všech instrumentů"
            section(f"CLAUDE — {inst.symbol}")
            print(warn(f"Přeskočeno ({reason})"))

    # ── SOUHRNNÝ VÝSLEDEK ─────────────────────────────────────────────────
    header("SOUHRNNÝ VÝSLEDEK")

    total_pass = sum(r.passed for r in all_results)
    total_fail = sum(r.failed for r in all_results)
    total = total_pass + total_fail

    print(f"\n  Celkem kontrol:  {total}")
    print(f"  {GREEN}Prošlo:{RESET}          {total_pass}")
    print(f"  {RED}Selhalo:{RESET}         {total_fail}")

    if total_fail == 0:
        print(f"\n  {GREEN}{BOLD}✓ Vše v pořádku — bot je připraven.{RESET}")
    else:
        print(f"\n  {RED}{BOLD}✗ Některé kontroly selhaly — viz výpis výše.{RESET}")

    if not args.claude and not args.all_claude:
        print(f"\n  {YELLOW}Tip: Claude API nebyl otestován. Spusť s --claude pro plný test.{RESET}")

    print()
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
