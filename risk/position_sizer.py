"""Position size calculation based on risk per trade.

Formula: shares = min(risk_amount / risk_per_share, max_position_cap)

Inputs:
  portfolio_value: Total portfolio value
  entry_price:     Expected entry price
  stop_loss_price: Stop loss price (hard stop)
  risk_pct:        Fraction of portfolio to risk (default 1.5%)
  max_position_pct: Max fraction of portfolio in one position (default 15%)

All money values use Python float. The caller is responsible for rounding
to the nearest whole share.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config.config import config
from regime.regime_detector import Regime

logger = logging.getLogger("risk.position_sizer")


@dataclass
class PositionSizeResult:
    shares: int
    position_value: float
    actual_risk_pct: float
    actual_position_pct: float
    capped_by: str    # "risk", "position_limit", or "sector_limit"
    is_valid: bool
    reason: str       # empty if valid


def calculate_position_size(
    portfolio_value: float,
    entry_price: float,
    stop_loss_price: float,
    risk_pct: float = None,
    max_position_pct: float = None,
    regime_multiplier: float = 1.0,
    available_cash: float = None,
) -> PositionSizeResult:
    """Calculate the number of shares to buy for a given risk budget.

    Args:
        portfolio_value:   Total portfolio value including cash.
        entry_price:       Expected entry price per share.
        stop_loss_price:   Hard stop loss price per share (must be < entry_price).
        risk_pct:          Fraction of portfolio to risk (default from config).
        max_position_pct:  Max single-position fraction (default from config).
        regime_multiplier: Multiplier from regime (0.5 for cautious, 1.0 for bull).
        available_cash:    Cash available for deployment (enforced as hard cap).

    Returns:
        PositionSizeResult with shares, values, and validation status.
    """
    risk_pct = risk_pct or config.RISK_PER_TRADE_PCT
    max_position_pct = max_position_pct or config.MAX_POSITION_SIZE_PCT

    if entry_price <= 0:
        return PositionSizeResult(0, 0, 0, 0, "invalid_price", False, "Entry price must be positive")
    if stop_loss_price >= entry_price:
        return PositionSizeResult(0, 0, 0, 0, "invalid_stop", False, "Stop loss must be below entry price")
    if portfolio_value <= 0:
        return PositionSizeResult(0, 0, 0, 0, "invalid_portfolio", False, "Portfolio value must be positive")

    # Apply regime multiplier (e.g., 0.5 in cautious mode)
    effective_risk_pct = risk_pct * regime_multiplier

    risk_amount = portfolio_value * effective_risk_pct
    risk_per_share = entry_price - stop_loss_price

    # Shares calculated from risk budget
    shares_by_risk = int(risk_amount / risk_per_share)

    # Shares calculated from position size cap
    max_position_value = portfolio_value * max_position_pct
    shares_by_position = int(max_position_value / entry_price)

    # Apply regime multiplier to position size cap too
    shares_by_position = int(shares_by_position * regime_multiplier)

    shares = min(shares_by_risk, shares_by_position)
    capped_by = "risk" if shares_by_risk <= shares_by_position else "position_limit"

    # Cash cap — cannot buy more than available cash allows
    if available_cash is not None and available_cash > 0:
        shares_by_cash = int(available_cash / entry_price)
        if shares_by_cash < shares:
            shares = shares_by_cash
            capped_by = "cash"

    if shares <= 0:
        return PositionSizeResult(0, 0, 0, 0, capped_by, False, "Position size rounds to 0 shares")

    position_value = shares * entry_price
    actual_risk = (shares * risk_per_share) / portfolio_value
    actual_position_pct = position_value / portfolio_value

    return PositionSizeResult(
        shares=shares,
        position_value=position_value,
        actual_risk_pct=actual_risk,
        actual_position_pct=actual_position_pct,
        capped_by=capped_by,
        is_valid=True,
        reason="",
    )


def calculate_stop_loss_price(entry_price: float, stop_pct: float = None) -> float:
    """Return the hard stop loss price for a given entry.

    Args:
        entry_price: Entry price per share.
        stop_pct:    Stop distance as fraction (default 7% from config).

    Returns:
        Stop loss price rounded to nearest 0.05 (NSE tick size).
    """
    stop_pct = stop_pct or config.HARD_STOP_PCT
    raw_stop = entry_price * (1 - stop_pct)
    return round(raw_stop / 0.05) * 0.05
