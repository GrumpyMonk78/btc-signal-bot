# Telegram bot setup — 5 kroků, ~2 minuty

Aby ti bot mohl posílat signály na Telegram, potřebuješ dvě věci v `.env`:

- `TELEGRAM_BOT_TOKEN` — identita tvého bota
- `TELEGRAM_CHAT_ID`   — kam má posílat (tvůj osobní chat, nebo skupina)

## 1. Vytvoř bota přes BotFather

Otevři v Telegramu: **@BotFather** ( https://t.me/BotFather ).

Pošli mu příkazy:

```
/newbot
```

BotFather se zeptá na dvě věci:
- **Display name** — cokoliv chceš, např. `My BTC Signal Bot`
- **Username** — musí končit na `bot`, např. `joe_btc_signals_bot`

Pak ti vrátí zprávu se **API tokenem**, který vypadá takhle:

```
1234567890:AAFq3Z6XyZk9p_AbCdEf1234567890abc_xyz
```

**Zkopíruj celý token.** Otevři `.env` ve své editor a doplň:

```
TELEGRAM_BOT_TOKEN=1234567890:AAFq3Z6XyZk9p_AbCdEf1234567890abc_xyz
```

## 2. Pošli botovi libovolnou zprávu

V Telegramu **vyhledej** username svého bota (např. `@joe_btc_signals_bot`),
otevři chat a **napiš mu cokoliv** (třeba `/start` nebo "hello"). Toto je nutné —
bez první zprávy od tebe Telegram API neví, kam má posílat odpovědi.

## 3. Zjisti chat_id

Pusť ze složky projektu:

```bash
py -m scripts.telegram_test --discover-chat-id
```

Vypíše ti něco jako:

```
Recently active chats:
  chat_id=123456789      type=private    title='Joe T'  last='hello'

  → Copy the chat_id you want into .env as TELEGRAM_CHAT_ID
```

**Zkopíruj číslo `chat_id`** do `.env`:

```
TELEGRAM_CHAT_ID=123456789
```

## 4. Ověř, že to funguje

```bash
py -m scripts.telegram_test
```

Pokud uvidíš na Telegramu zprávu typu

> **BTC AI Signal Bot**
> Telegram smoke test @ 2026-05-21T...
> If you see this, your bot is wired correctly.

→ máš hotovo.

## 5. (Volitelné) Skupinový chat místo soukromého

Pokud chceš signály do **skupiny** (např. abyste viděli i ostatní), vytvoř
v Telegramu skupinu, přidej do ní svého bota jako členů, **napiš v ní libovolnou
zprávu**, a pak znova spusť `--discover-chat-id`. Tentokrát uvidíš i řádek s
`type=group` a `chat_id` typicky **záporné číslo** (např. `-1009876543210`).
Použij tohle číslo místo soukromého.

## Časté problémy

- **"no recent messages"** — zapomněl jsi v kroku 2 napsat botovi. Otevři
  jeho chat a pošli libovolnou zprávu.
- **"Forbidden: bot can't initiate conversation"** — nepsáls botovi první.
  Stejné řešení.
- **HTTP 401 Unauthorized** — token je špatně. Zkopíruj ho znova z BotFather
  bez whitespace a uvozovek.
- **HTTP 404** — token je v platném formátu, ale neexistuje (typo). Zkontroluj.

## Co se posílá

Když pipeline vyhodnotí signál jako **approved** (Claude `enter` + risk
manager schválí), pošle ti zprávu typu:

```
🟢 LONG BTC/USD @ 70,000.00

SL: 69,300.00   (▲700.00, 1.00%)
TP: 71,500.00   (▲1,500.00, 2.14%)
R:R: 1 : 2.14

Confidence: 7/10
Size: $10,000 (0.142857 BTC)

Reasoning:
(Claude's 2-6 sentence justification)

Key risks:
• FOMC at 18:00
• thin Asian liquidity

Invalidation: close below 69300 on H1

setup_id: abc-123-def-456
```

**Bot ti nikdy nepošle SKIP rozhodnutí** — to by byl spam. Skipy jdou jen
do DB pro audit.
