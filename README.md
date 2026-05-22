# BTC AI Signal Bot

AI-assisted trading signal bot for **BTC/USD**. Hybrid architecture:

```
Alpaca data ─▶ Local scanner (TA filters) ─▶ Claude API ─▶ Risk manager (veto) ─▶ Telegram
                                                                 │
                                                                 ▼
                                                            SQLite log
```

Bot **does not execute trades**. It generates signals and sends them to Telegram.
You enter the trade manually. Phase 2: optional Alpaca auto-execution.

## Scope (phase 1)

- Instrument: **BTC/USD** (Alpaca crypto)
- Direction: **long-only**
- Timeframes: **H1 primary, H4 context**
- Data: OHLCV + crypto news (RSS) + Fear & Greed Index
- Model: `claude-sonnet-4-6`

## Quickstart

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env — fill Alpaca paper keys, Anthropic key, Telegram token+chat_id

# 3. Smoke test (verifies Alpaca data fetch works)
python -m scripts.smoke_fetch

# 4. (later) Run the bot
python -m bot.main
```

## Project layout

```
bot/
├── config.py            # env-based config, validated by pydantic
├── data/
│   └── market.py        # OHLCV provider abstraction + Alpaca implementation
├── strategy/
│   └── scanner.py       # cheap TA filters (placeholder)
├── llm/
│   ├── decider.py       # Claude API call (placeholder)
│   └── prompts.py       # versioned prompts (placeholder)
├── risk/
│   └── manager.py       # hard veto rules (placeholder)
├── notify/
│   └── telegram.py      # Telegram send (placeholder)
├── storage/
│   ├── db.py            # SQLite schema + migrations (placeholder)
│   └── models.py        # dataclasses (placeholder)
├── scheduler.py         # main async loop (placeholder)
└── main.py              # entry point (placeholder)

scripts/
└── smoke_fetch.py       # verify Alpaca data layer

backtest/                # historical replay (phase 2)
tests/                   # pytest suite
data/                    # SQLite DB lives here (gitignored)
logs/                    # log files (gitignored)
```

## Operating modes (`MODE` env var)

- `shadow` — generate signals, log everything, **do not** send Telegram. Default
  for early development.
- `paper` — generate signals + send Telegram. You enter trades manually
  in your broker (or Alpaca paper account). This is the 4+ week validation phase.
- `live` — phase 2. Signals + Alpaca execution.

## Disclaimer

This is not financial advice. Trading carries risk of total capital loss.
The bot is a tool; decisions and responsibility remain yours.
