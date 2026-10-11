"""Weekly performance report."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import create_engine

from config.config import config
from alerts.telegram_alert import send_text

logger = logging.getLogger("trading.reports")


def generate_weekly_report(portfolio_tracker) -> str:
    """Generate a weekly summary and send via Telegram."""
    summary = portfolio_tracker.get_performance_summary()
    snap = portfolio_tracker.snapshot()

    week_start = date.today() - timedelta(days=7)
    db_path = f"sqlite:///{config.DB_PATH}/portfolio_state.db"
    engine = create_engine(db_path)

    try:
        trades_df = pd.read_sql(
            f"SELECT * FROM trades WHERE exit_date >= '{week_start}' ORDER BY exit_date",
            engine,
        )
    except Exception:
        trades_df = pd.DataFrame()

    week_pnl = float(trades_df["realised_pnl"].sum()) if not trades_df.empty else 0.0
    week_trades = len(trades_df)
    week_wins = int((trades_df["realised_pnl"] > 0).sum()) if not trades_df.empty else 0

    mode = "[PAPER] " if config.PAPER_TRADING_MODE else ""
    report = (
        f"{mode}📅 Weekly Report\n"
        f"Portfolio: ₹{snap['portfolio_value']:,.0f}\n"
        f"Week P&L: {'+' if week_pnl >= 0 else ''}₹{week_pnl:,.0f}\n"
        f"Trades: {week_trades} ({week_wins} wins)\n"
        f"Open: {snap['open_positions_count']} positions | Cash: {snap['cash_pct'] * 100:.1f}%\n"
        f"Drawdown: {snap['drawdown_pct'] * 100:.1f}%"
    )

    send_text(report)
    return report
