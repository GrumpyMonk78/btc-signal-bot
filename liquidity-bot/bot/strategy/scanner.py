"""
scanner.py — Liquidity Sweep Scanner

Strategie:
1. Trend filter: EMA50 vs EMA200 na 1H timeframe
   - Bullish trend: close > EMA50 > EMA200
   - Bearish trend: close < EMA50 < EMA200

2. Sweep detection na 15min timeframe:
   - Sweep svíce: range >= SWEEP_CANDLE_MULTIPLIER × průměrný ATR
   - Bullish sweep (long setup): velká bearish svíce (down sweep) v bullish trendu
   - Bearish sweep (short setup): velká bullish svíce (up sweep) v bearish trendu

3. Reversal confirmation:
   - Svíce hned po sweep musí být opačného směru
   - Body ratio (body/total_range) >= REVERSAL_MIN_BODY_RATIO

4. Signal output: entry, stop_loss, take_profit, direction, confidence
"""

import logging
import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone

from bot.config import (
    SWEEP_CANDLE_MULTIPLIER,
    ATR_PERIOD,
    EMA_FAST,
    EMA_SLOW,
    REVERSAL_MIN_BODY_RATIO,
    LOOKBACK_BARS,
    RISK_REWARD_MIN,
    SL_ATR_MULTIPLIER,
    TP_RR_TARGET,
)

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    direction: str          # "long" | "short"
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    confidence: float       # 0.0 – 1.0
    reasoning: str
    timestamp: datetime
    sweep_candle_size: float
    avg_atr: float


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _trend_from_bars(df_1h: pd.DataFrame) -> Optional[str]:
    """
    Returns 'bullish', 'bearish', or None (mixed / undefined).
    Needs at least EMA_SLOW bars.
    """
    if len(df_1h) < EMA_SLOW + 5:
        return None

    close  = df_1h["close"]
    ema_f  = _ema(close, EMA_FAST).iloc[-1]
    ema_s  = _ema(close, EMA_SLOW).iloc[-1]
    last_c = close.iloc[-1]

    if last_c > ema_f > ema_s:
        return "bullish"
    if last_c < ema_f < ema_s:
        return "bearish"
    return None


def _detect_sweep(df_15m: pd.DataFrame) -> Optional[dict]:
    """
    Scans the last few bars for a liquidity sweep candle followed by a reversal.

    Returns dict with sweep info or None.
    """
    if len(df_15m) < ATR_PERIOD + 3:
        return None

    atr_series = _atr(df_15m, ATR_PERIOD)
    avg_atr    = atr_series.iloc[-3]   # ATR of bar before last 2 (avoid lookahead)

    # We check: bar[-2] = potential sweep, bar[-1] = reversal (just closed)
    sweep_bar   = df_15m.iloc[-2]
    reversal_bar = df_15m.iloc[-1]

    sweep_range    = sweep_bar["high"] - sweep_bar["low"]
    reversal_range = reversal_bar["high"] - reversal_bar["low"]

    if avg_atr == 0:
        return None

    sweep_multiplier = sweep_range / avg_atr

    if sweep_multiplier < SWEEP_CANDLE_MULTIPLIER:
        return None   # not a big enough sweep candle

    sweep_is_bearish = sweep_bar["close"] < sweep_bar["open"]
    sweep_is_bullish = sweep_bar["close"] > sweep_bar["open"]

    # ── Reversal body ratio check ──────────────────────────────────────────────
    if reversal_range == 0:
        return None

    reversal_body      = abs(reversal_bar["close"] - reversal_bar["open"])
    reversal_body_ratio = reversal_body / reversal_range

    if reversal_body_ratio < REVERSAL_MIN_BODY_RATIO:
        return None   # weak reversal, too much wick

    reversal_is_bullish = reversal_bar["close"] > reversal_bar["open"]
    reversal_is_bearish = reversal_bar["close"] < reversal_bar["open"]

    # ── Match: sweep down → reversal up (long setup) ──────────────────────────
    if sweep_is_bearish and reversal_is_bullish:
        return {
            "direction":           "long",
            "sweep_low":           sweep_bar["low"],
            "sweep_high":          sweep_bar["high"],
            "reversal_close":      reversal_bar["close"],
            "sweep_candle_size":   sweep_range,
            "avg_atr":             avg_atr,
            "sweep_multiplier":    sweep_multiplier,
            "reversal_body_ratio": reversal_body_ratio,
        }

    # ── Match: sweep up → reversal down (short setup) ─────────────────────────
    if sweep_is_bullish and reversal_is_bearish:
        return {
            "direction":           "short",
            "sweep_low":           sweep_bar["low"],
            "sweep_high":          sweep_bar["high"],
            "reversal_close":      reversal_bar["close"],
            "sweep_candle_size":   sweep_range,
            "avg_atr":             avg_atr,
            "sweep_multiplier":    sweep_multiplier,
            "reversal_body_ratio": reversal_body_ratio,
        }

    return None


def _build_signal(symbol: str, sweep: dict) -> Optional[Signal]:
    """
    From a detected sweep, compute entry / SL / TP and validate R:R.
    """
    direction   = sweep["direction"]
    entry       = sweep["reversal_close"]
    avg_atr     = sweep["avg_atr"]
    atr_buffer  = avg_atr * SL_ATR_MULTIPLIER

    if direction == "long":
        stop_loss   = sweep["sweep_low"] - atr_buffer
        risk        = entry - stop_loss
        take_profit = entry + risk * TP_RR_TARGET
    else:  # short
        stop_loss   = sweep["sweep_high"] + atr_buffer
        risk        = stop_loss - entry
        take_profit = entry - risk * TP_RR_TARGET

    if risk <= 0:
        logger.warning(f"{symbol}: risk <= 0, skipping signal")
        return None

    rr = (abs(take_profit - entry)) / risk

    if rr < RISK_REWARD_MIN:
        logger.info(f"{symbol}: R:R {rr:.2f} < {RISK_REWARD_MIN}, skipping")
        return None

    # ── Confidence score (heuristic) ──────────────────────────────────────────
    # Higher multiplier + cleaner reversal = higher confidence
    conf = min(1.0, (
        0.4 * min(sweep["sweep_multiplier"] / 4.0, 1.0) +  # sweep size weight
        0.4 * min(sweep["reversal_body_ratio"] / 0.7, 1.0) +  # clean reversal
        0.2 * min(rr / 3.0, 1.0)  # good R:R
    ))

    reasoning = (
        f"Liquidity sweep detected on {symbol}. "
        f"Sweep candle = {sweep['sweep_multiplier']:.1f}× avg ATR. "
        f"Reversal body ratio = {sweep['reversal_body_ratio']:.0%}. "
        f"Direction: {direction.upper()}. "
        f"Entry: {entry:.4f}, SL: {stop_loss:.4f}, TP: {take_profit:.4f}, R:R: {rr:.2f}."
    )

    return Signal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=rr,
        confidence=conf,
        reasoning=reasoning,
        timestamp=datetime.now(timezone.utc),
        sweep_candle_size=sweep["sweep_candle_size"],
        avg_atr=avg_atr,
    )


def scan(symbol: str, df_1h: pd.DataFrame, df_15m: pd.DataFrame) -> Optional[Signal]:
    """
    Main entry point. Returns a Signal if a valid setup is found, else None.

    Args:
        symbol:  instrument symbol (e.g. "BTC/USD")
        df_1h:   OHLCV DataFrame at 1H resolution (≥200 bars recommended)
        df_15m:  OHLCV DataFrame at 15min resolution (≥50 bars recommended)
                 Columns required: open, high, low, close, volume

    Returns:
        Signal dataclass or None
    """
    # ── 1. Trend filter ────────────────────────────────────────────────────────
    trend = _trend_from_bars(df_1h)
    if trend is None:
        logger.info(f"{symbol}: no clear trend (mixed EMA), skipping")
        return None

    logger.debug(f"{symbol}: trend = {trend}")

    # ── 2. Sweep detection ─────────────────────────────────────────────────────
    sweep = _detect_sweep(df_15m)
    if sweep is None:
        logger.debug(f"{symbol}: no sweep detected")
        return None

    # ── 3. Trend alignment check ───────────────────────────────────────────────
    if trend == "bullish" and sweep["direction"] != "long":
        logger.info(f"{symbol}: sweep is short but trend is bullish — skipping")
        return None
    if trend == "bearish" and sweep["direction"] != "short":
        logger.info(f"{symbol}: sweep is long but trend is bearish — skipping")
        return None

    logger.info(f"{symbol}: sweep aligned with trend ({trend}), building signal...")

    # ── 4. Build and validate signal ───────────────────────────────────────────
    signal = _build_signal(symbol, sweep)
    if signal:
        logger.info(
            f"✅ SIGNAL: {symbol} {signal.direction.upper()} | "
            f"Entry={signal.entry:.4f} SL={signal.stop_loss:.4f} "
            f"TP={signal.take_profit:.4f} RR={signal.risk_reward:.2f} "
            f"Conf={signal.confidence:.0%}"
        )

    return signal


# ── Quick backtest / replay helper ─────────────────────────────────────────────
def replay_scanner(df_1h: pd.DataFrame, df_15m: pd.DataFrame,
                   symbol: str = "BACKTEST", window_15m: int = 52) -> list[Signal]:
    """
    Walk-forward replay of the scanner over historical data.

    df_1h and df_15m must be aligned (same time coverage).
    Returns list of all signals found.
    """
    signals = []
    total = len(df_15m)

    for i in range(window_15m, total):
        slice_15m = df_15m.iloc[:i].copy()
        # approximate 1H slice: 4× 15min bars ≈ 1H
        slice_1h_end = min(len(df_1h), i // 4 + 1)
        slice_1h = df_1h.iloc[:slice_1h_end].copy()

        sig = scan(symbol, slice_1h, slice_15m)
        if sig:
            signals.append(sig)

    return signals
