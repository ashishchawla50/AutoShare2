"""Drawdown and loss limit monitoring.

Monitors four loss tiers:
  daily  (3%)  → halt until end of trading day
  weekly (6%)  → halt until Monday
  monthly(10%) → halt until 1st of next month
  max_dd (20%) → emergency halt, manual restart required

Peak-drawdown tiered response (replaces consecutive-loss pause):
  10% from peak → CAUTIOUS: position sizes halved (signal only, enforced in risk_manager)
  15% from peak → HALT: no new entries until drawdown recovers below 12%
  20% from peak → EMERGENCY: halt + Telegram alert, manual restart required

Consecutive losses still send a Telegram alert but no longer pause trading —
a 3-loss streak at 45% win rate has a 15% random probability and doesn't
justify sitting out the next (potentially winning) trade.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from config.config import config
from utils.time_utils import today_ist

logger = logging.getLogger("risk.drawdown")


class DrawdownMonitor:
    """Tracks portfolio drawdown and loss-based trading halts.

    Args:
        portfolio_manager: The active PortfolioManager instance.
    """

    def __init__(self, portfolio_manager) -> None:
        self._pm = portfolio_manager
        self._db_path = Path(config.DB_PATH) / "portfolio_state.db"
        self._engine = create_engine(f"sqlite:///{self._db_path}")

    # ── Public check interface ────────────────────────────────────────────────

    def check_all_limits(self) -> tuple[bool, str]:
        """Run all drawdown and loss limit checks.

        Returns:
            (can_trade: bool, reason: str)
            reason is empty string if trading is allowed.
        """
        from utils.health_check import is_emergency_halt_active
        if is_emergency_halt_active():
            return False, "Emergency halt active — delete data/emergency_halt.flag to resume"

        ok, reason = self._check_drawdown()
        if not ok:
            return False, reason

        ok, reason = self._check_daily_loss()
        if not ok:
            return False, reason

        ok, reason = self._check_weekly_loss()
        if not ok:
            return False, reason

        ok, reason = self._check_monthly_loss()
        if not ok:
            return False, reason

        return True, ""

    def get_position_size_multiplier(self) -> float:
        """Return position size multiplier based on peak drawdown tier.

        1.0 = full size (drawdown < 10%)
        0.5 = half size (drawdown 10–15%)
        0.0 = no new trades (drawdown > 15%, handled by check_all_limits)
        """
        dd = self._pm.get_drawdown_pct()
        if dd >= 0.10:
            return 0.5
        return 1.0

    def record_trade_result(self, pnl: float) -> None:
        """Record a closed trade's P&L and update consecutive loss counter.

        Loss streaks trigger Telegram alerts only — not trading pauses.
        A 3-loss streak at 45% win rate has ~15% random probability; pausing
        would miss the next valid trade. Portfolio drawdown limits are the
        correct circuit breaker, not streak counting.

        Args:
            pnl: Realised profit or loss in rupees.
        """
        streak = self._pm.get_consecutive_losses()
        if pnl < 0:
            streak += 1
        else:
            streak = 0

        self._update_loss_tracking(consecutive_losses=streak)

        if streak >= config.EXTENDED_PAUSE_AT:
            self._alert_loss_streak(streak, extended=True)
        elif streak >= config.CONSECUTIVE_LOSS_PAUSE_AT:
            self._alert_loss_streak(streak, extended=False)

    # ── Individual limit checks ───────────────────────────────────────────────

    def _check_drawdown(self) -> tuple[bool, str]:
        """Tiered peak-drawdown response.

        10% → cautious (position sizes halved via get_position_size_multiplier)
        15% → halt new entries
        20% → emergency halt
        """
        dd = self._pm.get_drawdown_pct()
        if dd >= config.MAX_DRAWDOWN_PCT:
            self._trigger_emergency_halt(dd)
            return False, f"Emergency halt: {dd:.1%} drawdown from peak exceeds {config.MAX_DRAWDOWN_PCT:.0%} limit"
        if dd >= 0.15:
            return False, f"Drawdown halt: {dd:.1%} from peak exceeds 15% — no new entries until recovery below 12%"
        if dd >= 0.10:
            logger.warning("Drawdown caution: %.1f%% from peak — position sizes halved", dd * 100)
        return True, ""

    def _check_daily_loss(self) -> tuple[bool, str]:
        """Check today's realised loss against the daily limit."""
        today_pnl_pct = self._get_today_pnl_pct()
        if today_pnl_pct <= -config.DAILY_LOSS_LIMIT_PCT:
            return False, f"Daily loss limit hit: {today_pnl_pct:.2%} (limit {config.DAILY_LOSS_LIMIT_PCT:.0%})"
        return True, ""

    def _check_weekly_loss(self) -> tuple[bool, str]:
        """Check rolling-week realised loss against the weekly limit."""
        week_pnl_pct = self._get_rolling_pnl_pct(days=7)
        if week_pnl_pct <= -config.WEEKLY_LOSS_LIMIT_PCT:
            return False, f"Weekly loss limit hit: {week_pnl_pct:.2%} (limit {config.WEEKLY_LOSS_LIMIT_PCT:.0%})"
        return True, ""

    def _check_monthly_loss(self) -> tuple[bool, str]:
        """Check rolling-month realised loss against the monthly limit."""
        month_pnl_pct = self._get_rolling_pnl_pct(days=30)
        if month_pnl_pct <= -config.MONTHLY_LOSS_LIMIT_PCT:
            return False, f"Monthly loss limit hit: {month_pnl_pct:.2%} (limit {config.MONTHLY_LOSS_LIMIT_PCT:.0%})"
        return True, ""

    # ── Loss retrieval helpers ────────────────────────────────────────────────

    def _get_today_pnl_pct(self) -> float:
        """Return today's total realised P&L as a fraction of portfolio value."""
        today = today_ist()
        portfolio_value = self._pm.get_portfolio_value()
        if portfolio_value <= 0:
            return 0.0
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT SUM(realised_pnl) FROM trades WHERE exit_date = :d"),
                    {"d": str(today)},
                ).fetchone()
            total = float(row[0] or 0) if row and row[0] is not None else 0.0
            return total / portfolio_value
        except Exception:
            return 0.0

    def _get_rolling_pnl_pct(self, days: int) -> float:
        """Return rolling N-day total realised P&L as fraction of portfolio."""
        cutoff = today_ist() - timedelta(days=days)
        portfolio_value = self._pm.get_portfolio_value()
        if portfolio_value <= 0:
            return 0.0
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT SUM(realised_pnl) FROM trades WHERE exit_date >= :d"),
                    {"d": str(cutoff)},
                ).fetchone()
            total = float(row[0] or 0) if row and row[0] is not None else 0.0
            return total / portfolio_value
        except Exception:
            return 0.0

    # ── Trigger actions ───────────────────────────────────────────────────────

    def _trigger_emergency_halt(self, drawdown_pct: float) -> None:
        """Activate emergency halt and send critical alerts."""
        from utils.health_check import set_emergency_halt
        peak = self._pm.get_peak_value()
        current = self._pm.get_portfolio_value()
        set_emergency_halt(f"Drawdown {drawdown_pct:.1%} from peak ₹{peak:,.0f}")

        from alerts.telegram_alert import send_emergency_halt
        send_emergency_halt(
            drawdown_pct=drawdown_pct * 100,
            portfolio_value=current,
            peak_value=peak,
        )
        logger.critical("EMERGENCY HALT: drawdown=%.1f%%", drawdown_pct * 100)

    def _alert_loss_streak(self, streak: int, extended: bool) -> None:
        """Send Telegram alert for consecutive loss streak — no trading pause."""
        from alerts.telegram_alert import send_text
        label = "EXTENDED" if extended else "WARNING"
        msg = (
            f"[{label}] {streak} consecutive losses. "
            f"Portfolio drawdown: {self._pm.get_drawdown_pct():.1%} from peak. "
            f"Trading continues — review open positions."
        )
        send_text(msg)
        logger.warning("Loss streak alert: %d consecutive losses (extended=%s)", streak, extended)

    def _update_loss_tracking(self, consecutive_losses: Optional[int] = None) -> None:
        today = today_ist()
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO loss_tracking (date, consecutive_losses) "
                    "VALUES (:d, :cl)"
                ),
                {"d": today, "cl": consecutive_losses or 0},
            )
