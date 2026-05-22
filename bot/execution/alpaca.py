"""
Order execution via Alpaca Markets API.

Respektuje MODE z konfigurace:
  shadow  -> zadny order (no-op, jen log)
  paper   -> bracket order na Alpaca paper endpoint
  live    -> bracket order na Alpaca live endpoint

Pouziva alpaca-py SDK (uz je v requirements.txt jako alpaca-py).

Bracket order = entry (market nebo limit) + SL (stop) + TP (limit)
odeslan jako jeden OrderRequest s order_class="bracket".

Verejne API
-----------
    execute_signal(signal) -> ExecutionResult
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from bot.config import settings, Mode
from bot.storage.models import ApprovedSignal, DecisionDirection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Co se stalo pri pokusu o odeslani orderu."""

    submitted: bool
    order_id: Optional[str] = None
    mode: str = "shadow"
    error: Optional[str] = None
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        if self.mode == "shadow":
            return f"[shadow] order NOT sent (simulation only)"
        if self.submitted:
            return f"[{self.mode}] bracket order submitted: {self.order_id}"
        return f"[{self.mode}] order FAILED: {self.error}"


# ---------------------------------------------------------------------------
# Alpaca client factory (singleton per session)
# ---------------------------------------------------------------------------

_trading_client = None


def _get_trading_client():
    """Vrati (a cachuje) Alpaca TradingClient pro aktualni rezim."""
    global _trading_client
    if _trading_client is not None:
        return _trading_client

    from alpaca.trading.client import TradingClient

    _trading_client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_api_secret,
        paper=settings.alpaca_paper,
    )
    return _trading_client


# ---------------------------------------------------------------------------
# Position size helpers
# ---------------------------------------------------------------------------

def _qty_for_instrument(signal: ApprovedSignal) -> str:
    """
    Vrati quantity jako string pro Alpaca.

    Pro akcie: pocet kusuu (zaokrouhleno dolu na cele kusy).
    Pro crypto (BTC/USD apod.): pocet coinuu (az 8 desetinnych mist).

    Alpaca prijima qty jako string nebo Decimal.
    """
    # Zjistime kind z konfigurace
    from bot.config import get_instrument
    inst = get_instrument(signal.instrument)
    kind = inst.kind if inst else "stock"

    if kind == "crypto":
        # BTC/USD: position_size_btc je uz vypocteny pocet coinuu
        qty = signal.position_size_btc
        # Minimalni order na Alpaca crypto je $1 notional
        if qty <= 0:
            raise ValueError(f"Invalid crypto qty={qty} for {signal.instrument}")
        return f"{qty:.8f}"
    else:
        # Akcie: position_size_usd / entry_price = pocet kusuu, zaokrouhlit dolu
        shares = int(signal.position_size_usd / signal.entry_price)
        if shares < 1:
            raise ValueError(
                f"Position too small for {signal.instrument}: "
                f"${signal.position_size_usd:.0f} / ${signal.entry_price:.2f} = {shares} shares"
            )
        return str(shares)


# ---------------------------------------------------------------------------
# Core order submission
# ---------------------------------------------------------------------------

def _submit_bracket_order(signal: ApprovedSignal) -> ExecutionResult:
    """
    Odesle bracket order na Alpaca.

    Pouziva MARKET entry (okamzite plneni pri otevreni trhu).
    SL = stop order, TP = limit order — oba se zrusene automaticky
    kdyz se aktivuje druhý (one-cancels-other je soucasti bracket orderu).
    """
    from alpaca.trading.requests import (
        MarketOrderRequest,
        TakeProfitRequest,
        StopLossRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

    client = _get_trading_client()

    try:
        qty_str = _qty_for_instrument(signal)
    except ValueError as exc:
        return ExecutionResult(
            submitted=False,
            mode="paper" if settings.alpaca_paper else "live",
            error=str(exc),
        )

    side = OrderSide.BUY if signal.direction == DecisionDirection.LONG else OrderSide.SELL

    order_req = MarketOrderRequest(
        symbol=signal.instrument.replace("/", ""),  # "BTC/USD" -> "BTCUSD" pro Alpaca
        qty=qty_str,
        side=side,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(signal.take_profit, 4)),
        stop_loss=StopLossRequest(stop_price=round(signal.stop_loss, 4)),
    )

    try:
        order = client.submit_order(order_req)
        mode_str = "paper" if settings.alpaca_paper else "live"
        logger.info(
            "execution [%s]: bracket order submitted id=%s qty=%s side=%s entry~%.4f SL=%.4f TP=%.4f",
            signal.instrument, order.id, qty_str, side.value,
            signal.entry_price, signal.stop_loss, signal.take_profit,
        )
        return ExecutionResult(
            submitted=True,
            order_id=str(order.id),
            mode=mode_str,
            details={
                "qty": qty_str,
                "side": side.value,
                "sl": signal.stop_loss,
                "tp": signal.take_profit,
                "client_order_id": str(order.client_order_id),
            },
        )
    except Exception as exc:
        logger.exception("execution [%s]: order submission failed", signal.instrument)
        return ExecutionResult(
            submitted=False,
            mode="paper" if settings.alpaca_paper else "live",
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def execute_signal(signal: ApprovedSignal) -> ExecutionResult:
    """
    Odesle signal na Alpaca nebo ho skipne (shadow rezim).

    V shadow rezimu se nic neposila — jen log. Takhle muzeme
    testovat celou pipeline bez rizika realnych orderu.

    Parameters
    ----------
    signal
        ApprovedSignal ktery prosel risk managerem.

    Returns
    -------
    ExecutionResult s informaci co se stalo.
    """
    mode = settings.mode

    if mode == Mode.SHADOW:
        logger.info(
            "execution [%s]: SHADOW mode — skipping order "
            "(would: %s %.8f @ ~%.4f SL=%.4f TP=%.4f $%.0f)",
            signal.instrument,
            signal.direction.value,
            signal.position_size_btc,
            signal.entry_price,
            signal.stop_loss,
            signal.take_profit,
            signal.position_size_usd,
        )
        return ExecutionResult(submitted=False, mode="shadow")

    # Paper nebo live — odesli skutecny order
    return _submit_bracket_order(signal)
