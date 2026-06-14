# AI Trading Signal Bot

**Course:** Data Processing in Python (JEM207)  
**Author:** Josef Pepeh  
**Submitted:** June 2026

---

## Project Overview

An autonomous trading signal bot that runs 24/7 on a Linux server and combines **classical technical analysis** with **large language model (LLM) reasoning** to generate buy/sell signals for financial instruments.

The system collects real-time market data, applies technical filters to identify potential trade setups, and then calls the Claude AI API (Anthropic) to make a final entry/skip decision based on full market context — including price action, news, macroeconomic events, and portfolio state.

### Instruments traded
| Symbol | Type | Exchange |
|--------|------|----------|
| BTC/USD | Crypto | Alpaca Crypto |
| NVDA | Stock | NASDAQ |
| TSLA | Stock | NASDAQ |
| IONQ | Stock | NYSE |

---

## Architecture

```
Market Data (Alpaca API)
        │
        ▼
  Technical Scanner          ← cheap local filters (EMA pullback, breakout, volume)
        │ trigger found?
        ▼
  Context Assembly           ← OHLCV bars + indicators + news + sentiment + portfolio
        │
        ▼
  Claude AI (Anthropic API)  ← LLM decision: enter/skip + entry/SL/TP + reasoning
        │ enter?
        ▼
  Risk Manager               ← deterministic veto rules (R:R, confidence, H4 trend, daily stop)
        │ approved?
        ▼
  Alpaca Execution           ← bracket order (entry + stop-loss + take-profit)
        │
        ▼
  Telegram Notification      ← signal details sent to user
        │
        ▼
  SQLite Storage             ← all decisions, signals, Claude calls logged
```

### Key components

| File | Purpose |
|------|---------|
| `bot/config.py` | Instrument definitions, settings |
| `bot/strategy/scanner.py` | 6 technical filters (3 long + 3 short) |
| `bot/strategy/indicators.py` | EMA, RSI, MACD, Bollinger Bands, VWAP, OBV, StochRSI, ATR |
| `bot/llm/prompts.py` | Claude system prompt (V4) with few-shot examples |
| `bot/llm/decider.py` | Claude API call with prompt caching (~90% token savings) |
| `bot/llm/context.py` | Context assembly + compact text renderer for Claude |
| `bot/risk/manager.py` | Deterministic risk rules (veto power over Claude) |
| `bot/execution/alpaca.py` | Bracket order submission via Alpaca API |
| `bot/execution/position_monitor.py` | Hourly time-exit enforcement (12h rule) |
| `bot/storage/db.py` | SQLite schema + queries |
| `bot/scheduler.py` | APScheduler jobs (hourly pipeline + position monitor) |
| `scripts/backtest_claude.py` | Historical backtest of Claude decisions |

---

## Data Sources

| Source | Data | Method |
|--------|------|--------|
| [Alpaca Markets API](https://alpaca.markets) | OHLCV bars (H1, H4) for stocks and crypto | REST API (`alpaca-py`) |
| [Alternative.me](https://alternative.me/crypto/fear-and-greed-index/) | Bitcoin Fear & Greed Index | REST API (JSON) |
| CoinDesk RSS | Bitcoin/crypto news | RSS feed (`feedparser`) |
| CoinTelegraph RSS | Crypto news | RSS feed (`feedparser`) |
| Yahoo Finance RSS | Stock news (NVDA, TSLA, IONQ) | RSS feed (`feedparser`) |
| Seeking Alpha RSS | Stock news | RSS feed (`feedparser`) |
| [investing.com](https://investing.com) | Macroeconomic calendar (CPI, FOMC, NFP) | Web scraping |

---

## Technical Filters (Scanner)

The scanner runs cheap local checks before calling the expensive Claude API. It uses a **bidirectional trend gate**:
- H4 close > EMA200 → **long signals only**
- H4 close < EMA200 → **short signals only**

### Long filters
1. **EMA Pullback** — price pulls back to EMA20 from an extended position (EMA20 > EMA50)
2. **Breakout ATR** — breakout above 20-bar high with ATR expansion ≥ 1.2× baseline
3. **Volume Absorption** — volume spike ≥ 2× average, close in upper third of bar

### Short filters (mirror image)
4. **EMA Pullback Short** — bounce to EMA20 from below
5. **Breakout ATR Short** — breakdown below 20-bar low with ATR expansion
6. **Volume Absorption Short** — volume spike, close in lower third of bar

---

## Claude AI Integration

When a scanner trigger fires, the full market context is assembled and sent to Claude:

- Last 30 H1 candles + last 30 H4 candles
- 20+ technical indicators (H1 and H4 timeframes)
- News from the last 24h (filtered by instrument keywords)
- Fear & Greed sentiment index
- Macro calendar (recent + upcoming events)
- Portfolio state (equity, open positions, daily PnL)

Claude returns a structured JSON decision:
```json
{
  "decision": "enter",
  "direction": "long",
  "entry_price": 222.64,
  "stop_loss": 218.37,
  "take_profit": 231.18,
  "confidence": 7,
  "size_hint": "normal",
  "reasoning": "EMA pullback in H4 uptrend, RSI not overbought...",
  "key_risks": ["FOMC tomorrow", "earnings next week"],
  "invalidation": "Close below EMA20 (218.51). Time exit: 12h."
}
```

**Prompt caching** is used to reduce API costs by ~90% — the system prompt (~8000 tokens) is cached and reused across calls.

---

## Risk Manager

The risk manager has **veto power** over Claude's decisions. It enforces:

1. Direction must match H4 trend (long only in uptrend, short only in downtrend)
2. Confidence ≥ 6/10
3. Risk:Reward ratio ≥ 1.5
4. Stop-loss distance within 0.3–4× ATR (not too tight, not too wide)
5. Daily PnL > −3% (daily stop-loss)
6. Open positions < 5
7. No high-impact macro events within ±30 minutes (FOMC, CPI, NFP)
8. No duplicate positions (same symbol already open on Alpaca)

Position sizing is deterministic: `position_usd = 1% × equity / SL_distance_pct`

---

## Installation & Setup

### Requirements
- Python 3.13+
- Alpaca paper trading account ([app.alpaca.markets](https://app.alpaca.markets))
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- Telegram bot token (via [@BotFather](https://t.me/BotFather))

### Install dependencies
```bash
pip install -r requirements.txt
```

### Configure environment
```bash
cp .env.example .env
# Fill in your API keys in .env
```

Required environment variables:
```
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
ANTHROPIC_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
MODE=paper        # shadow | paper | live
```

### Run smoke test
```bash
python -m scripts.test_all
```

### Run backtest
```bash
python -m scripts.backtest_claude --bars 500 --budget 1000000 --csv data/backtest_results.csv
```

### Start the bot
```bash
python -m bot.main
```

---

## Backtest Results

Backtest was run on 500 H1 bars (~3 weeks) across all 4 instruments.

| Metric | Value |
|--------|-------|
| Total triggers evaluated | 48 |
| Claude decisions: ENTER | 17 (35%) |
| Claude decisions: SKIP | 31 (65%) |
| Win rate (TP hit) | 47% |
| Average PnL per trade | +0.27% |
| Instruments | BTC/USD, NVDA, TSLA, IONQ |

See `analysis/backtest_analysis.ipynb` for full visualizations.

---

## Live Trading (Paper Mode)

The bot has been running in **paper trading mode** on Alpaca since May 22, 2026.

- Scheduler: every hour at HH:00:30 UTC
- Position monitor: every hour at HH:05:00 UTC (time-exit enforcement)
- Server: Hetzner Ubuntu 24.04, systemd service
- All signals, decisions, and Claude calls logged to SQLite

**Live results (May 22 – June 3, 2026):** 8 trades executed (IONQ, NVDA, TSLA). Trading paused after June 3 due to Alpaca Pattern Day Trading (PDT) protection blocking orders on the paper account — PDT limits accounts under $25k to 3 day-trades per 5 days. Since June 3 the scanner continues to run hourly and detects setups, but none have formed on the current H1 bar within the 1-hour freshness window required to trigger a Claude call. The bot is operating as designed.

---

## Repository Structure

```
.
├── bot/
│   ├── config.py               # Instruments + settings
│   ├── pipeline.py             # Main orchestrator
│   ├── scheduler.py            # APScheduler jobs
│   ├── data/
│   │   ├── market.py           # Alpaca data providers
│   │   ├── news.py             # RSS news fetcher
│   │   ├── sentiment.py        # Fear & Greed index
│   │   └── calendar.py         # Macro calendar
│   ├── strategy/
│   │   ├── scanner.py          # 6 technical filters
│   │   └── indicators.py       # Technical indicators
│   ├── llm/
│   │   ├── prompts.py          # Claude system prompt V4
│   │   ├── decider.py          # Claude API integration
│   │   └── context.py          # Context assembly
│   ├── risk/
│   │   └── manager.py          # Risk rules + position sizing
│   ├── execution/
│   │   ├── alpaca.py           # Bracket order submission
│   │   ├── portfolio.py        # Portfolio state query
│   │   └── position_monitor.py # Time-exit enforcement
│   ├── storage/
│   │   ├── db.py               # SQLite schema + queries
│   │   └── models.py           # Pydantic data models
│   └── notify/
│       └── telegram.py         # Telegram notifications
├── scripts/
│   ├── backtest_claude.py      # Historical backtest
│   ├── scanner_replay.py       # Scanner replay tool
│   ├── smoke_fetch.py          # Data fetch smoke test
│   └── test_all.py             # Full local test suite
├── tests/                      # pytest unit tests (121 tests)
├── analysis/
│   └── backtest_analysis.ipynb # Backtest visualizations
├── docs/
│   ├── CHANGELOG.md            # Change log
│   └── BOT_ARCHITECTURE.md     # Detailed architecture
├── data/
│   └── backtest_results_v4.csv # Backtest results
├── requirements.txt
└── .env.example
```

---

## Key Design Decisions

**Why LLM for trading?** Traditional rule-based systems can't incorporate unstructured context like news, macro events, and narrative reasoning. Claude can synthesize technical signals with qualitative information.

**Why Claude as the decision-maker, not the scanner?** The scanner is cheap and runs locally. Claude API calls cost money — the scanner acts as a pre-filter so Claude only processes high-potential setups (~5–10 per day instead of hundreds).

**Why deterministic risk manager?** LLMs can be overconfident or make inconsistent decisions. The risk manager provides hard guardrails that Claude cannot override.

**Prompt caching:** The system prompt is ~8000 tokens. Without caching, each Claude call would cost ~$0.03. With caching, subsequent calls cost ~$0.003 (90% savings).

---

## Testing

```bash
# Run all tests
python -m scripts.test_all

# Unit tests only
python -m pytest tests/ -x -q

# Scanner backtest
python -m scripts.scanner_replay --bars 500 --show 5
```

The test suite covers: indicators, scanner filters, risk manager rules, position monitor logic, DB schema, and Alpaca execution (with mocks).

---

## Disclaimer

This project is for **educational purposes only**. Paper trading mode uses simulated money. No real financial advice is provided. Past backtest results do not guarantee future performance.
