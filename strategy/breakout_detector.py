"""Breakout signal detection and gap management.

A valid breakout requires ALL of:
  1. Close > pivot price (consolidation high)
  2. Strong close: close ≥ high * 0.96 (top 4% of day's range)
  3. Volume surge: volume ≥ 2x 50-day average volume
  4. Healthy intraday range: (high - low) / low > 0.5%
  5. Not extended: close ≤ pivot * 1.05 (not chasing)

Entry timing:
  - Buy at market open NEXT day (not on the breakout day)
  - If gap-up open > 5% above pivot → SKIP (overextended)
  - If open already > 5% past pivot → SKIP
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd

from config.config import config
from utils.indicator_utils import volume_sma

logger = logging.getLogger("scanner.breakout")


class GapDecision(str, Enum):
    PROCEED = "PROCEED"
    SKIP = "SKIP"


@dataclass
class BreakoutResult:
    is_valid: bool
    reason: str
    pivot_price: float
    close_price: float
    volume: float
    avg_volume_50d: float
    volume_multiplier: float
    close_strength: float    # close as fraction of day's range (1.0 = closed at high)


def is_valid_breakout(
    today_ohlcv: dict,
    pivot_price: float,
    avg_volume_50d: float,
) -> BreakoutResult:
    """Check if today's candle is a valid VCP breakout.

    Args:
        today_ohlcv:    Dict with keys: open, high, low, close, volume.
        pivot_price:    The VCP pivot (consolidation high).
        avg_volume_50d: 50-day simple moving average of volume.

    Returns:
        BreakoutResult with is_valid flag and reason string.
    """
    o = float(today_ohlcv["open"])
    h = float(today_ohlcv["high"])
    l = float(today_ohlcv["low"])
    c = float(today_ohlcv["close"])
    v = float(today_ohlcv["volume"])

    if h <= l or h <= 0 or avg_volume_50d <= 0:
        return BreakoutResult(
            is_valid=False,
            reason="Invalid OHLCV data",
            pivot_price=pivot_price,
            close_price=c,
            volume=v,
            avg_volume_50d=avg_volume_50d,
            volume_multiplier=0,
            close_strength=0,
        )

    vol_mult = v / avg_volume_50d
    day_range = h - l
    close_strength = (c - l) / day_range if day_range > 0 else 0.0

    conditions: dict[str, tuple[bool, str]] = {
        "price_above_pivot": (c > pivot_price, f"close {c:.2f} ≤ pivot {pivot_price:.2f}"),
        "strong_close": (
            c >= h * config.STRONG_CLOSE_THRESHOLD,
            f"close {c:.2f} not in top 4% of range (high={h:.2f})",
        ),
        "bullish_body": (
            c > o,
            f"close {c:.2f} ≤ open {o:.2f} — bearish candle on breakout day (Gujral)",
        ),
        "volume_surge": (
            v >= avg_volume_50d * config.MIN_BREAKOUT_VOLUME_MULTIPLIER,
            f"volume {vol_mult:.1f}x < {config.MIN_BREAKOUT_VOLUME_MULTIPLIER}x avg",
        ),
        "healthy_range": (
            day_range / l > config.PIVOT_BREAKOUT_MIN_DAY_RANGE_PCT,
            f"intraday range {day_range/l:.2%} < {config.PIVOT_BREAKOUT_MIN_DAY_RANGE_PCT:.1%}",
        ),
        "not_extended": (
            c <= pivot_price * (1 + config.MAX_ENTRY_EXTENSION_PCT),
            f"close {c:.2f} is {(c-pivot_price)/pivot_price:.1%} past pivot (max {config.MAX_ENTRY_EXTENSION_PCT:.0%})",
        ),
    }

    for condition_name, (passed, failure_msg) in conditions.items():
        if not passed:
            return BreakoutResult(
                is_valid=False,
                reason=f"{condition_name}: {failure_msg}",
                pivot_price=pivot_price,
                close_price=c,
                volume=v,
                avg_volume_50d=avg_volume_50d,
                volume_multiplier=vol_mult,
                close_strength=close_strength,
            )

    return BreakoutResult(
        is_valid=True,
        reason="",
        pivot_price=pivot_price,
        close_price=c,
        volume=v,
        avg_volume_50d=avg_volume_50d,
        volume_multiplier=vol_mult,
        close_strength=close_strength,
    )


def evaluate_gap(
    prev_close: float,
    today_open: float,
    pivot_price: float,
) -> tuple[GapDecision, str]:
    """Evaluate the gap-up at open on the entry day.

    Called on the NEXT morning (entry day) before placing the order.

    Args:
        prev_close:  Prior trading day's close price.
        today_open:  Pre-market or intraday open price.
        pivot_price: The VCP pivot price.

    Returns:
        (GapDecision, reason_string)
    """
    if prev_close <= 0:
        return GapDecision.SKIP, "Invalid previous close"

    gap_pct = (today_open - prev_close) / prev_close

    if gap_pct > config.MAX_GAP_UP_PCT:
        return GapDecision.SKIP, f"Gap-up {gap_pct:.1%} > {config.MAX_GAP_UP_PCT:.0%} — R/R destroyed"

    if today_open > pivot_price * (1 + config.MAX_ENTRY_EXTENSION_PCT):
        return GapDecision.SKIP, (
            f"Open {today_open:.2f} already {(today_open-pivot_price)/pivot_price:.1%} "
            f"past pivot {pivot_price:.2f}"
        )

    if gap_pct < -config.MAX_GAP_DOWN_PCT:
        return GapDecision.SKIP, f"Gap-down {gap_pct:.1%} on breakout stock — setup failed"

    return GapDecision.PROCEED, ""


def get_avg_volume_50d(daily_ohlcv: pd.DataFrame) -> float:
    """Return the PIVOT_BREAKOUT_VOLUME_AVG_DAYS simple moving average of volume."""
    if len(daily_ohlcv) < config.PIVOT_BREAKOUT_VOLUME_AVG_DAYS:
        return float(daily_ohlcv["volume"].mean())
    return float(volume_sma(daily_ohlcv["volume"], config.PIVOT_BREAKOUT_VOLUME_AVG_DAYS).iloc[-1])
