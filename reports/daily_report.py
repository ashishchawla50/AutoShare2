"""End-of-day report generation and delivery via Telegram."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

from alerts.telegram_alert import send_daily_summary, send_text
from config.config import config
from utils.time_utils import today_ist

logger = logging.getLogger("trading.reports")


def generate_daily_report(
    portfolio_value: float,
    day_pnl: float,
    day_pnl_pct: float,
    open_positions: int,
    cash_pct: float,
    regime: str,
    trades_today: int,
) -> str:
    """Generate end-of-day report text and send via Telegram.

    Returns:
        Report text string.
    """
    send_daily_summary(
        portfolio_value=portfolio_value,
        day_pnl=day_pnl,
        day_pnl_pct=day_pnl_pct,
        open_positions=open_positions,
        cash_pct=cash_pct,
        regime=regime,
        trades_today=trades_today,
    )

    sign = "+" if day_pnl >= 0 else ""
    report = (
        f"=== Daily Summary {today_ist()} ===\n"
        f"Portfolio: ₹{portfolio_value:,.0f}\n"
        f"Day P&L: {sign}₹{day_pnl:,.0f} ({sign}{day_pnl_pct:.2f}%)\n"
        f"Open Positions: {open_positions}\n"
        f"Deployable cash: {cash_pct:.1f}% (after 20% reserve)\n"
        f"Regime: {regime}\n"
        f"Trades Today: {trades_today}"
    )
    logger.info("Daily report generated")
    return report


def generate_eod_position_list(portfolio_manager) -> str:
    """Return a formatted list of open positions for the daily report."""
    positions = portfolio_manager.get_open_positions()
    if not positions:
        return "No open positions."

    lines = ["Open Positions:"]
    for pos in positions:
        current = float(pos.get("current_price") or pos["entry_price"])
        entry = float(pos["entry_price"])
        pnl_pct = (current - entry) / entry * 100
        shares = int(pos["shares"])
        sign = "+" if pnl_pct >= 0 else ""
        lines.append(
            f"  {pos['symbol']:12} {shares:4}sh  entry=₹{entry:.0f}  "
            f"now=₹{current:.0f}  {sign}{pnl_pct:.1f}%  SL=₹{float(pos['stop_loss']):.0f}"
        )
    return "\n".join(lines)
