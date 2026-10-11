"""Portfolio-level risk enforcement: sector limits, position count, cash reserve.

Called before every new trade entry to gate approval.
Returns a clear pass/fail with a human-readable reason for logging and alerts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config.config import config
from regime.regime_detector import Regime, RegimeState
from risk.drawdown_monitor import DrawdownMonitor
from risk.position_sizer import PositionSizeResult

logger = logging.getLogger("risk.manager")


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str
    suggested_shares: int
    regime_multiplier: float


class RiskManager:
    """Gates all new trade entries against portfolio-level risk rules.

    Args:
        portfolio_manager: Active PortfolioManager instance.
        drawdown_monitor:  Active DrawdownMonitor instance.
    """

    def __init__(self, portfolio_manager, drawdown_monitor: DrawdownMonitor) -> None:
        self._pm = portfolio_manager
        self._ddm = drawdown_monitor

    def approve_entry(
        self,
        symbol: str,
        sector: str,
        size_result: PositionSizeResult,
        regime_state: RegimeState,
    ) -> RiskCheckResult:
        """Run all pre-entry risk checks and return approval decision.

        Args:
            symbol:        NSE trading symbol for the proposed entry.
            sector:        Sector classification of the stock.
            size_result:   Pre-calculated position size result.
            regime_state:  Current market regime state.

        Returns:
            RiskCheckResult with approved flag and reason string.
        """
        # Combine regime multiplier with drawdown-tier multiplier (take the lower of the two)
        drawdown_multiplier = self._ddm.get_position_size_multiplier()
        multiplier = min(regime_state.position_size_multiplier, drawdown_multiplier)

        # ── Regime gate ──────────────────────────────────────────────────────
        if not regime_state.allow_new_trades:
            return RiskCheckResult(
                False,
                f"Regime {regime_state.regime.value} does not allow new entries",
                0,
                multiplier,
            )

        # ── Drawdown and loss limits ─────────────────────────────────────────
        can_trade, halt_reason = self._ddm.check_all_limits()
        if not can_trade:
            return RiskCheckResult(False, halt_reason, 0, multiplier)

        # ── Open position count ──────────────────────────────────────────────
        open_positions = self._pm.get_open_positions()
        if len(open_positions) >= config.MAX_POSITIONS:
            return RiskCheckResult(
                False,
                f"Max positions reached ({config.MAX_POSITIONS})",
                0,
                multiplier,
            )

        # ── Already in this stock ────────────────────────────────────────────
        if any(p["symbol"].upper() == symbol.upper() for p in open_positions):
            return RiskCheckResult(
                False,
                f"Already holding {symbol}",
                0,
                multiplier,
            )

        # ── Cash reserve check ───────────────────────────────────────────────
        available_cash = self._pm.get_available_cash()
        if available_cash < size_result.position_value:
            return RiskCheckResult(
                False,
                f"Insufficient cash: need ₹{size_result.position_value:,.0f}, available ₹{available_cash:,.0f}",
                0,
                multiplier,
            )

        # ── Sector exposure ──────────────────────────────────────────────────
        # Positions with no sector on record resolve to "Unknown" and are
        # excluded from the cap — same pass-by-default convention as the
        # fundamental filters when Screener.in data is unavailable.
        if sector != "Unknown":
            from data_fetcher.fundamental_fetcher import get_fundamentals

            sector_map: dict[str, str] = {}
            for p in open_positions:
                fund = get_fundamentals(p["symbol"]) or {}
                sector_map[p["symbol"]] = fund.get("sector", "Unknown")
            sector_exposure = self._pm.get_sector_exposure(sector_map)
            current_sector_pct = sector_exposure.get(sector, 0.0)
            portfolio_value = self._pm.get_portfolio_value()
            new_sector_pct = current_sector_pct + (size_result.position_value / portfolio_value)

            if new_sector_pct > config.MAX_SECTOR_EXPOSURE_PCT:
                return RiskCheckResult(
                    False,
                    f"Sector {sector} exposure would be {new_sector_pct:.1%} (limit {config.MAX_SECTOR_EXPOSURE_PCT:.0%})",
                    0,
                    multiplier,
                )

        # ── Position size validity ───────────────────────────────────────────
        if not size_result.is_valid or size_result.shares <= 0:
            return RiskCheckResult(
                False,
                f"Invalid position size: {size_result.reason}",
                0,
                multiplier,
            )

        logger.info(
            "Risk approved: %s %d shares ₹%.0f (%.1f%% portfolio, risk=%.2f%%)",
            symbol,
            size_result.shares,
            size_result.position_value,
            size_result.actual_position_pct * 100,
            size_result.actual_risk_pct * 100,
        )
        return RiskCheckResult(
            approved=True,
            reason="",
            suggested_shares=size_result.shares,
            regime_multiplier=multiplier,
        )

    def approve_exit(self, symbol: str) -> tuple[bool, str]:
        """Verify that an exit is valid for a given symbol.

        Args:
            symbol: NSE trading symbol to exit.

        Returns:
            (approved: bool, reason: str)
        """
        pos = self._pm.get_open_position(symbol)
        if pos is None:
            return False, f"No open position found for {symbol}"
        return True, ""
