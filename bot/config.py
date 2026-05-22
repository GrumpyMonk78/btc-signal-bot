"""
Centralised, validated configuration.

Loads from environment variables (and optionally a `.env` file in the project
root). All other modules import `settings` from here — no module reads env
vars directly. This is the 12-factor boundary.

──────────────────────────────────────────────────────────────────────────────
INSTRUMENTS — jak přidat nebo odebrat symbol
──────────────────────────────────────────────────────────────────────────────
Edituj seznam INSTRUMENTS níže v tomto souboru. Každý řádek je volání
InstrumentConfig(...). Bot automaticky zpracuje všechny symboly v každém cyklu.

Typy:
  "crypto" — obchoduje přes Alpaca Crypto endpoint (BTC/USD, ETH/USD …)
  "stock"  — obchoduje přes Alpaca Stock endpoint (NVDA, TSLA, IONQ …)

Timeframes: "1Min" | "5Min" | "15Min" | "30Min" | "1H" | "4H" | "1D"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# Instrument definition
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class InstrumentConfig:
    """Definice jednoho obchodovaného instrumentu.

    Attributes
    ----------
    symbol
        Ticker ve formátu Alpaca: pro crypto "BTC/USD", pro akcie "NVDA".
    kind
        "crypto" nebo "stock" — určuje který Alpaca endpoint se použije.
    timeframe_primary
        Kratší timeframe pro skener (technické filtry).
    timeframe_context
        Delší timeframe pro Claude kontext (trend, struktura).
    news_keywords
        Klíčová slova pro filtrování zpráv relevantních k tomuto symbolu.
        Porovnávají se case-insensitive se záhlavím a perexem zprávy.
    enabled
        False = symbol zůstane v seznamu ale bot ho přeskočí. Rychlý způsob
        jak dočasně vypnout symbol bez mazání řádku.
    """
    symbol: str
    kind: Literal["crypto", "stock"]
    timeframe_primary: str = "1H"
    timeframe_context: str = "4H"
    news_keywords: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        allowed_tf = {"1Min", "5Min", "15Min", "30Min", "1H", "4H", "1D"}
        if self.timeframe_primary not in allowed_tf:
            raise ValueError(
                f"[{self.symbol}] timeframe_primary {self.timeframe_primary!r} "
                f"není platný. Povolené: {allowed_tf}"
            )
        if self.timeframe_context not in allowed_tf:
            raise ValueError(
                f"[{self.symbol}] timeframe_context {self.timeframe_context!r} "
                f"není platný. Povolené: {allowed_tf}"
            )
        if self.kind not in ("crypto", "stock"):
            raise ValueError(
                f"[{self.symbol}] kind musí být 'crypto' nebo 'stock', ne {self.kind!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# ★  SEZNAM INSTRUMENTŮ — edituj zde  ★
# ─────────────────────────────────────────────────────────────────────────────
#
# Chceš přidat symbol?  → Přidej nový řádek InstrumentConfig(...)
# Chceš vypnout symbol? → Nastav enabled=False
# Chceš odebrat symbol? → Smaž nebo zakomentuj řádek
#
INSTRUMENTS: list[InstrumentConfig] = [

    InstrumentConfig(
        symbol="BTC/USD",
        kind="crypto",
        timeframe_primary="1H",
        timeframe_context="4H",
        news_keywords=(
            "bitcoin", "btc", "crypto", "halving", "spot etf",
            "microstrategy", "saylor", "binance", "coinbase",
        ),
    ),

    InstrumentConfig(
        symbol="NVDA",
        kind="stock",
        timeframe_primary="1H",
        timeframe_context="4H",
        news_keywords=(
            "nvidia", "nvda", "gpu", "cuda", "blackwell", "hopper",
            "ai chip", "data center", "jensen huang",
        ),
    ),

    InstrumentConfig(
        symbol="TSLA",
        kind="stock",
        timeframe_primary="1H",
        timeframe_context="4H",
        news_keywords=(
            "tesla", "tsla", "elon musk", "cybertruck", "gigafactory",
            "ev sales", "autopilot", "full self driving", "fsd",
        ),
    ),

    InstrumentConfig(
        symbol="IONQ",
        kind="stock",
        timeframe_primary="1H",
        timeframe_context="4H",
        news_keywords=(
            "ionq", "quantum computing", "quantum computer",
            "qubit", "quantum supremacy",
        ),
    ),

    # ── Příklady dalších symbolů (odkomentuj pro aktivaci) ─────────────────
    # InstrumentConfig(
    #     symbol="ETH/USD",
    #     kind="crypto",
    #     timeframe_primary="1H",
    #     timeframe_context="4H",
    #     news_keywords=("ethereum", "eth", "defi", "staking", "layer 2"),
    # ),
    # InstrumentConfig(
    #     symbol="MSFT",
    #     kind="stock",
    #     timeframe_primary="1H",
    #     timeframe_context="4H",
    #     news_keywords=("microsoft", "msft", "azure", "copilot", "openai"),
    # ),
    # InstrumentConfig(
    #     symbol="AAPL",
    #     kind="stock",
    #     timeframe_primary="1H",
    #     timeframe_context="4H",
    #     news_keywords=("apple", "aapl", "iphone", "vision pro", "tim cook"),
    # ),
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers pro přístup k seznamu
# ─────────────────────────────────────────────────────────────────────────────

def get_enabled_instruments() -> list[InstrumentConfig]:
    """Vrátí jen aktivní instrumenty (enabled=True)."""
    return [i for i in INSTRUMENTS if i.enabled]


def get_instrument(symbol: str) -> InstrumentConfig | None:
    """Najdi instrument podle symbolu (case-insensitive)."""
    symbol_up = symbol.upper()
    for inst in INSTRUMENTS:
        if inst.symbol.upper() == symbol_up:
            return inst
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Mode enum
# ─────────────────────────────────────────────────────────────────────────────

class Mode(str, Enum):
    """Operating mode — determines what side-effects the bot performs."""

    SHADOW = "shadow"  # log only, no Telegram, no execution
    PAPER = "paper"    # log + Telegram, paper execution (Alpaca paper account)
    LIVE = "live"      # log + Telegram + live Alpaca execution


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    """All runtime configuration in one place."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Alpaca ────────────────────────────────────────────────────────────
    alpaca_api_key: str = Field(default="", description="Alpaca API key")
    alpaca_api_secret: str = Field(default="", description="Alpaca API secret")
    alpaca_paper: bool = Field(default=True, description="Use paper trading endpoint")
    alpaca_crypto_feed: Literal["us"] = Field(default="us")

    # ── Anthropic / Claude ────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    anthropic_model: str = Field(default="claude-sonnet-4-6")
    anthropic_daily_token_budget: int = Field(default=500_000, ge=1_000)

    # ── Telegram ──────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # ── Trading scope ─────────────────────────────────────────────────────
    # Pozn.: jednotlivý `instrument` je zachován pro zpětnou kompatibilitu
    # se staršími testy. Nový kód používá get_enabled_instruments().
    instrument: str = Field(
        default="BTC/USD",
        description="Primární instrument (legacy — nový kód čte INSTRUMENTS seznam)",
    )
    timeframe_primary: str = Field(default="1H")
    timeframe_context: str = Field(default="4H")

    # ── Risk ──────────────────────────────────────────────────────────────
    risk_per_trade: float = Field(default=0.01, gt=0, le=0.05)
    max_open_positions: int = Field(default=5, ge=1, le=20)
    daily_stop_pct: float = Field(default=-0.03, lt=0, gt=-0.20)
    daily_reset_tz: str = Field(default="UTC")
    min_confidence: int = Field(default=6, ge=1, le=10)
    min_rr: float = Field(default=1.5, gt=0)
    news_blackout_minutes: int = Field(default=30, ge=0, le=240)

    # ── Storage ───────────────────────────────────────────────────────────
    db_path: Path = Field(default=PROJECT_ROOT / "data" / "bot.db")

    # ── Logging ───────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_file: Path = Field(default=PROJECT_ROOT / "logs" / "bot.log")

    # ── Mode ──────────────────────────────────────────────────────────────
    mode: Mode = Field(default=Mode.SHADOW)

    # ── Validátory ────────────────────────────────────────────────────────
    @field_validator("instrument")
    @classmethod
    def _validate_instrument(cls, v: str) -> str:
        # Crypto symbols use slash form; stocks are plain tickers.
        # We accept both — validation of the actual format is done per-instrument
        # in InstrumentConfig.__post_init__.
        return v.upper()

    @field_validator("timeframe_primary", "timeframe_context")
    @classmethod
    def _validate_timeframe(cls, v: str) -> str:
        allowed = {"1Min", "5Min", "15Min", "30Min", "1H", "4H", "1D"}
        if v not in allowed:
            raise ValueError(f"timeframe must be one of {allowed}, got {v!r}")
        return v

    # ── Helpers ───────────────────────────────────────────────────────────
    def missing_secrets(self) -> list[str]:
        """Return list of secret env vars that are empty."""
        checks = {
            "ALPACA_API_KEY": self.alpaca_api_key,
            "ALPACA_API_SECRET": self.alpaca_api_secret,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
        }
        return [name for name, value in checks.items() if not value]

    def required_for_data(self) -> list[str]:
        """Secrets needed for market-data-only operations (smoke fetch)."""
        checks = {
            "ALPACA_API_KEY": self.alpaca_api_key,
            "ALPACA_API_SECRET": self.alpaca_api_secret,
        }
        return [name for name, value in checks.items() if not value]

    def active_instruments(self) -> list[InstrumentConfig]:
        """Zkratka — vrátí aktivní instrumenty ze seznamu INSTRUMENTS."""
        return get_enabled_instruments()


# Singleton — import this everywhere
settings = Settings()
