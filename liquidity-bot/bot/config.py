"""
config.py — Liquidity Sweep Bot configuration
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Alpaca ────────────────────────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Instruments ───────────────────────────────────────────────────────────────
# Alpaca supports: crypto (BTC/USD) and US stocks
# Forex NOT supported by Alpaca — would need OANDA/IBKR
INSTRUMENTS = [
    {"symbol": "BTC/USD",  "asset_class": "crypto",  "qty": 0.001},
    {"symbol": "NVDA",     "asset_class": "stock",   "qty": 1},
    {"symbol": "TSLA",     "asset_class": "stock",   "qty": 1},
    {"symbol": "IONQ",     "asset_class": "stock",   "qty": 5},
]

# ── Timeframes ─────────────────────────────────────────────────────────────────
# Primary trend: 1H, Entry trigger: 15min
TREND_TIMEFRAME = "1Hour"    # for EMA trend filter
ENTRY_TIMEFRAME = "15Min"    # for sweep detection

# ── Strategy parameters ───────────────────────────────────────────────────────
SWEEP_CANDLE_MULTIPLIER = 2.0   # sweep candle must be >= 2x avg ATR
ATR_PERIOD              = 14    # ATR lookback for avg range calc
EMA_FAST                = 50    # fast EMA for trend
EMA_SLOW                = 200   # slow EMA for trend
REVERSAL_MIN_BODY_RATIO = 0.3   # reversal candle: body/total_range >= 30%
LOOKBACK_BARS           = 100   # bars to fetch for calculations

# ── Risk management ────────────────────────────────────────────────────────────
RISK_REWARD_MIN   = 1.5    # minimum R:R ratio to take trade
SL_ATR_MULTIPLIER = 1.0    # SL = sweep high/low + 1x ATR buffer
TP_RR_TARGET      = 2.5    # TP at 2.5R

MAX_OPEN_POSITIONS = 2     # max concurrent positions across all instruments
MAX_RISK_PER_TRADE = 0.01  # 1% of account per trade (used for position sizing)

# ── Scanner schedule ──────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES = 15   # run scanner every 15 minutes

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE  = "logs/liquidity_bot.log"
