"""Independent GTT backup monitor — the safety net for Zerodha GTT failures.

GTT orders sit on Zerodha's servers, NOT on NSE's exchange. During Zerodha
outages (documented on budget days, election results, high-vol events), GTTs
do not fire. This monitor runs as a separate process and places market sell
orders directly if a stop level is breached — independent of GTT infrastructure.

Flow:
  Every 60 seconds during market hours:
    1. Fetch LTP for all open positions via Kite
    2. Load stop-loss levels from portfolio DB
    3. If LTP <= stop_loss: place market CNC sell order immediately
    4. Alert Telegram with reason: "GTT backup triggered"

This process is intentionally simple — no strategy logic, just price vs stop.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from config.config import config
from utils.time_utils import is_market_open

logger = logging.getLogger("broker.gtt_monitor")


class GTTBackupMonitor:
    """Polls open positions every 60 seconds and fires market sells if stop breached.

    Args:
        kite_client: Connected KiteClient instance.
    """

    POLL_INTERVAL_SECONDS = config.GTT_MONITOR_POLL_SECONDS
    MAX_SELL_ATTEMPTS = config.GTT_MONITOR_MAX_SELL_ATTEMPTS

    def __init__(self, kite_client) -> None:
        self._kite = kite_client
        self._db_path = Path(config.DB_PATH) / "portfolio_state.db"
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._triggered: set[str] = set()  # symbols already handled this session
        self._fail_counts: dict[str, int] = {}  # failed sell attempts per symbol

    def run_once(self) -> None:
        """Single poll cycle: check all open positions against their stops."""
        if not is_market_open():
            return

        positions = self._load_open_positions()
        if not positions:
            return

        symbols = [p["symbol"] for p in positions]
        ltps = self._fetch_ltps(symbols)

        for pos in positions:
            symbol = pos["symbol"]
            stop = pos["stop_loss_price"]
            ltp = ltps.get(symbol)

            if ltp is None:
                logger.warning("GTT monitor: no LTP for %s", symbol)
                continue

            if symbol in self._triggered:
                continue  # already handled this session

            if ltp <= stop:
                logger.critical(
                    "GTT BACKUP TRIGGERED: %s LTP=%.2f <= stop=%.2f",
                    symbol, ltp, stop,
                )
                if self._fire_market_sell(symbol, pos["quantity"]):
                    self._triggered.add(symbol)
                else:
                    # Failed sell must NOT count as handled — retry on the next
                    # poll, but cap attempts so a hard rejection doesn't spam
                    # orders/alerts every 60 seconds forever
                    self._fail_counts[symbol] = self._fail_counts.get(symbol, 0) + 1
                    if self._fail_counts[symbol] >= self.MAX_SELL_ATTEMPTS:
                        logger.critical(
                            "GTT backup sell failed %d times for %s — giving up, MANUAL EXIT REQUIRED",
                            self._fail_counts[symbol], symbol,
                        )
                        from alerts.telegram_alert import send_text
                        send_text(
                            f"🚨 GTT backup sell failed {self._fail_counts[symbol]} times for "
                            f"{symbol} — giving up. SELL MANUALLY IN KITE NOW."
                        )
                        self._triggered.add(symbol)

    def run_loop(self) -> None:
        """Continuous polling loop — call this from a background thread/process."""
        logger.info("GTT backup monitor started (poll every %ds)", self.POLL_INTERVAL_SECONDS)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                logger.error("GTT monitor poll error: %s", exc)
            time.sleep(self.POLL_INTERVAL_SECONDS)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _load_open_positions(self) -> list[dict]:
        """Load open positions with their stop-loss levels from DB."""
        try:
            # Remaining shares only (partial profit bookings shrink the
            # position), and only rows for the current mode — a stale paper
            # row must never fire a real market sell in live mode
            df = pd.read_sql(
                "SELECT symbol, "
                "shares - COALESCE(partial_sold_shares, 0) AS quantity, "
                "stop_loss AS stop_loss_price "
                "FROM open_positions WHERE is_paper = :paper",
                self._engine,
                params={"paper": 1 if config.PAPER_TRADING_MODE else 0},
            )
            df = df[df["quantity"] > 0]
            return df.to_dict("records")
        except Exception as exc:
            logger.error("GTT monitor: failed to load positions: %s", exc)
            return []

    def _fetch_ltps(self, symbols: list[str]) -> dict[str, float]:
        """Fetch last traded prices for a list of NSE symbols."""
        if not symbols or not self._kite.is_connected:
            return {}
        try:
            exchange_symbols = [f"NSE:{s}" for s in symbols]
            quotes = self._kite.raw.quote(exchange_symbols)
            return {
                s: quotes[f"NSE:{s}"]["last_price"]
                for s in symbols
                if f"NSE:{s}" in quotes
            }
        except Exception as exc:
            logger.error("GTT monitor: LTP fetch failed: %s", exc)
            return {}

    def _fire_market_sell(self, symbol: str, quantity: int) -> bool:
        """Place an immediate market CNC sell order and send Telegram alert.

        Returns True if the order was placed (or paper mode), False on failure
        so the caller can retry instead of marking the symbol handled.
        """
        if config.PAPER_TRADING_MODE:
            logger.info("PAPER: GTT backup would sell %d × %s at market", quantity, symbol)
            return True

        from alerts.telegram_alert import send_text
        try:
            order_id = self._kite.raw.place_order(
                tradingsymbol=symbol,
                exchange="NSE",
                transaction_type="SELL",
                quantity=quantity,
                order_type="MARKET",
                product="CNC",
                variety="regular",
            )
            logger.critical(
                "GTT backup market sell placed: %s qty=%d order_id=%s",
                symbol, quantity, order_id,
            )
        except Exception as exc:
            logger.critical("GTT backup sell FAILED for %s: %s — will retry", symbol, exc)
            send_text(
                f"🚨 GTT BACKUP: market sell FAILED for {symbol} x{quantity} ({exc}). "
                f"Retrying next poll. Check positions immediately."
            )
            return False

        send_text(
            f"GTT BACKUP FIRED: {symbol} x{quantity} — market sell placed. "
            f"Zerodha GTT may have failed. Check positions immediately."
        )
        return True
