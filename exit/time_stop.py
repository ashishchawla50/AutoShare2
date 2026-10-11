"""Time-based exit evaluation.

Re-exports the time stop check from profit_manager for clean imports.
Also provides the regime-based exit trigger.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from exit.profit_manager import should_exit_on_time_stop
from regime.regime_detector import Regime, RegimeState

logger = logging.getLogger("trading.time_stop")


def check_regime_exit(
    symbol: str,
    regime_state: RegimeState,
    entry_date: date,
    current_date: Optional[date] = None,
    days_since_bear_start: int = 0,
) -> tuple[bool, str]:
    """Return (should_exit, reason) based on regime deterioration.

    Bear regime detected: exit within 3 trading days (largest losers first).
    VIX > 35: exit within 2 trading days.

    Args:
        symbol:                NSE trading symbol.
        regime_state:          Current RegimeState.
        entry_date:            Date position was opened.
        current_date:          Today's date (default: today).
        days_since_bear_start: How many trading days since regime became BEAR/EXTREME.

    Returns:
        (should_exit: bool, reason: str)
    """
    from utils.time_utils import today_ist
    today = current_date or today_ist()

    if regime_state.regime == Regime.EXTREME_VOLATILITY:
        if days_since_bear_start >= 2:
            return True, f"Extreme volatility exit (VIX={regime_state.india_vix:.1f})"

    if regime_state.regime == Regime.BEAR:
        if days_since_bear_start >= 3:
            return True, f"Bear regime exit (day {days_since_bear_start}/3)"

    return False, ""


__all__ = ["should_exit_on_time_stop", "check_regime_exit"]
