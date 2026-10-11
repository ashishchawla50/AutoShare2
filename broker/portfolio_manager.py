"""Real-time portfolio state: holdings, positions, cash, and P&L.

Reconciles local database state with Kite's live holdings to detect
manual trades, broker errors, or GTT-triggered stop-losses between sessions.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from broker.kite_client import KiteClient
from config.config import config

logger = logging.getLogger("trading.portfolio")


class PortfolioManager:
    """Tracks portfolio state: cash, positions, P&L, and drawdown.

    Args:
        kite:             Authenticated KiteClient (None in paper mode).
        starting_capital: Initial portfolio value for paper mode.
    """

    def __init__(
        self,
        kite: Optional[KiteClient] = None,
        starting_capital: float = None,
    ) -> None:
        self._kite = kite
        self._db_path = Path(config.DB_PATH) / "portfolio_state.db"
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._capital = starting_capital or config.STARTING_CAPITAL

    # ── Cash & capital ────────────────────────────────────────────────────────

    def get_available_cash(self) -> float:
        """Return cash available for new trades (after minimum reserve).

        In live mode, queries Kite margins.
        In paper mode, calculates from DB positions.
        """
        if self._kite and not config.PAPER_TRADING_MODE:
            margins = self._kite.get_margins()
            broker_cash = float(margins.get("equity", {}).get("available", {}).get("live_balance", 0))
            # The Zerodha account also holds the owner's own cash, so the
            # broker balance alone let the bot spend beyond its capital and
            # ignore the reserve (2026-10-11 audit H1). Cap it at the bot's own
            # cash: starting capital + realised P&L - cost of open positions,
            # less the reserve on trading equity.
            bot_cash = self._capital + self._get_total_realized_pnl() - self._get_invested_value()
            reserved = self.get_trading_equity() * config.MIN_CASH_RESERVE_PCT
            return max(0.0, min(broker_cash, bot_cash - reserved))

        invested = self._get_invested_value()
        portfolio_value = self._capital  # simplified for paper mode
        reserved = portfolio_value * config.MIN_CASH_RESERVE_PCT
        available = portfolio_value - invested - reserved
        return max(0.0, available)

    def get_portfolio_value(self) -> float:
        """Return the BOT's portfolio value — the basis for all sizing and risk.

        This is trading equity (starting capital + bot P&L), NOT the Zerodha
        account value: the account also holds the owner's personal
        (blocklisted) investments, and sizing off the combined value made the
        bot compute ~10x oversized positions (2026-07-31: SONACOMS sized at
        ₹49.9k against ₹50k bot capital because the account totalled ~₹5L).
        Use get_account_value() for the account-wide view.
        """
        return self.get_trading_equity()

    def get_account_value(self) -> float:
        """Return the full Zerodha account value (cash + ALL holdings).

        Includes the owner's personal holdings — never use this for position
        sizing, sector caps, or drawdown math. Display/observability only.
        """
        if self._kite and not config.PAPER_TRADING_MODE:
            holdings = self._kite.get_holdings()
            holdings_value = sum(
                h["quantity"] * h["last_price"]
                for h in holdings
                if h["quantity"] > 0
            )
            margins = self._kite.get_margins()
            cash = float(margins.get("equity", {}).get("available", {}).get("live_balance", 0))
            return holdings_value + cash

        return self._capital

    # ── Positions ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """Return open positions for the current trading mode (paper vs live)."""
        try:
            paper_flag = 1 if config.PAPER_TRADING_MODE else 0
            df = pd.read_sql(
                "SELECT * FROM open_positions WHERE is_paper = :paper",
                self._engine,
                params={"paper": paper_flag},
            )
            return df.to_dict("records")
        except Exception as exc:
            logger.error("Failed to load open positions: %s", exc)
            return []

    def get_open_position(self, symbol: str) -> Optional[dict]:
        """Return the open position for a specific symbol, or None."""
        positions = self.get_open_positions()
        for pos in positions:
            if pos["symbol"].upper() == symbol.upper():
                return pos
        return None

    def open_position(
        self,
        symbol: str,
        entry_price: float,
        shares: int,
        stop_loss: float,
        gtt_order_id: Optional[str] = None,
        rs_score: float = 0.0,
        vcp_tightness: float = 0.0,
        regime: str = "",
        pivot_price: float = 0.0,
        strategy: str = "vcp",
        target_price: float = 0.0,
    ) -> None:
        """Record a new position opening in the database."""
        now = datetime.now()
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO open_positions "
                    "(symbol, entry_date, entry_price, shares, current_price, "
                    "stop_loss, trailing_stop, partial_sold_shares, realised_partial_pnl, "
                    "gtt_order_id, last_updated, is_paper, strategy, target_price) "
                    "VALUES (:sym, :edate, :eprice, :shares, :cprice, "
                    ":sl, :tsl, 0, 0.0, :gtt, :upd, :paper, :strategy, :target)"
                ),
                {
                    "sym": symbol.upper(),
                    "edate": date.today(),
                    "eprice": entry_price,
                    "shares": shares,
                    "cprice": entry_price,
                    "sl": stop_loss,
                    "tsl": stop_loss,
                    "gtt": gtt_order_id,
                    "upd": now,
                    "paper": config.PAPER_TRADING_MODE,
                    "strategy": strategy,
                    "target": target_price,
                },
            )
        logger.info(
            "Position opened: %s %d @ %.2f SL=%.2f strategy=%s",
            symbol, shares, entry_price, stop_loss, strategy,
        )

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
        shares_to_close: Optional[int] = None,
    ) -> Optional[dict]:
        """Record a position closure and move it to trade history.

        Args:
            symbol:          NSE trading symbol.
            exit_price:      Price at which the position was closed.
            exit_reason:     Reason code: stop_loss, time_stop, profit_target, regime, manual.
            shares_to_close: Partial close if specified; full close if None.

        Returns:
            Dict with trade details including P&L, or None if position not found.
        """
        pos = self.get_open_position(symbol)
        if not pos:
            logger.warning("close_position: no open position found for %s", symbol)
            return None

        total_shares = int(pos["shares"])
        partial_sold = int(pos.get("partial_sold_shares", 0))
        remaining_shares = total_shares - partial_sold
        closing_shares = shares_to_close or remaining_shares

        entry_price = float(pos["entry_price"])
        entry_date = pos["entry_date"]

        pnl_per_share = exit_price - entry_price
        gross_pnl = pnl_per_share * closing_shares
        pnl_pct = (exit_price - entry_price) / entry_price

        holding_days = (date.today() - (
            entry_date if isinstance(entry_date, date) else datetime.fromisoformat(str(entry_date)).date()
        )).days

        trade_record = {
            "symbol": symbol.upper(),
            "entry_date": entry_date,
            "entry_price": entry_price,
            "shares": closing_shares,
            "exit_date": date.today(),
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "realised_pnl": gross_pnl,
            "realised_pnl_pct": pnl_pct * 100,
            "holding_days": holding_days,
            "is_paper": config.PAPER_TRADING_MODE,
            "strategy": pos.get("strategy", "vcp"),
        }

        now = datetime.now()
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO trades "
                    "(symbol, entry_date, entry_price, shares, stop_loss, initial_stop_loss, "
                    "exit_date, exit_price, exit_reason, realised_pnl, realised_pnl_pct, is_paper, strategy) "
                    "VALUES (:sym, :edate, :eprice, :shares, :sl, :isl, "
                    ":xdate, :xprice, :xreason, :pnl, :pnlpct, :paper, :strategy)"
                ),
                {
                    "sym": symbol.upper(),
                    "edate": entry_date,
                    "eprice": entry_price,
                    "shares": closing_shares,
                    "sl": float(pos["stop_loss"]),
                    "isl": float(pos["stop_loss"]),
                    "xdate": date.today(),
                    "xprice": exit_price,
                    "xreason": exit_reason,
                    "pnl": gross_pnl,
                    "pnlpct": pnl_pct * 100,
                    "paper": config.PAPER_TRADING_MODE,
                    "strategy": pos.get("strategy", "vcp"),
                },
            )

            if closing_shares >= remaining_shares:
                # Full close
                conn.execute(
                    text("DELETE FROM open_positions WHERE symbol=:sym"),
                    {"sym": symbol.upper()},
                )
            else:
                # Partial close — update remaining shares
                conn.execute(
                    text(
                        "UPDATE open_positions SET partial_sold_shares=:ps, "
                        "realised_partial_pnl=realised_partial_pnl+:pnl, last_updated=:now "
                        "WHERE symbol=:sym"
                    ),
                    {
                        "ps": partial_sold + closing_shares,
                        "pnl": gross_pnl,
                        "now": now,
                        "sym": symbol.upper(),
                    },
                )

        logger.info(
            "Position closed: %s %d @ %.2f PnL=%.2f (%.2f%%) reason=%s",
            symbol, closing_shares, exit_price, gross_pnl, pnl_pct * 100, exit_reason,
        )
        return trade_record

    def get_recently_closed_trades(self, days: int = None) -> list[dict]:
        """Return trades (current mode) whose exit_date is within the last `days` days."""
        try:
            paper_flag = 1 if config.PAPER_TRADING_MODE else 0
            days = config.RECENT_TRADES_DEFAULT_DAYS if days is None else days
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            df = pd.read_sql(
                "SELECT symbol, shares, exit_date, exit_reason FROM trades "
                "WHERE is_paper = :paper AND exit_date >= :cutoff",
                self._engine,
                params={"paper": paper_flag, "cutoff": cutoff},
            )
            return df.to_dict("records")
        except Exception as exc:
            logger.error("Failed to load recent trades: %s", exc)
            return []

    def update_stop_loss(self, symbol: str, new_stop: float, new_gtt_id: Optional[str] = None) -> None:
        """Update the stop loss for an open position."""
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE open_positions SET stop_loss=:sl, trailing_stop=:tsl, "
                    "gtt_order_id=COALESCE(:gtt, gtt_order_id), last_updated=:now "
                    "WHERE symbol=:sym"
                ),
                {
                    "sl": new_stop,
                    "tsl": new_stop,
                    "gtt": new_gtt_id,
                    "now": datetime.now(),
                    "sym": symbol.upper(),
                },
            )

    def update_current_prices(self, price_map: dict[str, float]) -> None:
        """Batch-update current prices for all open positions."""
        for symbol, price in price_map.items():
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE open_positions SET current_price=:price, last_updated=:now "
                        "WHERE symbol=:sym"
                    ),
                    {"price": price, "now": datetime.now(), "sym": symbol.upper()},
                )

    # ── Sector exposure ───────────────────────────────────────────────────────

    def get_sector_exposure(self, sector_map: dict[str, str]) -> dict[str, float]:
        """Return fraction of portfolio invested per sector.

        Args:
            sector_map: Dict mapping symbol → sector string.

        Returns:
            Dict mapping sector → fraction of portfolio value.
        """
        positions = self.get_open_positions()
        portfolio_value = self.get_portfolio_value()
        if portfolio_value <= 0:
            return {}

        exposure: dict[str, float] = {}
        for pos in positions:
            sym = pos["symbol"]
            sector = sector_map.get(sym, "Unknown")
            position_value = float(pos["current_price"] or pos["entry_price"]) * int(pos["shares"])
            exposure[sector] = exposure.get(sector, 0.0) + position_value / portfolio_value

        return exposure

    # ── Drawdown tracking (P&L-based, immune to Zerodha fund sweeps) ─────────
    #
    # Zerodha transfers unused funds to your bank account (monthly if inactive
    # 30 days, quarterly otherwise — SEBI mandate). If we tracked drawdown via
    # account balance, a fund sweep would look like a 100% loss and trigger
    # emergency halt even when no trading loss occurred.
    #
    # Instead: track an internal equity curve = starting_capital + all_trading_pnl.
    # Fund sweeps are invisible to this calculation because they don't produce
    # a trade record. Only actual trade wins/losses move the equity curve.

    def get_trading_equity(self) -> float:
        """Return current trading equity: starting capital + all realised P&L + unrealised P&L.

        Immune to Zerodha fund transfers — only trade outcomes affect this value.
        """
        realized = self._get_total_realized_pnl()
        unrealized = self._get_total_unrealized_pnl()
        return self._capital + realized + unrealized

    def get_peak_equity(self) -> float:
        """Return the all-time peak trading equity from the metrics log."""
        try:
            result = pd.read_sql(
                "SELECT MAX(peak_value) as peak FROM portfolio_metrics", self._engine
            )
            peak = result["peak"].iloc[0] if not result.empty else None
            return float(peak) if peak and peak > 0 else self._capital
        except Exception:
            return self._capital

    def get_peak_value(self) -> float:
        """Alias for get_peak_equity — kept for API compatibility."""
        return self.get_peak_equity()

    def update_peak_value(self, current_value: float = None) -> float:
        """Update and return the new peak equity."""
        equity = current_value or self.get_trading_equity()
        current_peak = self.get_peak_equity()
        return max(current_peak, equity)

    def get_drawdown_pct(self) -> float:
        """Return current drawdown from peak as a fraction (0.05 = 5%).

        Calculated from the P&L-based equity curve, not Kite account balance.
        Immune to Zerodha's regulatory fund transfers.
        """
        peak = self.get_peak_equity()
        current = self.get_trading_equity()
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - current) / peak)

    def get_last_trade_date(self) -> Optional[date]:
        """Return the date of the most recent closed trade, or None if no trades."""
        try:
            df = pd.read_sql(
                "SELECT MAX(exit_date) as last_trade FROM trades", self._engine
            )
            val = df["last_trade"].iloc[0] if not df.empty else None
            if val is None:
                return None
            if isinstance(val, date):
                return val
            return date.fromisoformat(str(val)[:10])
        except Exception:
            return None

    def get_days_since_last_trade(self) -> int:
        """Return calendar days since the last closed trade (999 if no trades ever)."""
        last = self.get_last_trade_date()
        if last is None:
            return 999
        return (date.today() - last).days

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_invested_value(self) -> float:
        """Return total capital invested in open positions (at entry price)."""
        positions = self.get_open_positions()
        return sum(
            float(p["entry_price"]) * int(p["shares"])
            for p in positions
        )

    def _get_total_realized_pnl(self) -> float:
        """Return sum of realised P&L from closed trades in the current mode.

        Paper and live trades share the table; mixing them would let paper
        P&L inflate live trading equity.
        """
        try:
            paper_flag = 1 if config.PAPER_TRADING_MODE else 0
            df = pd.read_sql(
                "SELECT SUM(realised_pnl) as total FROM trades WHERE is_paper = :paper",
                self._engine,
                params={"paper": paper_flag},
            )
            return float(df["total"].iloc[0] or 0)
        except Exception:
            return 0.0

    def _get_total_unrealized_pnl(self) -> float:
        """Return sum of unrealised P&L across all open positions."""
        positions = self.get_open_positions()
        total = 0.0
        for p in positions:
            shares = int(p["shares"]) - int(p.get("partial_sold_shares", 0))
            current = float(p.get("current_price") or p["entry_price"])
            entry = float(p["entry_price"])
            total += (current - entry) * shares
        return total

    def get_consecutive_losses(self) -> int:
        """Return the current consecutive loss streak from trade history."""
        try:
            df = pd.read_sql(
                "SELECT realised_pnl FROM trades ORDER BY exit_date DESC, id DESC LIMIT 10",
                self._engine,
            )
            streak = 0
            for pnl in df["realised_pnl"]:
                if float(pnl) < 0:
                    streak += 1
                else:
                    break
            return streak
        except Exception:
            return 0
