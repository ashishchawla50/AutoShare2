"""Real-time portfolio tracker: coordinates positions, P&L, and daily metrics.

This is the high-level orchestration layer that ties together
PortfolioManager, DrawdownMonitor, and P&L calculation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from broker.portfolio_manager import PortfolioManager
from config.config import config
from portfolio.pnl_calculator import TradeCosts, calculate_net_pnl
from utils.time_utils import today_ist

logger = logging.getLogger("trading.portfolio")


class PortfolioTracker:
    """Unified view of portfolio state with real-time metrics.

    Args:
        portfolio_manager: The PortfolioManager instance.
    """

    def __init__(self, portfolio_manager: PortfolioManager) -> None:
        self._pm = portfolio_manager
        self._db_path = Path(config.DB_PATH) / "portfolio_state.db"
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._costs = TradeCosts()

    def snapshot(self) -> dict:
        """Return a complete portfolio snapshot for reporting and alerting."""
        positions = self._pm.get_open_positions()
        portfolio_value = self._pm.get_portfolio_value()
        available_cash = self._pm.get_available_cash()
        peak_value = self._pm.get_peak_value()
        drawdown = self._pm.get_drawdown_pct()

        invested = sum(
            float(p["current_price"] or p["entry_price"]) * int(p["shares"])
            for p in positions
        )
        cash_pct = available_cash / portfolio_value if portfolio_value > 0 else 0.0

        unrealised_pnl = sum(
            (float(p["current_price"] or p["entry_price"]) - float(p["entry_price"])) * int(p["shares"])
            for p in positions
        )

        return {
            "date": today_ist(),
            "portfolio_value": portfolio_value,
            "cash": available_cash,
            "invested": invested,
            "cash_pct": cash_pct,
            "unrealised_pnl": unrealised_pnl,
            "open_positions_count": len(positions),
            "peak_value": peak_value,
            "drawdown_pct": drawdown,
            "positions": positions,
        }

    def record_daily_metrics(self, regime: str = "") -> None:
        """Persist today's portfolio metrics to the database."""
        snap = self.snapshot()
        today = today_ist()

        try:
            existing = pd.read_sql(
                f"SELECT id FROM portfolio_metrics WHERE date='{today}'",
                self._engine,
            )
            with self._engine.begin() as conn:
                if not existing.empty:
                    conn.execute(
                        text(
                            "UPDATE portfolio_metrics SET portfolio_value=:pv, cash=:cash, "
                            "invested=:inv, total_pnl=:pnl, peak_value=:peak, "
                            "drawdown_pct=:dd, open_positions_count=:op, regime=:reg "
                            "WHERE date=:d"
                        ),
                        {
                            "pv": snap["portfolio_value"],
                            "cash": snap["cash"],
                            "inv": snap["invested"],
                            "pnl": snap["unrealised_pnl"],
                            "peak": snap["peak_value"],
                            "dd": snap["drawdown_pct"],
                            "op": snap["open_positions_count"],
                            "reg": regime,
                            "d": today,
                        },
                    )
                else:
                    conn.execute(
                        text(
                            "INSERT INTO portfolio_metrics "
                            "(date, portfolio_value, cash, invested, total_pnl, "
                            "peak_value, drawdown_pct, open_positions_count, regime) "
                            "VALUES (:d, :pv, :cash, :inv, :pnl, :peak, :dd, :op, :reg)"
                        ),
                        {
                            "d": today,
                            "pv": snap["portfolio_value"],
                            "cash": snap["cash"],
                            "inv": snap["invested"],
                            "pnl": snap["unrealised_pnl"],
                            "peak": snap["peak_value"],
                            "dd": snap["drawdown_pct"],
                            "op": snap["open_positions_count"],
                            "reg": regime,
                        },
                    )
        except Exception as exc:
            logger.error("Failed to record daily metrics: %s", exc)

    def get_trade_history(self, days: int = None) -> pd.DataFrame:
        """Return closed trade history for the last N days."""
        cutoff = today_ist().isoformat()
        try:
            return pd.read_sql(
                f"SELECT * FROM trades ORDER BY exit_date DESC LIMIT {int(config.TRADE_HISTORY_MAX_ROWS)}",
                self._engine,
            )
        except Exception as exc:
            logger.error("Failed to load trade history: %s", exc)
            return pd.DataFrame()

    def get_performance_summary(self) -> dict:
        """Return key performance metrics from trade history."""
        history = self.get_trade_history(days=config.TRADE_HISTORY_YEAR_DAYS)
        if history.empty:
            return {"trades": 0}

        wins = history[history["realised_pnl"] > 0]
        losses = history[history["realised_pnl"] <= 0]
        total_trades = len(history)
        win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
        avg_win = float(wins["realised_pnl_pct"].mean()) if not wins.empty else 0.0
        avg_loss = float(losses["realised_pnl_pct"].mean()) if not losses.empty else 0.0
        profit_factor = (
            float(wins["realised_pnl"].sum()) / abs(float(losses["realised_pnl"].sum()))
            if not losses.empty and losses["realised_pnl"].sum() != 0
            else float("inf")
        )

        return {
            "trades": total_trades,
            "win_rate": win_rate,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "profit_factor": profit_factor,
            "total_pnl": float(history["realised_pnl"].sum()),
        }
