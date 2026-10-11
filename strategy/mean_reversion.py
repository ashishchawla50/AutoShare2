"""Short-term pullback mean reversion — Connors RSI-2 variant adapted for NSE.

Entry conditions (ALL required):
  1. Trend gate: close > 200-day EMA (long-term uptrend — the most important filter)
  2. RSI(2) < 10 — extreme short-term oversold; expect bounce back to the mean

Exit conditions (first to trigger wins):
  A. Close > 5-day EMA — price has mean-reverted to short-term average
  B. RSI(2) > 70 — short-term momentum restored
  C. Time stop: 7 trading days — cut if mean reversion fails to materialise
  D. Hard stop: 5% below entry — loss protection

Strategy character:
  - Win rate 60-70%, avg win 1.5-3%, avg loss 3-5% (losses larger than wins)
  - Anti-correlated with breakout: makes money in choppy months, bleeds in trends
  - On NSE, round-trip costs (~0.5%) consume 15-30% of every winning trade
  - 200-EMA gate eliminates most bear-market false signals

Cost note: The identical Connors RSI-2 system shows PF 2+ on US data where round trips
cost ~0.05%. On Indian delivery (CNC), the 0.5% round-trip cost cuts edge significantly.
Full-cost backtest (0.5% round trip modeled) is the only honest measure of viability.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import pandas as pd

from config.config import config
from utils.indicator_utils import ema, rsi

logger = logging.getLogger("scanner.mean_reversion")


@dataclass
class MeanReversionResult:
    symbol: str
    passes: bool
    failure_reason: str
    entry_price: float        # current close (buy at next open)
    stop_loss: float          # entry * (1 - MR_STOP_PCT)
    exit_ema5: float          # 5-day EMA today — exit when close crosses above this
    rsi2: float               # RSI(2) today — entry trigger
    ema200: float             # 200-day EMA today — trend gate
    consecutive_down: int     # consecutive down closes (context)
    score: float              # 0-100 for ranking same-day signals (lower RSI = better)


def check_mean_reversion(symbol: str, df: pd.DataFrame) -> MeanReversionResult:
    """Run mean-reversion entry conditions on one stock.

    Args:
        symbol: NSE trading symbol.
        df:     Daily OHLCV DataFrame. Needs at least MR_EMA_TREND + 10 rows (~210).

    Returns:
        MeanReversionResult with passes flag, entry price, stop, and exit levels.
    """
    _fail = lambda reason: MeanReversionResult(
        symbol=symbol, passes=False, failure_reason=reason,
        entry_price=0.0, stop_loss=0.0, exit_ema5=0.0,
        rsi2=0.0, ema200=0.0, consecutive_down=0, score=0.0,
    )

    min_bars = config.MR_EMA_TREND + 10
    if len(df) < min_bars:
        return _fail(f"Insufficient data ({len(df)} bars, need {min_bars})")

    close = df["close"]
    current_price = float(close.iloc[-1])

    e200 = float(ema(close, config.MR_EMA_TREND).iloc[-1])
    e5 = float(ema(close, config.MR_EMA_EXIT).iloc[-1])
    rsi2_val = float(rsi(close, config.MR_RSI_PERIOD).iloc[-1])

    if math.isnan(e200) or math.isnan(rsi2_val) or math.isnan(e5):
        return _fail("Indicator produced NaN (insufficient history)")

    # ── Gate 1: long-term uptrend ─────────────────────────────────────────
    if current_price <= e200:
        return _fail(
            f"Close {current_price:.2f} <= EMA200 {e200:.2f} — not in uptrend"
        )

    # ── Gate 2: RSI(2) oversold — short-term pullback signal ─────────────
    if rsi2_val >= config.MR_RSI_ENTRY:
        return _fail(
            f"RSI(2) {rsi2_val:.1f} >= {config.MR_RSI_ENTRY} — not oversold"
        )

    # ── All gates pass ────────────────────────────────────────────────────
    stop_loss = round(current_price * (1 - config.MR_STOP_PCT), 2)
    consecutive_down = _count_consecutive_down(close)

    # Score: lower RSI = deeper oversold = stronger signal
    rsi_score = max(0.0, (config.MR_RSI_ENTRY - rsi2_val) / config.MR_RSI_ENTRY * 100)
    consec_score = min(100.0, consecutive_down / 5 * 100)
    # Stocks close to 200-EMA are more stretched and riskier; prefer moderate distance
    pct_above_ema200 = (current_price / e200 - 1) * 100
    trend_score = max(0.0, 50.0 - abs(pct_above_ema200 - 10.0) * 2)
    score = round(rsi_score * 0.5 + consec_score * 0.3 + trend_score * 0.2, 2)

    logger.debug(
        "MR pass: %s price=%.2f EMA200=%.2f EMA5=%.2f RSI2=%.1f down=%d score=%.1f",
        symbol, current_price, e200, e5, rsi2_val, consecutive_down, score,
    )

    return MeanReversionResult(
        symbol=symbol,
        passes=True,
        failure_reason="",
        entry_price=current_price,
        stop_loss=stop_loss,
        exit_ema5=e5,
        rsi2=rsi2_val,
        ema200=e200,
        consecutive_down=consecutive_down,
        score=score,
    )


def should_exit_mean_reversion(
    current_close: float,
    ema5: float,
    rsi2: float,
) -> tuple[bool, str]:
    """Check if an open mean-reversion position has reached its exit condition.

    Called each bar for positions with strategy == 'mean_reversion'.

    Returns:
        (should_exit, reason_string)
    """
    if current_close > ema5:
        return True, "mr_ema5_exit"
    if rsi2 > config.MR_RSI_EXIT:
        return True, "mr_rsi_exit"
    return False, ""


def _count_consecutive_down(close: pd.Series) -> int:
    """Count consecutive down closes ending on today's bar (max 10)."""
    count = 0
    for i in range(1, min(11, len(close))):
        if float(close.iloc[-i]) < float(close.iloc[-(i + 1)]):
            count += 1
        else:
            break
    return count
