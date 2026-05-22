"""
Portfolio state query from Alpaca.

Stahuje aktualni stav uctu z Alpaca API a vraci PortfolioState
pro risk manager.

V shadow rezimu vraci stub (10 000 USD, 0 pozic) — Alpaca paper
ucet existuje i v shadow, ale nechceme zaviset na API kdyz neobchodujeme.

Verejne API
-----------
    fetch_portfolio_state() -> PortfolioState
    stub_portfolio(equity_usd) -> PortfolioState
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.config import settings, Mode
from bot.storage.models import PortfolioState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stub pro shadow rezim
# ---------------------------------------------------------------------------

def stub_portfolio(equity_usd: float = 10_000.0) -> PortfolioState:
    """Vrati fiktivni portfolio pro shadow rezim nebo fallback."""
    return PortfolioState(
        equity_usd=equity_usd,
        open_positions=0,
        daily_pnl_pct=0.0,
        remaining_position_slots=settings.max_open_positions,
    )


# ---------------------------------------------------------------------------
# Real portfolio query
# ---------------------------------------------------------------------------

def fetch_portfolio_state() -> PortfolioState:
    """
    Stahne aktualni stav portfolia z Alpaca a vrati PortfolioState.

    V shadow rezimu vraci stub (nezavisi na API).
    V paper/live rezimu vola Alpaca TradingClient.

    Nikdy nevyhodi vyjimku — pri chybe vraci stub a loge warning.
    Duvod: chyba portfolio query nesmi zastavit celou pipeline.
    """
    if settings.mode == Mode.SHADOW:
        return stub_portfolio()

    try:
        return _query_alpaca()
    except Exception as exc:
        logger.warning(
            "portfolio: Alpaca query failed (%s: %s) — falling back to stub",
            type(exc).__name__, exc,
        )
        return stub_portfolio()


def _query_alpaca() -> PortfolioState:
    """Vola Alpaca API a sestavuje PortfolioState."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    client = TradingClient(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_api_secret,
        paper=settings.alpaca_paper,
    )

    # --- Equity ---
    account = client.get_account()
    equity_usd = float(account.equity)
    if equity_usd <= 0:
        # Fallback pokud ucet nema hodnotu (novy ucet)
        equity_usd = float(account.cash) or 10_000.0

    # --- Open positions ---
    positions = client.get_all_positions()
    open_positions = len(positions)

    # --- Daily PnL ---
    # Alpaca vraci equity a last_equity (konec predchoziho dne).
    # daily_pnl_pct = (equity - last_equity) / last_equity
    try:
        last_equity = float(account.last_equity)
        if last_equity > 0:
            daily_pnl_pct = (equity_usd - last_equity) / last_equity
        else:
            daily_pnl_pct = 0.0
    except (AttributeError, ValueError, ZeroDivisionError):
        daily_pnl_pct = 0.0

    remaining_slots = max(0, settings.max_open_positions - open_positions)

    logger.info(
        "portfolio: equity=$%.2f open_positions=%d daily_pnl=%.2f%% remaining_slots=%d",
        equity_usd, open_positions, daily_pnl_pct * 100, remaining_slots,
    )

    return PortfolioState(
        equity_usd=equity_usd,
        open_positions=open_positions,
        daily_pnl_pct=daily_pnl_pct,
        remaining_position_slots=remaining_slots,
    )
