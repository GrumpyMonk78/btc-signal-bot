"""
Centralised, validated configuration.

Loads from environment variables (and optionally a `.env` file in the project
root). All other modules import `settings` from here — no module reads env
vars directly. This is the 12-factor boundary.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Mode(str, Enum):
    """Operating mode — determines what side-effects the bot performs."""

    SHADOW = "shadow"  # log only, no Telegram, no execution
    PAPER = "paper"    # log + Telegram, no execution
    LIVE = "live"      # log + Telegram + Alpaca execution (phase 2)


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
    instrument: str = Field(default="BTC/USD")
    timeframe_primary: str = Field(default="1H")
    timeframe_context: str = Field(default="4H")

    # ── Risk ──────────────────────────────────────────────────────────────
    risk_per_trade: float = Field(default=0.01, gt=0, le=0.05)
    max_open_positions: int = Field(default=3, ge=1, le=10)
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

    @field_validator("instrument")
    @classmethod
    def _validate_instrument(cls, v: str) -> str:
        # Alpaca crypto symbols use slash form, e.g. "BTC/USD"
        if "/" not in v:
            raise ValueError(f"instrument must be in 'BASE/QUOTE' form, got {v!r}")
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
        """Return list of secret env vars that are empty.

        Used by smoke tests and bot startup to give a friendly error rather
        than a stack trace from the SDK when keys aren't set.
        """
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


# Singleton — import this everywhere
settings = Settings()
