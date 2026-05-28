# AI Signal Bot — Architektura a fungování

> Stav k: 2026-05-25
> Repo: [github.com/GrumpyMonk78/btc-signal-bot](https://github.com/GrumpyMonk78/btc-signal-bot)
> Server: Hetzner Ubuntu 24.04 @ 167.235.155.88

---

## 1. Filosofie

**Hybrid přístup:** Levný lokální Python skener filtruje šum, drahá Claude API rozhoduje jen u kvalitních setupů, a tvrdá kódová risk pravidla zabraňují Claudeovi udělat blbost.

Každý komponent dělá to, v čem je nejlepší:
- **Scanner** = rychlé technické filtry (pandas, numpy)
- **Claude Sonnet 4.6** = kontextové rozhodování s news/macro/sentiment
- **Risk manager** = deterministické veto právo
- **Alpaca paper/live API** = automatická exekuce bracket orderů
- **Telegram** = doručení signálu uživateli pro informaci

---

## 2. High-level pipeline

```
Trh (Alpaca API) → Scanner (lokálně) → Trigger? → Claude API → Risk Manager → Alpaca order + Telegram
                                          ↓ ne
                                       Konec (zaloguj scan)
```

Každou hodinu v **HH:00:30 UTC** pro každý aktivní instrument:

1. Stáhne aktuální stav portfolia z Alpaca (equity, open positions, daily PnL)
2. Scanner stáhne OHLCV data a spustí 3 lokální filtry
3. Pokud filtr triggne na **aktuálním baru** (fresh trigger < 1h 5min) → sestaví kontext
4. Pošle vše do Claude Sonnet 4.6 → dostane strukturované JSON rozhodnutí
5. Risk manager má veto právo (tvrdá pravidla v kódu)
6. Schválené signály → bracket order na Alpaca (paper/live) + zpráva na Telegram

---

## 3. Komponenty (modul po modulu)

### 3.1 Data layer

**`bot/data/market.py`** — Stahuje OHLCV svíčky z Alpaca. Dva providery:

| Provider | Použití |
|----------|---------|
| `AlpacaCryptoProvider` | BTC/USD, ETH/USD a jiné crypto páry |
| `AlpacaStocksProvider` | NVDA, TSLA, IONQ a jiné US akcie |

Factory funkce `provider_for(instrument)` automaticky vybere správný provider podle `instrument.kind`.

| Timeframe | Účel |
|-----------|------|
| H1 | Primary scan timeframe |
| H4 | Trend gate (filter long jen v H4 uptrendu) |

**`bot/data/news.py`** — RSS feedy filtrované na per-instrument klíčová slova (každý instrument má svůj seznam v `config.py`).

**`bot/data/sentiment.py`** — Fear & Greed Index z alternative.me API.

**`bot/data/calendar.py`** — Macro kalendář (FOMC, CPI, NFP — hardcoded data + RSS).

### 3.2 Instrumenty a konfigurace

**`bot/config.py`** — Centrální seznam všech obchodovaných instrumentů:

```python
INSTRUMENTS = [
    InstrumentConfig(symbol="BTC/USD", kind="crypto", ...),
    InstrumentConfig(symbol="NVDA",    kind="stock",  ...),
    InstrumentConfig(symbol="TSLA",    kind="stock",  ...),
    InstrumentConfig(symbol="IONQ",    kind="stock",  ...),
]
```

Přidání/odebrání instrumentu = editace tohoto seznamu. `enabled=False` = dočasné vypnutí bez mazání. Scheduler iteruje přes všechny enabled instrumenty automaticky.

### 3.3 Scanner

**`bot/strategy/scanner.py`** — Levné lokální filtry, žádný Claude. **3 long filtry, všechny gated H4 uptrendem:**

| Filtr | Logika |
|-------|--------|
| **EMA pullback** | Cena se nejdřív vzdálila od EMA20 (stretched), pak se k ní vrátila — testuje support |
| **Breakout + ATR** | Breakne nad N-day high s ATR expanzí (>1.2× baseline) a silným tělem svíčky |
| **Volume absorption** | Objem >2× MA20, close v horní třetině baru, bullish svíčka |

**Přechod (rising edge):** Filtr triggne jen při přechodu False→True — ne opakovaně když podmínka trvá.

**Cooldown:** Minimálně 4 hodiny mezi jakýmikoliv dvěma signály (globálně přes všechny filtry).

**Fresh trigger:** Pipeline volá Claude jen pokud poslední signál je starý méně než 1h 5min (= na aktuálním zavřeném baru).

Když žádný filter netriggne → bot zaloguje scan a končí. Žádný Claude call.

### 3.4 LLM decider

**`bot/llm/decider.py`** — Volá Claude Sonnet 4.6:

- System prompt definuje roli a JSON schema
- Kontext obsahuje: technical state, recent news, macro calendar, F&G index, portfolio stav
- Token budget: input ~5-7k, output ~500-800
- Claude vrací strukturované JSON:

```json
{
  "decision": "enter" | "skip",
  "direction": "long" | null,
  "entry_price": 67500.0,
  "stop_loss": 66800.0,
  "take_profit": 68900.0,
  "confidence": 7,
  "size_hint": "normal",
  "reasoning": "EMA20 pullback + H4 uptrend...",
  "key_risks": ["...", "..."],
  "invalidation": "Hourly close below 66500 would invalidate."
}
```

### 3.5 Risk manager

**`bot/risk/manager.py`** — Veto pravidla **mají přednost před Claudem**:

- Max otevřených pozic: `settings.max_open_positions` (default 5)
- TP/SL ratio: minimálně `settings.min_rr` (default 1.5)
- Claude confidence: minimálně `settings.min_confidence` (default 6/10)
- Žádný trade 30 min před a po FOMC/CPI/NFP
- Daily drawdown limit: `settings.daily_stop_pct` (default -3% equity)
- SL musí být rozumný (ne 0, ne větší než entry)

### 3.6 Execution

**`bot/execution/alpaca.py`** — Automatická exekuce po schválení risk managerem:

- **Shadow mode:** No-op — pouze zaloguje, žádný order
- **Paper/live mode:** Pošle bracket order na Alpaca (entry + SL + TP v jednom orderu)
- Position sizing: crypto = fractional BTC (8 des. míst), akcie = celé kusy
- Symbol konverze: `"BTC/USD"` → `"BTCUSD"` pro Alpaca API
- `order_id` se uloží do DB (`signals.order_id`)

**`bot/execution/portfolio.py`** — Reálný stav portfolia z Alpaca před každým kolem:

- Shadow mode: stub $10,000
- Paper/live mode: `equity`, `open_positions`, `daily_pnl_pct` z Alpaca account API
- Fallback na stub při výpadku API

### 3.7 Storage

**`bot/storage/db.py`** — SQLite na disku, schema_version=2, 6 tabulek + WAL mode:

| Tabulka | Obsah |
|---------|-------|
| `scans` | Každý hourly run (timestamp, instrument, n_signals, latest_filter) |
| `claude_calls` | Request + response + token usage + latency |
| `decisions` | Claudeovo rozhodnutí (decision, confidence, reasoning, key_risks) |
| `veto_log` | Zamítnutí risk managerem s kódy + position sizing |
| `signals` | Schválené signály (entry, SL, TP, position_usd, order_id) |
| `outcomes` | Výsledky obchodů (open/sl_hit/tp_hit/time_stopped) — pro budoucí tracking |

### 3.8 Notify

**`bot/notify/telegram.py`** — Zprávy přes Telegram Bot API. Daily summary v 00:05 UTC. Heartbeat při startu bota.

### 3.9 Scheduler & Main

**`bot/scheduler.py` + `bot/main.py`** — APScheduler AsyncIOScheduler:

- `main_loop`: cron `HH:00:30 UTC` — iteruje přes všechny enabled instrumenty
- `daily_summary`: `00:05 UTC` — shrnutí dne na Telegram
- `heartbeat`: jednorázově při startu — "bot spuštěn" zpráva

Portfolio se fetchuje čerstvě před každým kolem (ne jednou při startu).

---

## 4. Módy

| Mode | Scanner | Claude | Risk | Execution | Telegram |
|------|---------|--------|------|-----------|----------|
| `shadow` | ✅ | ✅ | ✅ | ❌ no-op | ✅ daily summary |
| `paper` | ✅ | ✅ | ✅ | ✅ Alpaca paper | ✅ signály + summary |
| `live` | ✅ | ✅ | ✅ | ✅ Alpaca live | ✅ signály + summary |

**Aktuální stav: `MODE=paper`** — bot posílá reálné bracket ordery na Alpaca paper account.

Změna módu: na serveru edituj `.env` → `MODE=paper` nebo `MODE=live` → `systemctl restart btc-signal-bot`.

---

## 5. Infrastruktura

### 5.1 Server

- **Provider:** Hetzner Cloud
- **OS:** Ubuntu 24.04 LTS
- **Specs:** 4 GB RAM, 40 GB disk
- **IP:** `167.235.155.88`
- **Login:** `ssh root@167.235.155.88`

### 5.2 Process model

- Bot běží jako `botuser` (ne root) → bezpečnostní izolace
- Spravován **systemd** (`/etc/systemd/system/btc-signal-bot.service`)
- Autostart on boot, `Restart=always`, `MemoryMax=512M`
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ReadWritePaths` jen pro `data/` a `logs/`

### 5.3 GitHub workflow

```
Lokálně (Windows)              GitHub                    Server (Hetzner)
─────────────────              ──────                    ────────────────
upravit kód
git add .
git commit -m "..."
git push origin main  ───►  branch main  ◄───  git pull (via deploy.sh)
                                                         python -B -m scripts.test_all
                                                         systemctl restart
```

**Deploy command na serveru:**
```bash
ssh root@167.235.155.88
sudo -u botuser -i
cd ~/btc-signal-bot
./deploy.sh
```

`deploy.sh`: pull → pip install → `python -B -m scripts.test_all` (40 testů) → restart → status check. Při selhání testů exit 1, žádný restart.

### 5.4 Bezpečnost

- **Deploy key** na serveru (`~botuser/.ssh/github_deploy`, **read-only** přístup k repo)
- **Sudoers NOPASSWD** jen na konkrétní systemctl příkazy
- **`.env`** je na serveru v `/home/botuser/btc-signal-bot/.env`, NIKDY v gitu

**Klíče v `.env`:**
```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
MODE=paper
```

### 5.5 Persistence

| Soubor/Složka | V gitu? | Backup strategie |
|---------------|---------|------------------|
| `bot/`, `tests/`, `scripts/` | ✅ Ano | git |
| `.env` | ❌ Ne | lokální kopie na Windows |
| `data/bot.db` | ❌ Ne | ručně před migracemi |
| `logs/` | ❌ Ne | systemd journal je primární |
| `.venv/` | ❌ Ne | re-create `python3 -m venv .venv` |

---

## 6. Klíčové cesty na serveru

```
/home/botuser/btc-signal-bot/        # Project root
├── bot/                              # Source code
│   ├── config.py                     # Instrumenty + settings
│   ├── pipeline.py                   # Orchestrator jednoho passe
│   ├── scheduler.py                  # APScheduler jobs
│   ├── main.py                       # Entry point
│   ├── data/                         # market, news, sentiment, calendar
│   ├── strategy/                     # scanner, indicators
│   ├── llm/                          # decider, prompts, context
│   ├── risk/                         # manager
│   ├── execution/                    # alpaca.py (orders), portfolio.py
│   ├── storage/                      # db.py (SQLite), models.py
│   └── notify/                       # telegram.py
├── scripts/                          # CLI nástroje
│   ├── test_all.py                   # 40 testů (smoke + unit)
│   ├── scanner_replay.py             # Historický backtest scanneru
│   ├── smoke_fetch.py                # Test stažení dat pro všechny instrumenty
│   ├── ask_claude.py                 # Manuální Claude call
│   └── dump_context.py              # Debug Claude payload
├── tests/                            # pytest testy
├── data/bot.db                       # SQLite (NOT in git)
├── logs/                             # App logs (NOT in git)
├── .venv/                            # Python virtual env (NOT in git)
├── .env                              # Secrets + MODE (NOT in git!)
├── deploy.sh                         # Deploy script
└── requirements.txt

/etc/systemd/system/btc-signal-bot.service    # Service unit
/etc/sudoers.d/botuser-systemctl              # NOPASSWD pro systemctl
```

---

## 7. Denní rytmus bota

```
00:00 UTC — Scan #1 (BTC, NVDA, TSLA, IONQ) → typicky no fresh trigger
00:05 UTC — Telegram daily summary
01:00 UTC — Scan #2 → ...
...
[24 kol/den × 4 instrumenty = 96 scanů/den]
[průměrně 0-2 fresh triggery/den → 0-2 Claude calls/den]
...
23:00 UTC — Scan #24
```

**Aktuální DB stav (server, paper mode od ~21.5.2026):**
- ~127+ scanů, minimálně 1 Claude call, 1 decision (skip), 0 schválených signálů, 1 veto
- Konzervativní filtry + H4 uptrend gate = málo signálů, ale kvalitní

---

## 8. Scanner výsledky — replay backtest (500 H1 barů, ~3 týdny)

| Symbol | Triggery | Rate/týden | breakout_atr | ema_pullback | volume_absorption |
|--------|----------|------------|--------------|--------------|-------------------|
| BTC/USD | 18 | ~6.1/w | 2 | 10 | 6 |
| NVDA | 16 | ~2.9/w | 3 | 11 | 2 |
| TSLA | 11 | ~2.0/w | 1 | 6 | 4 |
| IONQ | 14 | ~2.5/w | 0 | 10 | 4 |
| **Celkem** | **59** | | **6** | **37** | **16** |

`ema_pullback` dominuje (63% triggerů). `breakout_atr` vzácný — ATR_EXPANSION_MULT=1.2 je přísný, zvlášť pro méně likvidní akcie (IONQ=0).

---

## 9. Co bot dělá / nedělá

| Funkce | Stav |
|--------|------|
| Multi-instrument (BTC, NVDA, TSLA, IONQ) | ✅ |
| Auto-execution bracket orderů (Alpaca paper) | ✅ |
| Reálný portfolio stav před každým kolem | ✅ |
| Telegram signály v paper mode | ✅ |
| H4 uptrend gate (long-only) | ✅ |
| Short pozice | ❌ záměrně — long-only bot |
| Live Alpaca execution | ❌ zatím paper (přepnout MODE=live až po testování) |
| XTB / jiný broker | ❌ není API |
| Outcome tracking (SL/TP hit) | ❌ tabulka připravena, logika chybí |

---

## 10. Model & cost

| Model | Použití |
|-------|---------|
| **Claude Sonnet 4.6** ✅ | Production decider — sweet spot quality/cost |
| Claude Opus 4.6 | Zbytečný overhead pro tuto doménu |
| Claude Haiku 4.5 | Příliš mělký pro multi-faktor trading decisions |

**Cena/den:** ~$0.01–0.05 (Sonnet, 0-2 callů). Měsíčně ~$0.50–1.50.

---

## 11. Roadmap

### Krátkodobé

1. **Outcome tracking** — skript který denně kontroluje SL/TP hit pro otevřené signály
2. **Více instrumentů** — ETH/USD, MSFT, AAPL (odkomentovat v config.py)
3. **Lepší news/sentiment** — funding rates, on-chain data pro BTC

### Středně dlouhodobé (po 1-3 měsících paper modu)

4. **Vyhodnocení performance** — porovnat signály s backtestem
5. **Přepnutí na `live` mode** — reálné peníze přes Alpaca
6. **Short setupy** — jen v BTC bear marketu s přísnými filtry

### Dlouhodobé

7. **Fundamentální bot** — SEC filings, earnings surprises (separátní projekt)
8. **Multi-strategy** — různé filtry pro různé tržní režimy

---

## 12. Workflow pro úpravy kódu

```bash
# 1. Lokálně na Windows
cd "C:\Users\pepeh\OneDrive\Desktop\Algo_trading\Bot_claude\AI trading new bot"
# ... uprav kód ...
git add .
git commit -m "feat: popis změny"
git push origin main

# 2. Na serveru (přes SSH)
ssh root@167.235.155.88
sudo -u botuser -i
cd ~/btc-signal-bot
./deploy.sh

# 3. Sleduj logy
sudo journalctl -u btc-signal-bot -f
```

---

## 13. Quick reference commands

### Server admin

```bash
# Status služby
sudo systemctl status btc-signal-bot

# Restart
sudo systemctl restart btc-signal-bot

# Live logy
sudo journalctl -u btc-signal-bot -f

# Logy od konkrétního data
journalctl -u btc-signal-bot --since "2026-05-20" --no-pager | tail -50

# DB snapshot (jako botuser)
~/btc-signal-bot/.venv/bin/python3 -c "
import sqlite3
conn = sqlite3.connect('/home/botuser/btc-signal-bot/data/bot.db')
conn.row_factory = sqlite3.Row
for t in ['scans','claude_calls','decisions','signals','veto_log']:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n}')
"

# Změna MODE (paper → live nebo naopak)
nano /home/botuser/btc-signal-bot/.env
# Pak restart:
sudo systemctl restart btc-signal-bot
```

### Copper bot (separátní, /root/bots/copper_bot/)

```bash
# Status
cd /root/bots/copper_bot && set -a && source /root/bots/.env && set +a && /root/bot_env/bin/python3 copper_bot.py --status

# Dry-run
cd /root/bots/copper_bot && set -a && source /root/bots/.env && set +a && /root/bot_env/bin/python3 copper_bot.py --dry-run

# Logy
tail -50 /root/bots/copper_bot/cron.log
```

### Lokální development (Windows)

```bash
# Všechny testy
py -B -m scripts.test_all

# Scanner replay (backtest)
py -B -m scripts.scanner_replay --bars 500

# Smoke fetch (ověř data pro všechny instrumenty)
py -B -m scripts.smoke_fetch

# Dump context (debug Claude payload)
py -B -m scripts.dump_context
```

---

## TL;DR

**Bot běží 24/7 na Hetzner serveru jako systemd služba (`botuser`). Každou hodinu skenuje 4 instrumenty (BTC/USD, NVDA, TSLA, IONQ). Při fresh triggeru volá Claude Sonnet 4.6, risk manager vetuje, schválené signály jdou automaticky jako bracket ordery na Alpaca paper account + Telegram notifikace. Aktuální mode: `paper`. Workflow: `git push` lokálně → `./deploy.sh` na serveru.**
