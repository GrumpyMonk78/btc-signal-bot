# Changelog — AI Signal Bot

> **Pravidlo:** Každá změna kódu musí být zapsána sem PŘED commitem.
> Formát: datum, soubor(y), co a proč se změnilo.

---

## 2026-06-19 — Bugfix: krypto nepodporuje bracket order (OTOCO)

**Soubory:**
- `bot/execution/alpaca.py` — `_submit_bracket_order()` rozděleno na dvě větve:
  krypto = jednoduchý market order, akcie = bracket order (SL+TP)

**Co se mění:**
Alpaca vrací `crypto orders not allowed for advanced order_class: otoco` při pokusu
o bracket order na BTC/USD. Pro krypto nyní posíláme jednoduchý market order bez
SL/TP — time exit a progress check zajišťuje `position_monitor` každou hodinu.
Pro akcie zůstává bracket order beze změny.

**Proč:**
Alpaca paper API neumožňuje OTOCO (bracket) ordery pro krypto instrumenty.
BTC/USD short signal přišel 19.6. 13:00 UTC, Claude schválil (conf=6), Telegram
odeslán, ale order selhal s kódem 42210000.

---

## 2026-06-14 — Bugfix: ScannerTrigger Literal chybí short filtry

**Soubory:**
- `bot/storage/models.py` — přidány short varianty do `ScannerTrigger.filter` Literal

**Co se mění:**
Pydantic model `ScannerTrigger` měl v poli `filter` pouze long varianty:
`"ema_pullback", "breakout_atr", "volume_absorption"`. Když scanner od 4. června
začal nacházet short setupy (`ema_pullback_short` atd.), pipeline crashovala s
`ValidationError` a Claude nebyl volán. Bot tiše přeskakoval všechny short triggery
po dobu 10 dní. Opraveno přidáním všech 6 variant do Literal.

**Proč:**
Short filtry byly přidány do scanneru (fáze 2), ale `models.py` nebyl aktualizován.
Chyba se projevila až když trh přešel do downtrendu a scanner začal generovat
výhradně short signály.

---

## 2026-06-14 — Školní projekt JEM207: README + Jupyter notebook + analysis/

**Soubory:**
- `README.md` — přepsán pro akademické publikum (JEM207). Popis architektury, instrumentů, data sources, scanner filtrů, Claude integrace, risk manageru, backtestu a instalace.
- `analysis/backtest_analysis.ipynb` — nový Jupyter notebook s vizualizacemi: decision distribution, per-instrument breakdown, PnL distribution, cumulative returns, filter analysis, confidence analysis, hold time, token usage, dashboard.

**Co se mění:**
Přidány dokumenty pro odevzdání školního projektu. Žádný kód bota se nemění — pouze README a analýza.

**Proč:**
Deadline JEM207 — Data Processing in Python: 14. 6. 2026.

---

## 2026-06-02 — No duplicate position check v execute_signal()

**Soubory:**
- `bot/execution/alpaca.py` — přidán check před odesláním orderu: pokud symbol už má otevřenou pozici na Alpaca, order se nepošle a vrátí se ExecutionResult(submitted=False, error="duplicate_position")

**Co se mění:**
Před každým bracket orderem (paper i live) se stáhnou aktuální pozice z Alpaca. Pokud symbol už je otevřen, order se zamítne s logem a telegram se nepošle. Shadow mode není dotčen.

**Proč:**
IONQ se triggeroval 5× za 4 dny. Při otevřené pozici bot posílal nový order na stejný symbol — způsobilo PDT violation a zbytečné zdvojení expozice.

---

## 2026-05-28 — Position monitor: automatický time exit po 12h

**Soubory:**
- `bot/execution/position_monitor.py` (nový) — hourly job: projde otevřené pozice na Alpaca, zkontroluje věk pozice a pohyb k TP, pošle market close order pokud 12h bez >=30% pohybu k TP
- `bot/scheduler.py` — přidán job `monitor_positions` každou hodinu na HH:05 UTC

**Co se mění:**
1. Každou hodinu (5 minut po pipeline) monitor stáhne otevřené pozice z Alpaca.
2. Pro každou pozici zjistí entry čas z DB (tabulka `signals`), entry cenu, SL a TP.
3. Pokud pozice existuje >= 12h A cena nepokročila k TP alespoň 30% vzdálenosti → cancel bracket orders + market sell (nebo buy cover pro short).
4. Víkendy: Alpaca akcie neobchodují v sobotu/neděli — monitor detekuje zavřený trh a přeskočí. Crypto (BTC) obchoduje 24/7 — monitor funguje normálně.
5. Telegram notifikace při time exit.

**Proč:**
Time exit byl implementován jen v backtestové simulaci a v Claude promptu (jako text v `invalidation`). Bot sám žádný close order neposlal — pozice by visela dokud SL nebo TP. IONQ pozice otevřená 28.5. — bez monitoru by mohla viset celý víkend.

---

## 2026-05-28 — Risk manager: short povoleno + H4 indikátory v kontextu

**Soubory:**
- `bot/risk/manager.py` — odstraněn `_check_long_only` (vetoval každý short); nahrazen `_check_direction_matches_h4` — long povoleno jen pokud h4_uptrend=True, short jen pokud h4_downtrend=True
- `bot/strategy/scanner.py` — H4 indikátory přidány do context dict: `h4_ema20`, `h4_ema50`, `h4_rsi14`, `h4_atr14`, `h4_macd`, `h4_macd_signal`, `h4_macd_hist`
- `bot/llm/context.py` — nová sekce `indicators_h4:` v render_context_for_prompt(); Claude vidí hladší H4 signály vedle H1

**Co se mění:**
1. Risk manager přestal vetovat short signály. Nové pravidlo: direction musí odpovídat H4 trendu (long ↔ h4_uptrend, short ↔ h4_downtrend). Pokud H4 trend a direction nesedí → veto `direction_h4_mismatch`.
2. Scanner spočítá EMA20/50, RSI14, ATR14, MACD na H4 datech a přidá je do context dict každého triggeru.
3. Context renderer zobrazí H4 indikátory jako samostatnou sekci v user message pro Claude — hladší hodnoty než H1 pomáhají Claudovi správně vyhodnotit sílu trendu a nastavit SL/TP.

**Proč:**
Risk manager měl hardcoded `_check_long_only` z Phase 1 — každý short signal byl vetován bez ohledu na scanner/Claude. Zároveň Claude dostával jen H1 indikátory (šumivé), ale H4 indikátory (EMA, RSI, ATR) jsou hladší a lépe reflektují silné trendy.

---

## 2026-05-28 — Phase 2+3: Short selling + news váha + obousměrný trend

**Soubory:**
- `bot/strategy/scanner.py` — short filtry: `ema_pullback_short`, `breakout_atr_short`, `volume_absorption_short`; short gate: H4 close < EMA200
- `bot/llm/prompts.py` — `SYSTEM_DECIDER_V4`: short reasoning, news váha (+1 conf při potvrzující zprávě <6h, skip při odporující), few-shot short příklady; `active_prompt()` → v4.0.0
- `bot/storage/models.py` — ověření Direction enum pro short, consistency rules pro short direction
- `bot/execution/alpaca.py` — short bracket order (side="sell", SL/TP opačně)
- `scripts/backtest_claude.py` — short statistiky v `_print_summary()`
- `tests/test_scanner.py` — testy pro short filtry

**Co se mění:**
1. Scanner nyní hledá triggery v OBOU směrech. Long gate: H4 close > EMA200. Short gate: H4 close < EMA200. Nikdy obojí zároveň.
2. Zrcadlové short filtry — stejná logika jako long, jen obráceně: pullback k EMA20 shora, breakdown pod ATR, volume absorption při poklesu.
3. Claude dostane v promptu V4 explicitní short reasoning sekci + pravidla pro news váhu.
4. Zprávy mají větší váhu: relevantní zpráva z posledních 6h potvrzující směr → confidence +1. Zpráva odporující směru → doporučuj skip bez ohledu na techniku.
5. Alpaca execution podporuje short bracket ordery.

**Proč:**
Backtest V6 ukázal TSLA 0% win rate na long (7 trades, 7 losses) — TSLA byl v downtrendu. Bot potřebuje shortovat v bearish prostředí. NVDA a IONQ jsou profitabilní long, ale v korekci by měly shortovat. News váha: živý bot zprávy dostává ale prompt jim nedával dostatečnou váhu.

---

## 2026-05-28 — Time-based exit + budget arg pro backtest

**Soubory:**
- `bot/llm/prompts.py` — SYSTEM_DECIDER_V3.1: přidáno pravidlo dynamického časového exitu (12h)
- `scripts/backtest_claude.py` — `--budget` CLI argument; `simulate_trade_outcome()` max 12h timeout

**Co se změnilo:**
1. Prompt V3.1: přidáno pravidlo "pokud se cena do 12 barů nepohne k TP alespoň o 30% vzdálenosti, pozice ztrácí sílu — doporučuj exit na tržní ceně". Claude to zahrne do `invalidation` pole v JSON.
2. Simulace: `simulate_trade_outcome()` má nový parametr `time_exit_bars=12` — pokud po 12 barech cena nepokročila k TP, simulace vrátí `timeout` s cenou close posledního baru.
3. `--budget` argument: backtest může přepsat výchozí 500k token budget pro delší běhy.

**Proč:**
Backtest V5 ukázal TSLA pozici která visela 71h než zasáhla SL. Time exit by ji uzavřel na break-even nebo malé ztrátě po 12h. Zároveň budget 500k nestačil pro 168 triggerů — TSLA a IONQ se nedostaly na řadu.

---

## 2026-05-28 — Anthropic prompt caching v decider.py

**Soubory:**
- `bot/llm/decider.py` — přidán `cache_control: {"type": "ephemeral"}` na system prompt a few-shot bloky

**Co se změnilo:**
Implementován Anthropic prompt caching (beta feature `prompt-caching-2024-07-31`). System prompt (~7900 tokenů) a few-shot příklady (~500 tokenů) jsou označeny jako cacheable. První volání za hodinu platí plně, všechna další volání se stejným promptem platí jen cache read (90% sleva na input tokeny).

Logování rozšířeno: `usage.cache_creation_input_tokens` a `usage.cache_read_input_tokens` se tisknou do logu pokud jsou nenulové.

**Proč:**
Backtest V4 stál ~$1.51 za 48 triggerů (~$0.031/trigger). S cachingem bude ~$0.003–0.005/trigger (system prompt se cachuje, platí se jen user message ~400–600 tokenů). Při ostrém provozu (scheduler každou hodinu, ~5–10 triggerů/den) = úspora ~90%.

**Efektivita Claude:** beze změny — cached prompt je identický s původním, Claude vidí stejný kontext.

---

## 2026-05-27 — Prompt V3: oprava over-conservative RSI/StochRSI veta

**Soubory:**
- `bot/llm/prompts.py` — SYSTEM_DECIDER_V3, active_prompt() → v3.0.0

**Co se změnilo:**
V2 prompt měl dva problémy které způsobily 0% enter rate:
1. Chybělo pravidlo pro momentum filtry — Claude správně viděl RSI>70 a skipoval, ale u `breakout_atr` a `volume_absorption` je RSI>70 normální součást setupu, ne veto.
2. "Default is SKIP" + "conf 4-5 = SKIP" bylo příliš přísné — bez live news je conf vždy 4-5, takže bot nikdy neobchoduje.
Nové pravidlo: u `breakout_atr` a `volume_absorption` triggerů RSI/StochRSI slouží jako vstupní timing (ne jako veto). Conf 6 je dostatečné pro vstup pokud technické podmínky sedí.

---

## 2026-05-27 — Nové indikátory: BB, VWAP, RSI divergence, Stoch RSI, OBV, rel. síla

**Soubory:**
- `bot/strategy/indicators.py` — nové funkce: bollinger_bands, vwap, stoch_rsi, obv, rsi_divergence, ema200 (H4)
- `bot/strategy/scanner.py` — nové indikátory přidány do context dict pro každý trigger
- `bot/llm/context.py` — render_context_for_prompt: nová sekce "indicators" s BB, VWAP, Stoch RSI, OBV, RSI div, rel. síla

**Co se přidává:**
1. **Bollinger Bands (20,2)** — BB%B (kde je cena v pásmu), BB šíře (squeeze = nízká volatilita před breakoutem)
2. **VWAP** — intraday volume-weighted average price; klíčový level pro US stocks
3. **Stochastic RSI** — %K a %D; citlivější než RSI samotný, lepší timing
4. **OBV (On-Balance Volume)** — kumulativní potvrzení volumem; OBV trend potvrzuje/vyvrací cenový trend
5. **RSI divergence** — bullish divergence (cena lower low, RSI higher low) = silný reversal signal
6. **EMA200 na H1** — dlouhodobá podpora/odpor přímo na H1 timeframe (H4 EMA200 je gate, H1 EMA200 je kontext)

**Proč:**
Claude dostával jen ATR, EMA20/50, RSI, MACD. Chybí mu kontext pro volatilitu (BB), intraday levely (VWAP), volume trend (OBV), jemnější momentum (Stoch RSI) a divergence.

---

## 2026-05-27 — Fix: backtest_claude.py — správná SL/TP simulace bar-by-bar

**Soubory:**
- `scripts/backtest_claude.py`

**Co se změnilo:**
`OutcomeSimulation.pnl_pct()` brala cenu za přesně 8h bez SL/TP check — zjednodušení bylo špatné. Nahrazeno funkcí `simulate_trade_outcome()` která prochází každý H1 bar po triggeru a kontroluje: dosáhlo `high` TP? → win. Dosáhlo `low` SL? → loss. První zasažení vyhrává. Vrací `TradeOutcome` s výsledkem (hit_tp/hit_sl/timeout), exit cenou, barem a PnL%. `BacktestRow` rozšířen o `outcome`, `exit_price`, `exit_bar`, `exit_hours`. Tiskne W/L místo jen PnL%.

**Proč:**
Původní simulace ignorovala SL/TP úplně — PnL byl jen cena za 8h minus entry. Trade s TP=+3% mohl být označen jako -1% jenom proto že 8h po triggeru cena byla níž než TP přestože TP bylo zasaženo dřívě.

---

## 2026-05-27 — Fix: test_news.py — aktualizace pro multi-instrument news refactor

**Soubory:**
- `tests/test_news.py` (přepsán)

**Co se změnilo:**
Staré testy testovaly `is_btc_relevant()` — funkci která byla odstraněna při multi-instrument refactoru (news.py teď nefiltruje klíčovými slovy, Claude to dělá sám). Nové testy pokrývají: výběr feedů dle `instrument.kind` (crypto vs stock), správné vložení tickeru do URL, fallback pro neznámý kind, zpětnou kompatibilitu `fetch_news()` shimem, a `_entry_timestamp()` helper.

**Proč:**
`py -m pytest` selhal s `ImportError: cannot import name 'is_btc_relevant'`.

---

## 2026-05-27 — Fix: invalidation None bug + multi-instrument prompt V2

**Soubory:**
- `bot/storage/models.py` — `invalidation` field: `str` → `Optional[str]` s coercí None→""
- `bot/llm/prompts.py` — system prompt V2: multi-instrument místo BTC-only; few-shot příklady pro akcie

**Co se změnilo:**
1. `Decision.invalidation` přijímá `None` od Claude (coercion na `""`). Předtím crashoval s `ValidationError: Input should be a valid string, got None` — v backtestu 10/48 triggerů skončilo jako ERROR.
2. System prompt přepsán: odstraněno "BTC/USD long-only" → "multi-instrument long-only". Přidány 2 few-shot příklady pro US akcie (NVDA breakout enter, IONQ skip pro nízkou likviditu). Starý prompt uložen jako `SYSTEM_DECIDER_V1`, nový jako `SYSTEM_DECIDER_V2`.

**Proč:**
Backtest ukázal: 10 ERRORů z `invalidation=None`, a Claude skipoval 100% IONQ/NVDA/TSLA triggerů protože prompt ho učil rozhodovat o BTC/USD — few-shot příklady byly BTC-only. Claude tak ignoroval kontexty pro akcie.

**Výsledek:**
Očekáváme: 0 ERRORů, lepší enter rate pro akcie (cíl ~10-20% z triggerů).

---

## 2026-05-27 — Nový script: scripts/backtest_claude.py

**Soubory:**
- `scripts/backtest_claude.py` (nový)

**Co se změnilo:**
Nový script pro backtest Claude rozhodnutí na historických triggerech. Projde posledních N barů, najde triggery přes scanner, pro každý trigger sestaví historický kontext (data jaká bot viděl v ten moment) a zavolá Claude API. Simuluje outcome — kde byla cena 4h/8h/24h po triggeru. Výstup: tabulka v terminálu + CSV.

**Proč:**
Ověření kvality strategie před ostrým nasazením. Chceme vědět jestli Claude + scanner kombinace vydělává na historických datech.

---

## 2026-05-27 — H4 uptrend gate: EMA20>EMA50 → close>EMA200

**Soubory:**
- `bot/strategy/scanner.py`
- `tests/test_scanner.py`
- `bot/pipeline.py`
- `scripts/scanner_replay.py`

**Co se změnilo:**
Původní H4 uptrend podmínka (`close > EMA50 AND EMA20 > EMA50`) byla příliš přísná — blokovala všechny triggery v sideways a konsolidaci. Nahrazena za `close > EMA200` (dlouhodobý uptrend).

Zároveň navýšen `bars_context` v pipeline z 200 na 800 a `context_limit` v scanner_replay z `max(200, ...)` na `max(800, ...)` — EMA200 na H4 potřebuje min. 200 H4 barů k inicializaci.

**Proč:**
Za 5 dní od deploye (22.5.–27.5.) bot nenašel jediný fresh trigger. Replay ukázal že stará podmínka blokovala vše v aktuálním tržním prostředí (BTC konsolidace pod EMA50 na H4).

**Výsledek po změně:**
- 500 H1 barů → 48 triggerů za ~3 týdny (bylo 0)
- IONQ trigger dnes 27.5. ve 12:00 UTC
- Všechny testy: 11/11 passed

---

## 2026-05-22 — Multi-instrument execution + paper mode

**Soubory:**
- `bot/execution/alpaca.py` (nový)
- `bot/execution/portfolio.py` (nový)
- `bot/pipeline.py`
- `bot/scheduler.py`
- `bot/main.py`
- `bot/storage/db.py`

**Co se změnilo:**
Přidán execution modul — bracket ordery přes Alpaca API (entry + SL + TP v jednom orderu). Portfolio stav se fetchuje čerstvě před každým kolem ze scheduleru (ne hardcoded $10k). Schema migration V2: přidán `order_id` sloupec do `signals` tabulky.

**Proč:**
Bot byl v shadow mode — logoval signály ale nikam je neposílal. Přechod na paper mode s reálnou exekucí na Alpaca paper účtu.

**Výsledek:**
- `MODE=paper` nastaven na serveru
- 40/40 testů passed při deployi
- Bot běží od 22.5. 18:00 UTC bez chyb

---

## 2026-05-21 — Multi-instrument podpora

**Soubory:**
- `bot/config.py`
- `bot/data/market.py`
- `bot/pipeline.py`
- `bot/scheduler.py`
- `bot/data/news.py`
- `scripts/smoke_fetch.py`
- `scripts/scanner_replay.py`
- `scripts/test_all.py` (nový)
- `docs/DEPLOY_UBUNTU.md`

**Co se změnilo:**
Bot rozšířen z BTC-only na 4 instrumenty: BTC/USD, NVDA, TSLA, IONQ. Přidán `AlpacaStocksProvider` pro US akcie. Scheduler iteruje přes všechny enabled instrumenty automaticky. Per-instrument news keywords v `config.py`.

**Proč:**
Diverzifikace — více příležitostí, různé korelace. Stocks obchodují jen v US market hours (14:30–21:00 UTC).

**Výsledek:**
- Scanner replay: 59 triggerů za ~3 týdny přes 4 instrumenty
- Smoke fetch: PASS pro všechny instrumenty
