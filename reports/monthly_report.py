"""Monthly performance report with detailed trade breakdown."""

from __future__ import annotations

import logging
from datetime import date
from calendar import monthrange

import pandas as pd
from sqlalchemy import create_engine

from config.config import config
from alerts.telegram_alert import send_text

logger = logging.getLogger("trading.reports")


def generate_monthly_report(portfolio_tracker) -> str:
    """Generate a monthly summary and send via Telegram."""
    today = date.today()
    month_start = today.replace(day=1)

    db_path = f"sqlite:///{config.DB_PATH}/portfolio_state.db"
    engine = create_engine(db_path)

    try:
        trades_df = pd.read_sql(
            f"SELECT * FROM trades WHERE exit_date >= '{month_start}' ORDER BY exit_date",
            engine,
        )
    except Exception:
        trades_df = pd.DataFrame()

    snap = portfolio_tracker.snapshot()
    month_pnl = float(trades_df["realised_pnl"].sum()) if not trades_df.empty else 0.0
    month_trades = len(trades_df)
    month_wins = int((trades_df["realised_pnl"] > 0).sum()) if not trades_df.empty else 0
    win_rate = month_wins / month_trades * 100 if month_trades > 0 else 0.0

    mode = "[PAPER] " if config.PAPER_TRADING_MODE else ""
    report = (
        f"{mode}📊 Monthly Report — {today.strftime('%B %Y')}\n"
        f"Portfolio: ₹{snap['portfolio_value']:,.0f}\n"
        f"Month P&L: {'+' if month_pnl >= 0 else ''}₹{month_pnl:,.0f}\n"
        f"Trades: {month_trades} | Win Rate: {win_rate:.0f}%\n"
        f"Open: {snap['open_positions_count']} positions | Cash: {snap['cash_pct'] * 100:.1f}%\n"
        f"Peak Drawdown: {snap['drawdown_pct'] * 100:.1f}%"
    )

    send_text(report)
    logger.info("Monthly report generated for %s", today.strftime("%B %Y"))
    return report
