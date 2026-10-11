"""Hard stop and trailing stop management.

Hard stop: 7% below entry — placed as GTT on Zerodha. Non-negotiable.
Trailing stop: Ratchets up as profit increases. Never moves down.

Trailing rules:
  Profit ≥ 10% → move stop to breakeven
  Profit ≥ 15% → move stop to +5%
  Profit ≥ 20% → move stop to +10%
  Profit ≥ 30% → move stop to +20%

Dynamic floor: max(rule-based stop, 10-day EMA × 0.97)
The stop is ALWAYS the higher of the two.

CRITICAL: Never move the stop further from entry. Log any attempted violation
as a WARNING — it may indicate a bug or manual interference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config.config import config
from utils.indicator_utils import ema

logger = logging.getLogger("trading.stop_loss")


@dataclass
class TrailingRule:
    profit_pct: float    # Profit threshold to activate this rule
    trail_to: float      # Stop moves to this fraction above entry (e.g., 0.0 = breakeven, 0.05 = +5%)


# Trailing rules from spec — ordered from tightest to loosest profit threshold
TRAILING_RULES: list[TrailingRule] = [
    TrailingRule(profit_pct=0.10, trail_to=0.0),    # 10% profit → stop to breakeven
    TrailingRule(profit_pct=0.15, trail_to=0.05),   # 15% → stop to +5%
    TrailingRule(profit_pct=0.20, trail_to=0.10),   # 20% → stop to +10%
    TrailingRule(profit_pct=0.30, trail_to=0.20),   # 30% → stop to +20%
]


def calculate_trailing_stop(
    entry_price: float,
    current_price: float,
    current_stop: float,
    daily_ohlcv: pd.DataFrame,
) -> tuple[float, str]:
    """Calculate the new trailing stop level.

    The new stop is ALWAYS >= the current stop (ratchet mechanism).
    It's the MAXIMUM of:
      1. Rule-based stop (from trailing rules)
      2. Dynamic floor: 10-day EMA × 0.97

    Args:
        entry_price:   Entry price for this position.
        current_price: Latest close price.
        current_stop:  Current stop loss price.
        daily_ohlcv:   Recent daily OHLCV for 10-day EMA calculation.

    Returns:
        (new_stop_price, reason_string)
    """
    if entry_price <= 0:
        return current_stop, "Invalid entry price"

    profit_pct = (current_price - entry_price) / entry_price
    rule_based_stop = entry_price * (1 - config.HARD_STOP_PCT)  # hard stop as floor
    reason = "hard_stop"

    # Apply the highest applicable trailing rule
    for rule in reversed(TRAILING_RULES):
        if profit_pct >= rule.profit_pct:
            rule_stop = entry_price * (1 + rule.trail_to)
            if rule_stop > rule_based_stop:
                rule_based_stop = rule_stop
                reason = f"trailing_{int(rule.profit_pct*100)}pct_profit"
            break

    # Dynamic floor: 10-day EMA × 0.97
    ema_floor = _get_ema_floor(daily_ohlcv)
    if ema_floor > rule_based_stop:
        rule_based_stop = ema_floor
        reason = "ema10_floor"

    # Ratchet: new stop cannot be lower than current stop
    if rule_based_stop < current_stop:
        return current_stop, "ratchet_hold"

    if rule_based_stop > current_stop:
        logger.info(
            "Trailing stop updated: %.2f → %.2f (%s, profit=%.1f%%)",
            current_stop, rule_based_stop, reason, profit_pct * 100,
        )

    return rule_based_stop, reason


def _get_ema_floor(daily_ohlcv: pd.DataFrame, period: int = 10) -> float:
    """Return 10-day EMA × 0.97 as the dynamic stop floor."""
    if len(daily_ohlcv) < period:
        return 0.0
    ema_val = float(ema(daily_ohlcv["close"], period).iloc[-1])
    return ema_val * 0.97


def should_exit_on_stop(current_price: float, stop_price: float) -> bool:
    """Return True if the current price has triggered the stop loss."""
    return current_price <= stop_price


def calculate_hard_stop(entry_price: float) -> float:
    """Return the initial hard stop loss price (config.HARD_STOP_PCT below entry, 10% since 2026-10-11).

    Rounded to nearest 0.05 (NSE tick size).
    """
    raw = entry_price * (1 - config.HARD_STOP_PCT)
    return round(raw / 0.05) * 0.05


def validate_stop_not_widened(
    symbol: str,
    current_stop: float,
    proposed_stop: float,
) -> bool:
    """Return True if the proposed stop is >= current stop (never widen).

    Logs a CRITICAL warning if a stop is being moved further from entry.
    This should never happen — it's a code bug or manual override.
    """
    if proposed_stop < current_stop:
        logger.critical(
            "STOP WIDENING ATTEMPT for %s: current=%.2f proposed=%.2f — BLOCKED",
            symbol, current_stop, proposed_stop,
        )
        return False
    return True
