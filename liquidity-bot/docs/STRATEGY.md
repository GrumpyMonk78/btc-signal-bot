# Liquidity Sweep Bot — Strategie

## Koncept

Bot hledá "liquidity sweep" — situaci, kdy velká svíce prorazí za stop-lossy ostatních traderů,
a pak se cena obrátí zpět. Toto je jeden z nejběžnějších smart money konceptů (ICT / SMC).

## Logika krok za krokem

### 1. Trend filter (1H timeframe)
- Výpočet EMA50 a EMA200 na 1H datech
- **Bullish trend**: close > EMA50 > EMA200
- **Bearish trend**: close < EMA50 < EMA200
- Pokud mixed → žádný trade

### 2. Sweep detekce (15min timeframe)
- Výpočet průměrného ATR (14 period)
- **Sweep svíce** = range >= 2× průměrný ATR (velká, abnormální svíce)
- Hledáme bar[-2] jako sweep, bar[-1] jako reversal

### 3. Reversal confirmation
- Po sweep musí přijít svíce opačného směru
- Tělo svíce >= 30% z celkového range (žádný "doji")

### 4. Alignment check
- Long setup: sweep dolů → reversal nahoru + bullish trend
- Short setup: sweep nahoru → reversal dolů + bearish trend

### 5. Risk management
- SL: za low/high sweep svíce + 1× ATR buffer
- TP: 2.5× risk (2.5R)
- Minimum R:R = 1.5 (jinak signal zahozen)
- Max 2 otevřené pozice najednou

## Parametry (config.py)

| Parametr | Default | Popis |
|----------|---------|-------|
| SWEEP_CANDLE_MULTIPLIER | 2.0 | Sweep musí být >= Nx průměrný ATR |
| ATR_PERIOD | 14 | Lookback pro ATR |
| EMA_FAST | 50 | Trend filter fast EMA |
| EMA_SLOW | 200 | Trend filter slow EMA |
| REVERSAL_MIN_BODY_RATIO | 0.3 | Min tělo reversal svíce (30%) |
| TP_RR_TARGET | 2.5 | Take profit na 2.5R |
| SL_ATR_MULTIPLIER | 1.0 | ATR buffer za sweep |
| MAX_OPEN_POSITIONS | 2 | Max concurrent trades |

## Instrumenty

- **BTC/USD** (crypto) — funguje 24/7, nejvíce sweep aktivity
- **NVDA, TSLA, IONQ** (US stocks) — jen během trading hours

## Poznámky k Forex

Alpaca **nepodporuje Forex**. Pro forex by bylo potřeba:
- OANDA API (https://developer.oanda.com)
- Interactive Brokers TWS API
- FXCM API

Nejlepší instrumenty pro tuto strategii jsou **BTC/USD a NQ futures**
(hodně retail stop-huntingu, velká liquidita).
