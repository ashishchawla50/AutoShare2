"""20-day range breakout scanner — high-frequency signal generation.

Entry conditions (all 4 must pass):
  1. Today's close == highest close in last N days (new range high)
  2. Volume >= 1.5x prior 20-day average (institutional participation)
  3. Price > 50 EMA (basic uptrend — stock is not in a downtrend)
  4. RSI(14) between 45 and 75 (has momentum, not overbought)

Exit rules:
  - Fixed target: +10% from entry (2:1 R:R at 5% stop)
  - Hard stop: -5% from entry (quick cut)
  - Time stop: 7 trading days with no target hit

Design intent: deliberately simple — 4 conditions vs VCP's 16 — so it fires
1-3 times per day on a 200-stock universe. Quality is lower per trade than VCP,
but the higher frequency produces more total opportunity for the portfolio.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import pandas as pd

from config.config import config
from utils.indicator_utils import ema, rsi

logger = logging.getLogger("scanner.breakout")


@dataclass
class BreakoutScanResult:
    symbol: str
    passes: bool
    failure_reason: str
    entry_price: float
    stop_loss: float
    target_price: float
    volume_ratio: float     # today's volume / prior 20d avg
    rsi14: float
    ema50: float
    days_high: int          # how many days back is this a new high for
    score: float            # 0-100 for ranking among same-day signals


def check_range_breakout(symbol: str, df: pd.DataFrame) -> BreakoutScanResult:
    """Run 20-day range breakout conditions on one stock.

    Args:
        symbol: NSE trading symbol.
        df:     Daily OHLCV DataFrame — at least 60 rows required.

    Returns:
        BreakoutScanResult with passes flag and entry/exit prices.
    """
    _fail = lambda reason: BreakoutScanResult(
        symbol=symbol, passes=False, failure_reason=reason,
        entry_price=0.0, stop_loss=0.0, target_price=0.0,
        volume_ratio=0.0, rsi14=0.0, ema50=0.0, days_high=0, score=0.0,
    )

    min_bars = max(config.BREAKOUT_LOOKBACK_DAYS + 1, config.BREAKOUT_MIN_HISTORY)
    if len(df) < min_bars:
        return _fail(f"Insufficient data ({len(df)} bars, need {min_bars})")

    close = df["close"]
    volume = df["volume"]

    current_price = float(close.iloc[-1])
    lookback = config.BREAKOUT_LOOKBACK_DAYS

    # Highest close in the lookback window (excluding today)
    prior_high = float(close.iloc[-(lookback + 1):-1].max())
    e50 = float(ema(close, config.BREAKOUT_EMA_PERIOD).iloc[-1])
    rsi14 = float(rsi(close, config.BREAKOUT_RSI_PERIOD).iloc[-1])

    # Prior N-day average volume (excludes today — avoids self-reference)
    n_vol = config.BREAKOUT_VOLUME_AVG_LOOKBACK
    avg_vol_20d = float(volume.iloc[-(n_vol + 1):-1].mean())
    today_vol = float(volume.iloc[-1])
    vol_ratio = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0.0

    # ── Condition 1: today closes at a new N-day high ─────────────────────
    if current_price <= prior_high:
        return _fail(
            f"Close {current_price:.2f} not a new {lookback}d high "
            f"(prior high {prior_high:.2f})"
        )

    # ── Condition 2: volume confirmation ─────────────────────────────────
    if vol_ratio < config.BREAKOUT_VOLUME_MIN:
        return _fail(
            f"Volume {today_vol:,.0f} is {vol_ratio:.1f}x avg "
            f"(need {config.BREAKOUT_VOLUME_MIN}x)"
        )

    # ── Condition 3: price above 50 EMA (basic uptrend) ──────────────────
    if current_price <= e50:
        return _fail(f"Price {current_price:.2f} <= EMA50 {e50:.2f} (downtrend)")

    # ── Condition 4: RSI in momentum zone ────────────────────────────────
    if not math.isnan(rsi14):
        if rsi14 > config.BREAKOUT_RSI_MAX:
            return _fail(f"RSI {rsi14:.1f} > {config.BREAKOUT_RSI_MAX} (overbought)")
        if rsi14 < config.BREAKOUT_RSI_MIN:
            return _fail(f"RSI {rsi14:.1f} < {config.BREAKOUT_RSI_MIN} (no momentum)")

    # ── All pass — compute entry, stop, target ────────────────────────────
    stop_loss = round(current_price * (1 - config.BREAKOUT_STOP_PCT), 2)
    target_price = round(current_price * (1 + config.BREAKOUT_TARGET_PCT), 2)

    # How many days back is this a new high? (more days = stronger breakout)
    days_high = _count_days_high(close, lookback)

    # Score: 0-100
    vol_score = min(100.0, (vol_ratio - config.BREAKOUT_SCORE_VOL_FLOOR)
                    / config.BREAKOUT_SCORE_VOL_SPAN * 100)
    rsi_score = max(0.0, 100.0 - abs(rsi14 - config.BREAKOUT_SCORE_RSI_CENTER)
                    * config.BREAKOUT_SCORE_RSI_SLOPE)
    days_score = min(100.0, days_high / lookback * 100)
    w_vol, w_rsi, w_days = config.BREAKOUT_SCORE_WEIGHTS
    score = round(vol_score * w_vol + rsi_score * w_rsi + days_score * w_days, 2)

    logger.debug(
        "Breakout pass: %s price=%.2f EMA50=%.2f vol=%.1fx RSI=%.1f %dd-high score=%.1f",
        symbol, current_price, e50, vol_ratio, rsi14, days_high, score,
    )

    return BreakoutScanResult(
        symbol=symbol,
        passes=True,
        failure_reason="",
        entry_price=current_price,
        stop_loss=stop_loss,
        target_price=target_price,
        volume_ratio=vol_ratio,
        rsi14=rsi14,
        ema50=e50,
        days_high=days_high,
        score=score,
    )


def _count_days_high(close: pd.Series, lookback: int) -> int:
    """Return how many consecutive days back this is a new high."""
    current = float(close.iloc[-1])
    count = lookback
    for i in range(2, lookback + 2):
        if float(close.iloc[-i]) >= current:
            count = i - 2
            break
    return max(1, count)
