# CLAUDE.md — Pravidla pro AI asistenta v tomto projektu

## ⚠️ POVINNÉ PRAVIDLO — BEZ VÝJIMKY

**Před každou změnou kódu musí být záznam v `docs/CHANGELOG.md`.**

Postup při každé změně:
1. Zapsat do `docs/CHANGELOG.md` co, proč a v kterých souborech se mění
2. Provést změnu kódu
3. Spustit testy
4. Commitnout

Bez záznamu v CHANGELOG.md nesmí dojít k žádné editaci `.py` souborů.

---

## Projekt

AI Signal Bot — Python bot běžící 24/7 na Hetzner serveru.
Detaily viz `docs/BOT_ARCHITECTURE.md`.

## Stack

- Python 3.13, APScheduler, SQLite, Alpaca API, Anthropic Claude API
- Telegram Bot API pro notifikace
- systemd na Ubuntu 24.04 (Hetzner)

## Instrumenty

BTC/USD (crypto), NVDA, TSLA, IONQ (US stocks) — viz `bot/config.py`

## Workflow pro změny

```
1. Zapiš do docs/CHANGELOG.md
2. Uprav kód lokálně
3. py -B -m pytest -x --tb=short -q   (nebo scripts/test_all.py)
4. git add . && git commit -m "..."
5. git push origin main
6. Na serveru: sudo -u botuser -i → cd ~/btc-signal-bot → ./deploy.sh
```

## Klíčové soubory

| Soubor | Účel |
|--------|------|
| `bot/config.py` | Instrumenty + settings |
| `bot/strategy/scanner.py` | Technické filtry (scanner) |
| `bot/pipeline.py` | Orchestrator pipeline |
| `bot/scheduler.py` | APScheduler jobs |
| `bot/execution/alpaca.py` | Bracket order execution |
| `bot/execution/portfolio.py` | Portfolio state query |
| `bot/storage/db.py` | SQLite schema + queries |
| `docs/CHANGELOG.md` | ⚠️ Povinný záznam změn |
| `docs/BOT_ARCHITECTURE.md` | Kompletní dokumentace |

## Testování

```bash
py -B -m scripts.test_all          # všechny testy (40+)
py -B -m pytest tests/ -x -q      # jen unit testy
py -B -m scripts.scanner_replay --bars 500 --show 5   # backtest scanneru
```

## Server

```
IP: 167.235.155.88
Bot path: /home/botuser/btc-signal-bot/
Service: btc-signal-bot (systemd)
Mode: paper (Alpaca paper trading)
```
