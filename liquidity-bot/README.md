# Liquidity Sweep Bot

Algo trading bot hledající **liquidity sweep** setupy — velké svíce (stop-hunt) následované reversalem ve směru trendu.

**Žádné AI / Claude volání** — čistá technická logika v Pythonu.

## Quickstart

```bash
# 1. Zkopíruj .env
cp .env.example .env
# Vyplň ALPACA_API_KEY, ALPACA_SECRET_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

# 2. Instalace závislostí
pip install -r requirements.txt

# 3. Test jednoho cyklu
python main.py --once

# 4. Backtest scanneru na BTC datech
python main.py --backtest

# 5. Spustit bota (každých 15 min skenuje)
python main.py
```

## Strategie

Viz `docs/STRATEGY.md` pro detail.

TL;DR:
1. Trend filter: EMA50/200 na 1H
2. Sweep detekce: svíce >= 2× ATR na 15min
3. Reversal confirmation: opačná svíce s čistým tělem
4. Bracket order: SL za sweep + TP na 2.5R

## Struktura

```
liquidity-bot/
├── main.py                        # Entry point + scheduler
├── requirements.txt
├── .env.example
├── bot/
│   ├── config.py                  # Veškerá konfigurace
│   ├── pipeline.py                # Orchestrator
│   ├── strategy/
│   │   └── scanner.py             # Sweep detektor (hlavní logika)
│   ├── execution/
│   │   └── alpaca_client.py       # Alpaca orders + data
│   └── notification/
│       └── telegram_notify.py     # Telegram zprávy
└── docs/
    └── STRATEGY.md                # Detailní popis strategie
```

## Paper trading

Bot je nastaven na **paper trading** (Alpaca paper API).
Pro live trading změň `paper=True` → `paper=False` v `alpaca_client.py` — až po důkladném backtestování!
