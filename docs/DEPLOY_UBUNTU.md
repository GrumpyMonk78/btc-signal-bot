# Deploy na Ubuntu — krok za krokem

Tento návod předpokládá čistý Ubuntu server (22.04 LTS nebo novější) a SSH přístup.
Předpokládá se, že máš `sudo` práva. Bot poběží jako systemd služba a restartuje se
sám při chybě nebo rebootu.

## 1. Připoj se na server

```bash
ssh tvuj-user@ip.adresa.serveru
```

## 2. Zjisti, jaký máš Python

```bash
python3 --version
```

Bot vyžaduje **Python 3.11 nebo novější**. Pokud máš starší (např. 3.10 na Ubuntu 22.04 vanilla),
přidej deadsnakes PPA:

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev git
```

Pro Ubuntu 24.04 už `python3` = 3.12, takže stačí:
```bash
sudo apt install -y python3 python3-venv python3-dev git
```

## 3. Vytvoř dedikovaného uživatele

Nedoporučuje se pouštět bota pod root. Vytvoř separátního uživatele bez
sudo práv:

```bash
sudo adduser --disabled-password --gecos "" botuser
```

## 4. Nahraj nebo naklonuj projekt

**Možnost A — git** (pokud máš repozitář):
```bash
sudo -u botuser -i
git clone https://github.com/tvuj-user/btc-signal-bot.git
exit
```

**Možnost B — scp z tvého PC** (rychlejší pro start):
Na lokálním PC v projekt složce:
```bash
# Pošli celý projekt (kromě .env, .venv, data/, logs/) na server
rsync -avz --exclude='.env' --exclude='.venv' --exclude='data/' --exclude='logs/' \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    ./ tvuj-user@ip.adresa:/tmp/btc-bot/

# Na serveru přesuň pod botusera
ssh tvuj-user@ip.adresa
sudo mv /tmp/btc-bot /home/botuser/btc-signal-bot
sudo chown -R botuser:botuser /home/botuser/btc-signal-bot
```

## 5. Vytvoř venv a nainstaluj závislosti

```bash
sudo -u botuser -i
cd ~/btc-signal-bot
python3.11 -m venv .venv          # nebo python3 podle verze
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 6. Vytvoř `.env` na serveru

```bash
cp .env.example .env
nano .env
```

Vyplň **všechno**:
- `ALPACA_API_KEY`, `ALPACA_API_SECRET` (paper keys stačí pro start)
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (viz docs/TELEGRAM_SETUP.md)

`Ctrl+O` uložit, `Ctrl+X` odejít.

## 7. Testy před spuštěním služby

```bash
# Kompletni test vsech instrumentu: data, indikatory, scanner, news, sentiment
# (-B = ignoruj .pyc cache, vzdy nacteme aktualni .py)
.venv/bin/python -B -m scripts.test_all
# Mel bys videt 40+ testu PASS. Pokud neco FAIL, cti chybovou hlasku.

# Volitelne: otestuj jen jeden symbol
.venv/bin/python -B -m scripts.test_all --symbol BTC/USD
.venv/bin/python -B -m scripts.test_all --symbol NVDA

# Data layer detail (vsechny instrumenty)
.venv/bin/python -B -m scripts.smoke_fetch

# Telegram
.venv/bin/python -B -m scripts.telegram_test
# Na telefon by mela prijit testovaci zprava

# Jedno realne rozhodnuti Claude (~$0.02)
.venv/bin/python -B -m scripts.ask_claude
# Mel bys videt Decision + Risk verdict v terminalu
```

Pokud všechno funguje, pokračuj.

Odejdi ze sudo shellu:
```bash
exit
```

## 8. Nainstaluj systemd unit

```bash
sudo cp /home/botuser/btc-signal-bot/deploy/btc-signal-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable btc-signal-bot
sudo systemctl start btc-signal-bot
```

Ověř, že běží:
```bash
sudo systemctl status btc-signal-bot
```

Mělo by ukázat `active (running)`.

## 9. Sleduj logy

```bash
# Live tail
sudo journalctl -u btc-signal-bot -f

# Posledních 100 řádků
sudo journalctl -u btc-signal-bot -n 100

# Jen errors
sudo journalctl -u btc-signal-bot -p err
```

Plus file log:
```bash
sudo tail -f /home/botuser/btc-signal-bot/logs/bot.log
```

Telegram heartbeat ti během minuty od `systemctl start` pošle zprávu
"BTC AI Signal Bot started".

## 10. Co dělá služba

- Každou hodinu na **HH:00:30 UTC** spustí pipeline pro **všechny instrumenty** (scan → Claude → risk → DB → Telegram)
  - Instrumenty jsou definovány v `bot/config.py` → `INSTRUMENTS` — stačí přidat/odebrat/disable
  - Každý instrument má vlastní H1 scanner, Claude kontext a news feed
- Každé **00:05 UTC** pošle na Telegram daily summary
- Při crashe se sama restartuje (Restart=always, max 5× za minutu)
- Loguje do journald (vidíš přes `journalctl`)
- Rotuje log soubory v `logs/` (5× 5MB)

Přidat nový instrument: otevři `bot/config.py`, přidej `InstrumentConfig(...)` do `INSTRUMENTS`, restartuj službu.
Dočasně vypnout instrument: nastav `enabled=False` v jeho `InstrumentConfig`.

## Užitečné příkazy

```bash
# Restart po editaci .env
sudo systemctl restart btc-signal-bot

# Zastavit
sudo systemctl stop btc-signal-bot

# Zakázat autostart po rebootu
sudo systemctl disable btc-signal-bot

# Update kódu po `git pull` nebo rsync
sudo systemctl restart btc-signal-bot

# Prohlížení DB na serveru
sudo -u botuser sqlite3 /home/botuser/btc-signal-bot/data/bot.db
# Pak v sqlite shellu:
#   .tables
#   SELECT * FROM decisions ORDER BY ts_utc DESC LIMIT 5;
#   .quit

# Stáhnout DB k sobě (pro analýzu)
scp tvuj-user@ip.adresa:/home/botuser/btc-signal-bot/data/bot.db ./bot.db
# Otevři v DB Browser for SQLite
```

## Co když systemctl status ukáže `failed`?

```bash
sudo journalctl -u btc-signal-bot -n 50
```

Nejčastější příčiny:
- **Špatná cesta v ExecStart** — uprav `/etc/systemd/system/btc-signal-bot.service`
  pokud máš jiný path nebo username
- **Chybí env vars** — `cat /home/botuser/btc-signal-bot/.env` — ověř, že tam jsou
- **Špatná Python verze** — `botuser` musí mít přístup k Pythonu 3.11+
- **Síťové firewally** — `curl https://api.anthropic.com` a `curl https://data.alpaca.markets`
  ze serveru musí jet
