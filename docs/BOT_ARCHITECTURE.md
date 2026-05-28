# BTC AI Signal Bot — Architektura a fungování

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
- **Telegram** = doručení signálu uživateli (manuální exekuce v XTB)

---

## 2. High-level pipeline

```
Trh (Alpaca API) → Scanner (lokálně) → Trigger? → Claude API → Risk Manager → Telegram
                                          ↓ ne
                                       Konec
```

Každou hodinu v **HH:00:30 UTC**:

1. Scanner stáhne OHLCV data a spustí 3 lokální filtry
2. Pokud nějaký filtr triggne → bot postaví kontext (news, sentiment, makro)
3. Pošle vše do Claude Sonnet 4.6 → dostane strukturované JSON rozhodnutí
4. Risk manager má veto právo (tvrdá pravidla v kódu)
5. Schválené signály jdou na Telegram → ručně zadáš trade do XTB

---

## 3. Komponenty (modul po modulu)

### 3.1 Data layer

**`bot/data/market.py`** — Stahuje BTC OHLCV svíčky z Alpaca (paper account).

| Timeframe | Účel |
|-----------|------|
| H1 | Primary scan timeframe |
| H4 | Trend gate (filter long jen v H4 uptrendu) |
| D1 | Regime context |

**`bot/data/news.py`** — RSS feedy filtrované na BTC/crypto/macro klíčová slova.

**`bot/data/sentiment.py`** — Fear & Greed Index z alternative.me API.

**`bot/data/calendar.py`** — Macro kalendář (FOMC, CPI, NFP — hardcoded data + RSS).

### 3.2 Scanner

**`bot/scanner.py`** — Levné lokální filtry, žádný Claude. **3 long filtry, všechny gated H4 uptrendem:**

| Filtr | Logika |
|-------|--------|
| **EMA pullback** | Cena se nejdřív vzdálila od EMA20, pak se k ní vrátila (testuje support) |
| **Breakout + ATR** | Breakne nad N-day high s rozumným ATR rozšířením |
| **Volume absorption** | Pokles s vysokým objemem ale malým range = nákupní absorpce |

Když žádný filter netriggne → bot zaloguje scan a končí. Žádný Claude call.

### 3.3 LLM decider

**`bot/llm/decider.py`** — Volá Claude Sonnet 4.6:

- System prompt definuje roli a JSON schema
- Context obsahuje: technical state, recent news, macro calendar, F&G index
- Token budget: input ~6k, output ~800
- Claude vrací strukturované JSON:

```json
{
  "action": "enter" | "skip",
  "direction": "long",
  "entry": 67500.0,
  "sl": 66800.0,
  "tp": 68900.0,
  "confidence": 0.72,
  "reasoning": "EMA20 pullback + dropping VIX..."
}
```

### 3.4 Risk manager

**`bot/risk/manager.py`** — Veto pravidla **mají přednost před Claudem**:

- Max 2 otevřené pozice
- SL musí být ≥ 1× ATR
- TP musí být ≥ 1.5× SL (min Risk/Reward 1.5)
- Žádný trade 30 min před a po FOMC/CPI/NFP
- Daily drawdown limit: -2% equity
- Confidence threshold: Claude musí mít ≥ 0.6

### 3.5 Storage

**`bot/storage/db.py`** — SQLite na disku, 6 tabulek:

| Tabulka | Obsah |
|---------|-------|
| `scans` | Každý hourly run (timestamp, n_filters_triggered) |
| `claude_calls` | Request + response + token usage + cost |
| `decisions` | Claudeovo rozhodnutí (action, confidence, reasoning) |
| `signals` | Schválené signály po veto |
| `veto_log` | Zamítnutí risk managerem s důvodem |
| `meta` | Schema version, settings |

### 3.6 Notify

**`bot/notify/telegram.py`** — Pošle Telegram zprávu přes Bot API (httpx, async). Daily summary v 00:05 UTC.

### 3.7 Scheduler & Main

**`bot/scheduler.py` + `bot/main.py`** — APScheduler AsyncIOScheduler, cron job na HH:00:30. Main loop drží process živý.

---

## 4. Mode = "shadow"

Aktuálně bot běží v `shadow` módu:

- ✅ Claude analyzuje
- ✅ Risk manager vetuje
- ✅ Signály se logují do DB
- ❌ **NEPOSÍLAJÍ se na Telegram** (kromě daily summary)

Tohle je úmyslné — sbíráš data 1-3 měsíce, vyhodnotíš performance backtestem, **pak** přepneš na `live` mode.

---

## 5. Infrastruktura

### 5.1 Server

- **Provider:** Hetzner Cloud
- **OS:** Ubuntu 24.04 LTS
- **Specs:** 4 GB RAM, 40 GB disk
- **IP:** `167.235.155.88`

### 5.2 Process model

- Bot běží jako `botuser` (ne root) → bezpečnostní izolace
- Spravován **systemd** (`/etc/systemd/system/btc-signal-bot.service`)
- Autostart on boot, `Restart=always`, `MemoryMax=512M`
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ReadWritePaths` jen pro `data/` + `logs/`

### 5.3 GitHub workflow

```
Lokálně (Windows)              GitHub                    Server (Hetzner)
─────────────────              ──────                    ────────────────
upravit kód                                              
git add .                                                
git commit -m "..."                                      
git push origin main  ───►  branch main  ◄───  git pull (via deploy.sh)
                                                         pytest
                                                         systemctl restart
                                                         status check
```

**Deploy command na serveru:**
```bash
sudo -u botuser -i
cd ~/btc-signal-bot
./deploy.sh
```

`deploy.sh` má 5 kroků: pull → install deps → pytest → restart → status check. Při selhání testů exit 1 (žádný restart se špatným kódem).

### 5.4 Bezpečnost

- **Deploy key** na serveru (`~/.ssh/github_deploy`, **read-only** přístup k repo)
- **Sudoers NOPASSWD** jen na konkrétní systemctl příkazy s argumenty
- **`.env`** na serveru, NIKDY v gitu (`.gitignore` ho chrání)

**Klíče v `.env`:**
- `ALPACA_API_KEY` + `ALPACA_API_SECRET`
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`

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
├── data/
│   └── bot.db                        # SQLite (NOT in git)
├── logs/                             # App logs (NOT in git, created at deploy)
├── .venv/                            # Python virtual env (NOT in git)
├── .env                              # Secrets (NOT in git!)
├── deploy.sh                         # Deploy script
└── requirements.txt

/etc/systemd/system/btc-signal-bot.service    # Service unit
/etc/sudoers.d/botuser-systemctl              # NOPASSWD pro systemctl
```

---

## 7. Denní rytmus bota

```
00:00 UTC — Scan #1   → typicky no trigger
00:05 UTC — Telegram daily summary (yesterday's scans, calls, decisions)
01:00 UTC — Scan #2   → typicky no trigger
...
[24 scanů/den, ~1-3 Claude calls/den průměrně]
...
23:00 UTC — Scan #24  → typicky no trigger
```

**Aktuální DB stav:** ~11 scans, 1 Claude call, 1 decision, 0 signálů, 1 veto. Konzervativní filtry = málo signálů, ale kvalitní.

---

## 8. Co bot **nedělá** (záměrně)

| Funkce | Stav | Důvod |
|--------|------|-------|
| Auto-execution trades | ❌ | XTB nemá retail API (xStation zrušen) |
| Short pozice | ❌ | Všechny filtry jsou long-only zatím |
| Real-time backtest | ❌ | Historický replay je separátní skript |
| Live Telegram signály | ❌ | Mode = shadow (sběr dat) |
| Multi-symbol | ❌ | Pouze BTC zatím |

---

## 9. Model & cost

| Model | Použití |
|-------|---------|
| **Claude Sonnet 4.6** ✅ | Production decider — sweet spot quality/cost |
| Claude Opus 4.6 | Nepotřeba pro tuto doménu — marginal returns |
| Claude Haiku 4.5 | Příliš mělký pro multi-faktor trading decisions |

**Cena/den:** ~$0.03 (Sonnet, 1-3 callů). Měsíčně ~$1.

---

## 10. Roadmap

### Krátkodobé (next priority)

1. **Více filtrů** — bull flag, VWAP reclaim, EMA50 pullback, higher high
2. **Více instrumentů** — ETH, SOL (long-only)
3. **Lepší news/sentiment** — funding rates, on-chain data

### Středně dlouhodobé (po 1-3 měsících shadow modu)

4. **Vyhodnocení performance** — backtest na nasbíraných signálech
5. **Přepnutí na `live` mode** — Telegram signály reálně
6. **Short setupy** s přísnými regime filtry (jen v BTC bear marketu)

### Dlouhodobé

7. **Auto-execution přes Alpaca live** — order management, position tracking
8. **Multi-strategy** — různé filtry pro různé režimy (trend vs range)

---

## 11. Workflow pro úpravy kódu

```bash
# 1. Lokálně na Windows
cd "C:\Users\pepeh\OneDrive\Desktop\Algo_trading\Bot_claude\AI trading new bot"
# ... uprav kód ...
git add .
git commit -m "feat: add VWAP reclaim filter"
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

## 12. Quick reference commands

### Server admin

```bash
# Status služby
sudo systemctl status btc-signal-bot

# Restart
sudo systemctl restart btc-signal-bot

# Live logy
sudo journalctl -u btc-signal-bot -f

# DB query (jako botuser)
~/btc-signal-bot/.venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('/home/botuser/btc-signal-bot/data/bot.db')
for t in ['scans','claude_calls','decisions','signals','veto_log']:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n}')
"
```

### Lokální development

```bash
# Spustit testy
python -m pytest -x --tb=short -q

# Spustit replay (backtest)
python -m scripts.scanner_replay

# Dump context (debug Claude payload)
python -m scripts.dump_context

# Manuální Claude call (test)
python -m scripts.ask_claude
```

---

## TL;DR

**Bot běží 24/7 na Hetzner serveru, každou hodinu skenuje BTC, ~1-3x denně volá Claude Sonnet 4.6 na rozhodnutí. Risk manager vetuje. Aktuálně shadow mode — signály se logují, ale neposílají na Telegram. Workflow: `git push` lokálně → `./deploy.sh` na serveru.**
