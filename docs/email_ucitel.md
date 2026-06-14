**Komu:** Josef.kurka@fsv.cuni.cz  
**Předmět:** JEM207 — Odevzdání projektu: AI Trading Signal Bot

---

Dobrý den, pane doktore,

dovolte mi předložit svůj semestrální projekt do kurzu JEM207 — Data Processing in Python.

Pracoval jsem samostatně (bez partnera).

## Co jsem vytvořil

**AI Trading Signal Bot** — autonomní obchodní bot běžící 24/7 na serveru, který kombinuje klasickou technickou analýzu s rozhodováním velkého jazykového modelu (Claude AI od Anthropic).

Pipeline funguje takto:
1. Každou hodinu stáhne OHLCV data z Alpaca API (BTC/USD, NVDA, TSLA, IONQ)
2. Lokální technický scanner (6 filtrů — EMA pullback, ATR breakout, volume absorption, long i short varianty) hledá potenciální setup
3. Pokud scanner najde trigger, sestaví kompletní kontext (svíčky, 20+ indikátorů, zprávy z RSS, Fear & Greed index, makro kalendář) a zavolá Claude API
4. Claude vrátí strukturované JSON rozhodnutí: vstoupit/přeskočit, směr, entry/SL/TP, confidence
5. Deterministický risk manager má právo veta (R:R, confidence, H4 trend, denní stop-loss, duplicitní pozice)
6. Schválené signály se exekuují přes Alpaca bracket order a pošlou přes Telegram

## GitHub repozitář

https://github.com/GrumpyMonk78/btc-signal-bot

Repozitář obsahuje kompletní zdrojový kód, testy, backtest data a Jupyter notebook s analýzou výsledků (`analysis/backtest_analysis.ipynb`).

## Přístup na běžící server

Pro přímé ověření jsem Vám zřídil read-only SSH přístup na produkční server kde bot běží:

```
Host:     167.235.155.88
Uživatel: ucitel
Heslo:    JEM207bot2026
```

### Příkazy pro prozkoumání projektu

**Přihlášení:**
```bash
ssh ucitel@167.235.155.88
```

**Struktura projektu:**
```bash
ls /home/botuser/btc-signal-bot/
```

**Zdrojový kód (ukázky):**
```bash
cat /home/botuser/btc-signal-bot/bot/strategy/scanner.py
cat /home/botuser/btc-signal-bot/bot/llm/decider.py
cat /home/botuser/btc-signal-bot/bot/risk/manager.py
cat /home/botuser/btc-signal-bot/bot/execution/alpaca.py
```

**Dokumentace:**
```bash
cat /home/botuser/btc-signal-bot/README.md
cat /home/botuser/btc-signal-bot/docs/BOT_ARCHITECTURE.md
cat /home/botuser/btc-signal-bot/docs/CHANGELOG.md
```

**Backtest data:**
```bash
cat /home/botuser/btc-signal-bot/data/backtest_results_v4.csv
ls /home/botuser/btc-signal-bot/analysis/
```

**Status živého bota:**
```bash
systemctl status btc-signal-bot
```

**Živé logy (posledních 50 řádků):**
```bash
journalctl -u btc-signal-bot -n 50
```

**Živé logy (stream, ukončit Ctrl+C):**
```bash
journalctl -u btc-signal-bot -f
```

**Logy za posledních 24 hodin:**
```bash
journalctl -u btc-signal-bot --since "24 hours ago"
```

**Testy:**
```bash
ls /home/botuser/btc-signal-bot/tests/
```

> Poznámka: soubor `.env` (API klíče — Alpaca, Anthropic, Telegram) není z tohoto účtu přístupný z bezpečnostních důvodů.

## Jupyter notebook

Backtest analýzu s vizualizacemi najdete v repozitáři v souboru `analysis/backtest_analysis.ipynb`. Notebook lze spustit lokálně po klonování repozitáře a instalaci závislostí (`pip install -r requirements.txt`).

Backtest zahrnuje 48 triggerů přes 4 instrumenty, win rate 47 %, průměrné PnL +0,27 % na obchod.

Děkuji za celý semestr a těším se na zpětnou vazbu.

S pozdravem,  
Josef Hlahůlek
