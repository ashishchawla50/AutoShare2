"""Minervini Trend Template — all 8 conditions must pass simultaneously.

The trend template ensures we only buy stocks in a confirmed stage 2 uptrend
with proper EMA alignment. No exceptions, no overrides.

Requires minimum 210 trading days of data to compute all EMAs reliably.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from utils.indicator_utils import ema

logger = logging.getLogger("scanner.trend_template")


@dataclass
class TrendTemplateResult:
    passes: bool
    conditions: dict[str, bool] = field(default_factory=dict)
    current_price: float = 0.0
    ema50: float = 0.0
    ema150: float = 0.0
    ema200: float = 0.0
    ema200_20d_ago: float = 0.0
    low_52w: float = 0.0
    high_52w: float = 0.0
    pct_above_52w_low: float = 0.0
    pct_below_52w_high: float = 0.0
    conditions_met: int = 0


def passes_trend_template(daily_ohlcv: pd.DataFrame) -> TrendTemplateResult:
    """Check all 8 Minervini Trend Template conditions.

    Args:
        daily_ohlcv: DataFrame with DatetimeIndex and [open, high, low, close, volume].
                     Minimum 210 trading days required.

    Returns:
        TrendTemplateResult with conditions dict and overall pass/fail.
    """
    close = daily_ohlcv["close"]

    if len(close) < 210:
        logger.debug(
            "Insufficient data for trend template: %d days (need 210)", len(close)
        )
        return TrendTemplateResult(passes=False, conditions={}, current_price=0.0)

    current = float(close.iloc[-1])
    low_52w = float(close.tail(252).min())
    high_52w = float(close.tail(252).max())

    e50 = float(ema(close, 50).iloc[-1])
    e150 = float(ema(close, 150).iloc[-1])
    e200 = float(ema(close, 200).iloc[-1])
    # 20 trading days ≈ 4 calendar weeks
    e200_20d_ago = float(ema(close, 200).iloc[-20])

    conditions = {
        "c1_price_above_ema50": current > e50,
        "c2_price_above_ema150": current > e150,
        "c3_price_above_ema200": current > e200,
        "c4_ema50_above_ema150": e50 > e150,
        "c5_ema150_above_ema200": e150 > e200,
        # EMA200 must be trending up for at least 4 weeks
        "c6_ema200_trending_up": e200 > e200_20d_ago,
        # Price at least 25% above 52-week low (in an uptrend)
        "c7_price_25pct_above_52w_low": current >= low_52w * 1.25,
        # Price within 25% of 52-week high (not in deep correction)
        "c8_price_within_25pct_of_52w_high": current >= high_52w * 0.75,
    }

    all_pass = all(conditions.values())
    conditions_met = sum(1 for v in conditions.values() if v)
    pct_above_52w_low = (current - low_52w) / low_52w if low_52w > 0 else 0.0
    pct_below_52w_high = (high_52w - current) / high_52w if high_52w > 0 else 0.0

    if not all_pass:
        failing = [k for k, v in conditions.items() if not v]
        logger.debug("Trend template failed (%d/8): %s", conditions_met, failing)

    return TrendTemplateResult(
        passes=all_pass,
        conditions=conditions,
        current_price=current,
        ema50=e50,
        ema150=e150,
        ema200=e200,
        ema200_20d_ago=e200_20d_ago,
        low_52w=low_52w,
        high_52w=high_52w,
        pct_above_52w_low=pct_above_52w_low,
        pct_below_52w_high=pct_below_52w_high,
        conditions_met=conditions_met,
    )


def trend_template_score(result: TrendTemplateResult) -> float:
    """Return a 0–100 score indicating trend template strength.

    A stock with all 8 conditions met = 100. Partial credit for near-misses.
    Used only for ranking, not as an entry gate.
    """
    return (result.conditions_met / 8) * 100
