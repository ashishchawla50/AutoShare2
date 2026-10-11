"""Partial profit booking and full exit management.

Profit tiers:
  Profit ≥ 20% → sell 33% of position
  Profit ≥ 30% → sell another 33%
  Remaining 34% runs with trailing stop

Partial sells use limit orders at LTP - 0.1% to minimise slippage.

Technical breakdown exits:
  - Close below 50-day EMA on volume > 1.5x average → EXIT
  - Close below VCP consolidation low → EXIT IMMEDIATELY (breakout failed)
  - Two consecutive closes below 10-day EMA (after being profitable) → REVIEW
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.config import config
from utils.indicator_utils import ema, volume_sma

logger = logging.getLogger("trading.profit")


@dataclass
class ProfitTier:
    profit_pct: float       # Trigger profit threshold (e.g., 0.20 = 20%)
    sell_fraction: float    # Fraction of ORIGINAL position to sell (e.g., 0.33)


# config.PARTIAL_PROFIT_TIERS; whatever is not sold runs with the trailing stop
PROFIT_TIERS: list[ProfitTier] = [
    ProfitTier(profit_pct=p, sell_fraction=f) for p, f in config.PARTIAL_PROFIT_TIERS
]


def check_partial_profit(
    entry_price: float,
    current_price: float,
    total_shares: int,
    partial_sold_shares: int,
) -> Optional[tuple[int, str]]:
    """Check if any partial profit tier should be triggered.

    Args:
        entry_price:          Entry price per share.
        current_price:        Current market price.
        total_shares:         Original position size.
        partial_sold_shares:  Shares already partially sold.

    Returns:
        (shares_to_sell, tier_description) or None if no tier triggered.
    """
    if entry_price <= 0:
        return None

    profit_pct = (current_price - entry_price) / entry_price
    shares_already_sold_pct = partial_sold_shares / total_shares if total_shares > 0 else 0.0

    for tier in PROFIT_TIERS:
        expected_sold_pct = sum(
            t.sell_fraction for t in PROFIT_TIERS if t.profit_pct <= tier.profit_pct
        )
        if profit_pct >= tier.profit_pct and shares_already_sold_pct < expected_sold_pct:
            shares_to_sell = int(total_shares * tier.sell_fraction)
            if shares_to_sell > 0:
                description = (
                    f"Profit {profit_pct:.1%} triggered tier {int(tier.profit_pct*100)}%: "
                    f"selling {shares_to_sell} shares"
                )
                return shares_to_sell, description
    return None


def check_technical_breakdown(
    symbol: str,
    daily_ohlcv: pd.DataFrame,
    entry_price: float,
    vcp_consolidation_low: Optional[float] = None,
) -> tuple[bool, str]:
    """Check for technical breakdown conditions requiring exit.

    Args:
        symbol:                 NSE trading symbol (for logging).
        daily_ohlcv:            Recent daily OHLCV.
        entry_price:            Entry price (to determine if position is profitable).
        vcp_consolidation_low:  VCP base low; if price closes below this, exit immediately.

    Returns:
        (should_exit: bool, reason: str)
    """
    if len(daily_ohlcv) < config.BREAKDOWN_MIN_HISTORY:
        return False, ""

    close = daily_ohlcv["close"]
    volume = daily_ohlcv["volume"]
    current_close = float(close.iloc[-1])
    current_vol = float(volume.iloc[-1])

    # ── Rule 1: Close below VCP consolidation low → immediate exit ─────────
    if vcp_consolidation_low and current_close < vcp_consolidation_low:
        return True, f"Close {current_close:.2f} below VCP low {vcp_consolidation_low:.2f} — breakout failed"

    # ── Rule 2: Close below 50-day EMA on elevated volume ─────────────────
    ema50 = float(ema(close, config.BREAKDOWN_EMA_DAYS).iloc[-1])
    avg_vol = float(volume_sma(volume, config.BREAKDOWN_VOLUME_AVG_DAYS).iloc[-1])

    if current_close < ema50 and current_vol > avg_vol * config.BREAKDOWN_VOLUME_MULT:
        return True, (
            f"Close {current_close:.2f} below 50-day EMA {ema50:.2f} "
            f"on {current_vol/avg_vol:.1f}x volume"
        )

    # ── Rule 3: Two consecutive closes below 10-day EMA (after being profitable) ──
    if entry_price > 0:
        profit_pct = (current_close - entry_price) / entry_price
        if profit_pct > 0:  # only trigger if we've been profitable
            ema10_series = ema(close, config.REVIEW_EMA_DAYS)
            if len(ema10_series) >= 2:
                today_below = float(close.iloc[-1]) < float(ema10_series.iloc[-1])
                yesterday_below = float(close.iloc[-2]) < float(ema10_series.iloc[-2])
                if today_below and yesterday_below:
                    logger.warning(
                        "%s: Two consecutive closes below 10-day EMA — consider exit",
                        symbol,
                    )
                    # Review signal — not a hard exit rule, but flag for operator
    return False, ""


def should_exit_on_time_stop(
    entry_date,
    current_date,
    entry_price: float,
    current_price: float,
) -> tuple[bool, str]:
    """Check if the time-stop exit should trigger.

    Exit if profit < 7% after 15 trading days.

    Args:
        entry_date:    Date position was opened.
        current_date:  Today's date.
        entry_price:   Entry price per share.
        current_price: Current price per share.

    Returns:
        (should_exit: bool, reason: str)
    """
    from config.holidays import trading_days_between
    import datetime

    if isinstance(entry_date, str):
        entry_date = datetime.date.fromisoformat(entry_date)
    if isinstance(current_date, str):
        current_date = datetime.date.fromisoformat(current_date)

    trading_days_held = trading_days_between(entry_date, current_date)

    if trading_days_held < config.TIME_STOP_DAYS:
        return False, ""

    profit_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0

    if profit_pct < config.TIME_STOP_MIN_PROFIT_PCT:
        return True, (
            f"Time stop: {trading_days_held} days held, "
            f"profit {profit_pct:.1%} < {config.TIME_STOP_MIN_PROFIT_PCT:.0%} threshold"
        )
    return False, ""
