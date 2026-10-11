"""Weinstein Stage Analysis — classify stocks into Stage 1/2/3/4.

Only trade stocks in Stage 2 (Advancing). Never trade Stage 4 (Declining).

Stage detection uses the 30-week Simple Moving Average as the primary anchor,
with volume confirmation (up-weeks should have higher average volume than down-weeks).
Requires minimum 40 weeks of data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

import pandas as pd

from config.config import config
from utils.indicator_utils import daily_to_weekly, sma

logger = logging.getLogger("scanner.stage")


class Stage(IntEnum):
    BASING = 1       # Flat base; trading below or at declining MA
    ADVANCING = 2    # Rising, above rising 30-week MA — buy zone
    TOPPING = 3      # Price above MA but MA flattening/rolling over
    DECLINING = 4    # Below declining MA — danger zone


@dataclass
class StageResult:
    stage: Stage
    current_price: float
    ma30w: float
    ma_rising: bool
    price_above_ma: bool
    volume_confirmed: bool
    weeks_available: int


def detect_stage(daily_ohlcv: pd.DataFrame) -> StageResult:
    """Detect Weinstein stage from daily OHLCV.

    Args:
        daily_ohlcv: DataFrame with DatetimeIndex and [open, high, low, close, volume].
                     Must have at least 40 weeks of data (~200 trading days).

    Returns:
        StageResult with stage classification and supporting metrics.
    """
    if len(daily_ohlcv) < 200:
        logger.debug("Insufficient data for stage analysis (%d rows)", len(daily_ohlcv))
        return StageResult(
            stage=Stage.BASING,
            current_price=0.0,
            ma30w=0.0,
            ma_rising=False,
            price_above_ma=False,
            volume_confirmed=False,
            weeks_available=0,
        )

    weekly = daily_to_weekly(daily_ohlcv)
    n_weeks = len(weekly)

    if n_weeks < 40:
        return StageResult(
            stage=Stage.BASING,
            current_price=float(daily_ohlcv["close"].iloc[-1]),
            ma30w=0.0,
            ma_rising=False,
            price_above_ma=False,
            volume_confirmed=False,
            weeks_available=n_weeks,
        )

    ma30 = sma(weekly["close"], config.WEINSTEIN_MA_PERIOD_WEEKS)
    current_price = float(weekly["close"].iloc[-1])
    current_ma = float(ma30.iloc[-1])
    ma_4w_ago = float(ma30.iloc[-4])
    ma_8w_ago = float(ma30.iloc[-8])

    price_above_ma = current_price > current_ma
    # MA is rising if current > 4 weeks ago > 8 weeks ago (consistent uptrend)
    ma_rising = current_ma > ma_4w_ago > ma_8w_ago
    price_trend_up = float(weekly["close"].iloc[-1]) > float(weekly["close"].iloc[-4])

    # Volume confirmation: advancing weeks should have higher avg volume than declining weeks
    last_8w = weekly.tail(8)
    up_weeks = last_8w[last_8w["close"] >= last_8w["open"]]
    down_weeks = last_8w[last_8w["close"] < last_8w["open"]]

    if len(up_weeks) > 0 and len(down_weeks) > 0:
        volume_confirmed = float(up_weeks["volume"].mean()) > float(down_weeks["volume"].mean())
    elif len(up_weeks) > 0:
        volume_confirmed = True  # all up-weeks, positive by default
    else:
        volume_confirmed = False

    # Stage classification logic
    if price_above_ma and ma_rising and price_trend_up and volume_confirmed:
        stage = Stage.ADVANCING
    elif price_above_ma and not ma_rising:
        # Price above MA but MA rolling over → topping
        stage = Stage.TOPPING
    elif not price_above_ma and current_ma < ma_4w_ago:
        # Below declining MA → Stage 4
        stage = Stage.DECLINING
    else:
        # Base or transition
        stage = Stage.BASING

    return StageResult(
        stage=stage,
        current_price=current_price,
        ma30w=current_ma,
        ma_rising=ma_rising,
        price_above_ma=price_above_ma,
        volume_confirmed=volume_confirmed,
        weeks_available=n_weeks,
    )


def is_stage2(daily_ohlcv: pd.DataFrame) -> tuple[bool, StageResult]:
    """Convenience function: return (is_stage2, result) for a stock.

    Args:
        daily_ohlcv: Daily OHLCV DataFrame.

    Returns:
        (True if Stage 2, StageResult with details)
    """
    result = detect_stage(daily_ohlcv)
    return result.stage == Stage.ADVANCING, result
