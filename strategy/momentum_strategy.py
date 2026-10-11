"""Simple EMA-crossover momentum strategy — runs alongside the VCP scanner.

Entry conditions (all 4 must pass):
  1. Price > 21 EMA  — stock is in a short-term uptrend
  2. 21 EMA > 50 EMA — medium-term trend is aligned
  3. Volume >= 1.5x 20-day average — institutional participation today
  4. Price within 12% of 20-day high — stock showing relative strength

Exit rules (first trigger wins):
  - Fixed profit target: +12% from entry (2.4:1 reward-to-risk)
  - Hard stop: -5% from entry
  - Time stop: 12 trading days with no target hit

Regime: allowed in BULL and CAUTIOUS (blocked in BEAR / EXTREME_VOL).
Position size: 8% of portfolio, capped by 1% portfolio risk at 5% stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from config.config import config
from utils.indicator_utils import ema, rsi, sma

logger = logging.getLogger("scanner.momentum")


@dataclass
class MomentumResult:
    symbol: str
    passes: bool
    failure_reason: str
    score: float          # 0–100, higher = better setup
    entry_price: float
    stop_loss: float
    target_price: float
    ema21: float
    ema50: float
    rsi14: float
    volume_ratio: float   # today's volume / 20d average


def check_momentum(symbol: str, df: pd.DataFrame) -> MomentumResult:
    """Run all momentum conditions for one stock.

    Args:
        symbol: NSE trading symbol.
        df:     Daily OHLCV DataFrame with at least 60 rows.

    Returns:
        MomentumResult with passes flag and scoring details.
    """
    _fail = lambda reason: MomentumResult(
        symbol=symbol, passes=False, failure_reason=reason,
        score=0.0, entry_price=0.0, stop_loss=0.0, target_price=0.0,
        ema21=0.0, ema50=0.0, rsi14=0.0, volume_ratio=0.0,
    )

    if len(df) < 60:
        return _fail("Insufficient data (need 60 days)")

    close = df["close"]
    volume = df["volume"]

    current_price = float(close.iloc[-1])
    e21 = float(ema(close, config.MOMENTUM_EMA_FAST).iloc[-1])
    e50 = float(ema(close, config.MOMENTUM_EMA_SLOW).iloc[-1])
    rsi14 = float(rsi(close, 14).iloc[-1])

    avg_vol_20d = float(volume.iloc[-21:-1].mean())  # prior 20 days, not today
    today_vol = float(volume.iloc[-1])
    vol_ratio = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0.0

    high_20d = float(close.tail(20).max())
    pct_from_high = (high_20d - current_price) / high_20d if high_20d > 0 else 1.0

    # ── Condition 1: price above fast EMA ────────────────────────────────
    if current_price <= e21:
        return _fail(f"Price {current_price:.2f} <= EMA21 {e21:.2f}")

    # ── Condition 2: fast EMA above slow EMA ─────────────────────────────
    if e21 <= e50:
        return _fail(f"EMA21 {e21:.2f} <= EMA50 {e50:.2f} (downtrend)")

    # ── Condition 3: volume confirmation ─────────────────────────────────
    if vol_ratio < config.MOMENTUM_VOLUME_MULTIPLIER:
        return _fail(
            f"Volume {today_vol:,.0f} is {vol_ratio:.1f}x avg "
            f"(need {config.MOMENTUM_VOLUME_MULTIPLIER}x)"
        )

    # ── Condition 4: within 12% of 20-day high ────────────────────────────
    if pct_from_high > config.MOMENTUM_MAX_PCT_FROM_HIGH:
        return _fail(
            f"Price {pct_from_high:.1%} below 20d high "
            f"(limit {config.MOMENTUM_MAX_PCT_FROM_HIGH:.0%})"
        )

    # ── RSI guard: not overbought or oversold (skip if NaN — insufficient data) ─
    import math
    if not math.isnan(rsi14) and not (config.MOMENTUM_RSI_MIN <= rsi14 <= config.MOMENTUM_RSI_MAX):
        return _fail(f"RSI {rsi14:.1f} outside [{config.MOMENTUM_RSI_MIN}, {config.MOMENTUM_RSI_MAX}]")

    # ── All conditions passed — calculate entry, stop, target ────────────
    stop_loss = round(current_price * (1 - config.MOMENTUM_STOP_PCT), 2)
    target_price = round(current_price * (1 + config.MOMENTUM_TARGET_PCT), 2)

    # Score: weighted combination of momentum signals (0–100)
    # Higher EMA separation = stronger trend
    ema_gap_score = min(100.0, ((e21 - e50) / e50) * 1000)
    # Higher volume ratio = more conviction (capped at 5x)
    vol_score = min(100.0, (vol_ratio - 1.5) / 3.5 * 100)
    # Closer to 20d high = stronger relative strength
    rs_score = (1.0 - pct_from_high / config.MOMENTUM_MAX_PCT_FROM_HIGH) * 100
    # RSI sweet spot: 55-65 scores highest
    rsi_score = max(0.0, 100.0 - abs(rsi14 - 60.0) * 4)

    score = round(
        ema_gap_score * 0.30
        + vol_score    * 0.35
        + rs_score     * 0.25
        + rsi_score    * 0.10,
        2,
    )

    logger.debug(
        "Momentum pass: %s price=%.2f EMA21=%.2f EMA50=%.2f vol=%.1fx RSI=%.1f score=%.1f",
        symbol, current_price, e21, e50, vol_ratio, rsi14, score,
    )

    return MomentumResult(
        symbol=symbol,
        passes=True,
        failure_reason="",
        score=score,
        entry_price=current_price,
        stop_loss=stop_loss,
        target_price=target_price,
        ema21=e21,
        ema50=e50,
        rsi14=rsi14,
        volume_ratio=vol_ratio,
    )
