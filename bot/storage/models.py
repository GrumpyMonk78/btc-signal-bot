"""
Pydantic models for everything that flows through the pipeline.

Decision-related models are validated *strictly* — invalid Claude output
must raise rather than silently coerce. The risk manager downstream
assumes a Decision instance is well-formed.

Models
------
    NewsItem            single news headline + metadata
    MacroEvent          single high-impact macro event (FOMC, CPI, NFP…)
    SentimentSnapshot   Fear & Greed Index + 7-day trend
    PortfolioState      open positions, daily PnL, equity
    Bar                 single OHLCV bar (compact form for the prompt)
    ScannerTrigger      what the local scanner saw + relevant numbers
    DeciderContext      complete payload sent to Claude
    Decision            structured Claude output (entry/skip + parameters)
    DecisionDirection   enum: long / short / null
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# Auxiliary data types
# ─────────────────────────────────────────────────────────────────────────────


class NewsItem(BaseModel):
    """A single news headline relevant to BTC."""

    timestamp: datetime = Field(description="Publication time, UTC")
    source: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=512)
    summary: str = Field(default="", max_length=2048)
    url: str = Field(default="")


class MacroEvent(BaseModel):
    """A high-impact macro release (FOMC, CPI, NFP, PCE, etc.)."""

    timestamp: datetime = Field(description="Scheduled release time, UTC")
    name: str = Field(min_length=1, max_length=128)
    importance: Literal["high", "medium"] = "high"
    region: str = Field(default="US", max_length=8)


class SentimentSnapshot(BaseModel):
    """Fear & Greed Index value plus a 7-day trend window."""

    value: int = Field(ge=0, le=100, description="Current Fear & Greed Index value (0-100)")
    classification: str = Field(min_length=1, description="e.g. 'Extreme Fear', 'Greed'")
    trend_7d: list[int] = Field(
        default_factory=list,
        description="Last 7 daily values, oldest first",
    )

    @field_validator("trend_7d")
    @classmethod
    def _validate_trend(cls, v: list[int]) -> list[int]:
        if not all(0 <= x <= 100 for x in v):
            raise ValueError("all trend values must be in 0..100")
        return v


class PortfolioState(BaseModel):
    """Account state at decision time. May be a stub during early phases."""

    equity_usd: float = Field(gt=0, description="Total account equity in USD")
    open_positions: int = Field(ge=0, description="Number of currently open positions")
    daily_pnl_pct: float = Field(description="Realised + unrealised PnL since UTC midnight, fraction")
    remaining_position_slots: int = Field(ge=0)


class Bar(BaseModel):
    """A compact OHLCV bar for the prompt. Timestamp serialises to ISO 8601."""

    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_high_low(self) -> "Bar":
        if self.high < self.low:
            raise ValueError(f"high ({self.high}) < low ({self.low})")
        if not (self.low <= self.open <= self.high):
            raise ValueError("open must be within [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("close must be within [low, high]")
        return self


class ScannerTrigger(BaseModel):
    """What the local scanner flagged — passed to Claude as a hint, not a hard ask."""

    filter: Literal["ema_pullback", "breakout_atr", "volume_absorption", "ema_pullback_short", "breakout_atr_short", "volume_absorption_short"]
    timestamp: datetime
    price: float = Field(gt=0)
    notes: dict = Field(default_factory=dict, description="Numeric context: ATR, EMA values, etc.")


# ─────────────────────────────────────────────────────────────────────────────
# DeciderContext — the full payload sent to Claude
# ─────────────────────────────────────────────────────────────────────────────


class DeciderContext(BaseModel):
    """Complete context the decider sees.

    Heterogeneous on purpose — Claude's edge is in weighing the *combination*
    of these signals, not any one of them. We keep the structure compact so
    the prompt stays under a few thousand tokens.
    """

    instrument: str = Field(default="BTC/USD")
    as_of: datetime = Field(description="Decision timestamp, UTC")

    bars_primary: list[Bar] = Field(
        description="Most recent N H1 bars (oldest first)",
        min_length=1,
        max_length=200,
    )
    bars_context: list[Bar] = Field(
        description="Most recent N H4 bars (oldest first)",
        min_length=1,
        max_length=200,
    )
    trigger: ScannerTrigger

    news_last_24h: list[NewsItem] = Field(default_factory=list, max_length=20)
    sentiment: Optional[SentimentSnapshot] = None
    macro_recent: list[MacroEvent] = Field(default_factory=list, max_length=10)
    macro_upcoming: list[MacroEvent] = Field(default_factory=list, max_length=10)
    portfolio: PortfolioState


# ─────────────────────────────────────────────────────────────────────────────
# Decision — Claude's output
# ─────────────────────────────────────────────────────────────────────────────


class DecisionDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class Decision(BaseModel):
    """Strict structured output from Claude.

    Conventions
    -----------
    - `decision == "skip"` → all trade-related fields must be null/None.
    - `decision == "enter"` → direction, entry_price, stop_loss, take_profit
      must all be provided. For long-only mode (phase 1) the risk manager
      additionally rejects anything where direction != "long".
    - `confidence` is 1..10 inclusive; the risk manager uses a minimum
      threshold (config: MIN_CONFIDENCE).
    - `size_hint` is qualitative — actual lot size is computed deterministically
      from RISK_PER_TRADE × equity / SL distance by the risk manager.
    """

    decision: Literal["enter", "skip"]
    direction: Optional[DecisionDirection] = None
    entry_price: Optional[float] = Field(default=None, gt=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    confidence: int = Field(ge=1, le=10)
    size_hint: Literal["normal", "reduced", "skip"] = "normal"
    reasoning: str = Field(min_length=1, max_length=2000)
    key_risks: list[str] = Field(default_factory=list, max_length=10)
    invalidation: Optional[str] = Field(default="", max_length=512,
                                       description="What price action would invalidate this thesis")

    @field_validator("invalidation", mode="before")
    @classmethod
    def _coerce_invalidation_none(cls, v: object) -> str:
        """Claude sometimes returns null for invalidation — coerce to empty string."""
        return v if v is not None else ""

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Decision":
        if self.decision == "skip":
            # Skip → all trade fields must be null.
            for fld in ("direction", "entry_price", "stop_loss", "take_profit"):
                if getattr(self, fld) is not None:
                    raise ValueError(
                        f"decision=='skip' but {fld} is set ({getattr(self, fld)!r}). "
                        "Skip decisions must have null trade fields."
                    )
            return self

        # decision == "enter" → all trade fields must be set.
        missing = [
            fld for fld in ("direction", "entry_price", "stop_loss", "take_profit")
            if getattr(self, fld) is None
        ]
        if missing:
            raise ValueError(f"decision=='enter' but missing fields: {missing}")

        # Geometric sanity for long.
        if self.direction == DecisionDirection.LONG:
            if not (self.stop_loss < self.entry_price):
                raise ValueError(
                    f"long: stop_loss ({self.stop_loss}) must be < entry_price ({self.entry_price})"
                )
            if not (self.take_profit > self.entry_price):
                raise ValueError(
                    f"long: take_profit ({self.take_profit}) must be > entry_price ({self.entry_price})"
                )
        elif self.direction == DecisionDirection.SHORT:
            if not (self.stop_loss > self.entry_price):
                raise ValueError(
                    f"short: stop_loss ({self.stop_loss}) must be > entry_price ({self.entry_price})"
                )
            if not (self.take_profit < self.entry_price):
                raise ValueError(
                    f"short: take_profit ({self.take_profit}) must be < entry_price ({self.entry_price})"
                )

        # size_hint == "skip" is only valid if decision == "skip"
        if self.size_hint == "skip":
            raise ValueError("size_hint=='skip' is only allowed when decision=='skip'")
        return self

    def risk_reward_ratio(self) -> float:
        """|TP - entry| / |entry - SL|. NaN if any leg is None."""
        if self.entry_price is None or self.stop_loss is None or self.take_profit is None:
            return float("nan")
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        if risk == 0:
            return float("inf")
        return reward / risk




# Risk manager output + final approved signal


class RiskVerdict(BaseModel):
    """Output of the deterministic risk manager - veto power over Claude."""

    approved: bool
    reason: str = Field(min_length=1, max_length=512)
    veto_codes: list[str] = Field(default_factory=list, max_length=10)
    position_size_usd: float = Field(ge=0)
    position_size_btc: float = Field(ge=0)
    r_r_ratio: float = Field(ge=0)


class ApprovedSignal(BaseModel):
    """Signal that passed both Claude and the risk manager."""

    signal_id: str = Field(min_length=1, max_length=64)
    instrument: str
    direction: DecisionDirection
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    position_size_usd: float = Field(gt=0)
    position_size_btc: float = Field(gt=0)
    confidence: int = Field(ge=1, le=10)
    r_r_ratio: float = Field(gt=0)
    reasoning: str = Field(max_length=2000)
    key_risks: list[str] = Field(default_factory=list, max_length=10)
    invalidation: str = Field(default="", max_length=512)
    created_at: datetime
