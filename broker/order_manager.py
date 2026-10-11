"""Order placement, tracking, and GTT synchronisation.

Order placement hierarchy:
  1. Check margin availability before every order
  2. Place order
  3. Poll for confirmation (every 2 seconds, max 30 seconds)
  4. If rejected: log reason, alert Telegram, do NOT retry
  5. If timeout: log, alert, check order book
  6. Exponential backoff ONLY for network errors (not exchange rejections)

GTT orders for all stop losses — survive bot restarts and internet outages.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from broker.kite_client import KiteClient
from config.config import config

logger = logging.getLogger("orders.manager")

_ORDER_STATUS_POLL_INTERVAL = config.ORDER_STATUS_POLL_SECONDS
_ORDER_STATUS_TIMEOUT = config.ORDER_STATUS_TIMEOUT_SECONDS
_TERMINAL_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED"}


class OrderManager:
    """Manages all order lifecycle events.

    Args:
        kite: An authenticated KiteClient instance.
    """

    def __init__(self, kite: KiteClient) -> None:
        self._kite = kite

    def buy_cnc(
        self,
        symbol: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: float = 0.0,
        tag: str = "",
    ) -> Optional[str]:
        """Place a CNC (delivery) buy order.

        Args:
            symbol:      NSE trading symbol.
            quantity:    Number of shares to buy.
            order_type:  "MARKET" or "LIMIT".
            limit_price: Required if order_type is "LIMIT".
            tag:         Optional order tag for identification.

        Returns:
            order_id string on success, None on failure.
        """
        if not self._check_margin(symbol, quantity, limit_price):
            logger.error("Insufficient margin for %s x%d", symbol, quantity)
            return None

        order_id = self._kite.place_order(
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=quantity,
            order_type=order_type,
            price=limit_price,
            tag=tag or f"bot_entry_{symbol}",
        )

        if order_id is None:
            from alerts.telegram_alert import send_system_error
            send_system_error("OrderManager", f"BUY order rejected for {symbol}")
            return None

        final_status = self._wait_for_fill(order_id)
        logger.info(
            "BUY %s qty=%d status=%s order_id=%s",
            symbol, quantity, final_status, order_id,
        )
        return order_id if final_status == "COMPLETE" else None

    def sell_cnc(
        self,
        symbol: str,
        quantity: int,
        order_type: str = "LIMIT",
        limit_price: float = 0.0,
        tag: str = "",
    ) -> Optional[str]:
        """Place a CNC sell order.

        Attempts limit order first; falls back to market after 2 minutes if unfilled.

        Args:
            symbol:      NSE trading symbol.
            quantity:    Number of shares to sell.
            order_type:  "MARKET" or "LIMIT".
            limit_price: LTP-based limit price (use get_limit_sell_price() helper).
            tag:         Optional order tag.

        Returns:
            order_id string on success, None on failure.
        """
        order_id = self._kite.place_order(
            tradingsymbol=symbol,
            transaction_type="SELL",
            quantity=quantity,
            order_type=order_type,
            price=limit_price,
            tag=tag or f"bot_exit_{symbol}",
        )

        if order_id is None:
            from alerts.telegram_alert import send_system_error
            send_system_error("OrderManager", f"SELL order rejected for {symbol}")
            return None

        final_status = self._wait_for_fill(order_id, timeout=config.ORDER_FILL_WAIT_SECONDS)
        if final_status != "COMPLETE":
            # Upgrade to market order
            logger.warning(
                "Limit sell for %s not filled in 2 min — upgrading to MARKET", symbol
            )
            self._kite.cancel_order(order_id)
            market_id = self._kite.place_order(
                tradingsymbol=symbol,
                transaction_type="SELL",
                quantity=quantity,
                order_type="MARKET",
                tag=tag or f"bot_exit_mkt_{symbol}",
            )
            if market_id is None:
                from alerts.telegram_alert import send_system_error
                send_system_error("OrderManager", f"Market-upgrade SELL rejected for {symbol}")
                return None
            market_status = self._wait_for_fill(market_id)
            if market_status != "COMPLETE":
                from alerts.telegram_alert import send_system_error
                send_system_error(
                    "OrderManager",
                    f"Market-upgrade SELL for {symbol} ended {market_status} — treat exit as failed",
                )
                return None
            return market_id

        return order_id

    def get_fill_price(self, order_id: Optional[str]) -> Optional[float]:
        """Return the broker's average fill price for a completed order.

        Returns None in paper mode (no real fill exists) or when the price is
        unavailable — callers fall back to the intended price.
        """
        if not order_id or config.PAPER_TRADING_MODE:
            return None
        history = self._kite.get_order_history(order_id)
        for entry in reversed(history):
            if entry.get("status") == "COMPLETE":
                avg = float(entry.get("average_price") or 0.0)
                return avg if avg > 0 else None
        return None

    def place_gtt_stop_loss(
        self,
        symbol: str,
        stop_price: float,
        quantity: int,
    ) -> Optional[str]:
        """Place a GTT stop-loss order for an open position.

        Args:
            symbol:      NSE trading symbol.
            stop_price:  Price at which to trigger the market sell.
            quantity:    Total shares to sell at trigger.

        Returns:
            GTT trigger_id string, or None on failure.
        """
        trigger_id = self._kite.place_gtt(
            tradingsymbol=symbol,
            trigger_price=stop_price,
            quantity=quantity,
            transaction_type="SELL",
        )
        if trigger_id:
            logger.info("GTT stop-loss placed: %s @ %.2f id=%s", symbol, stop_price, trigger_id)
        return trigger_id

    def update_gtt_stop_loss(
        self,
        old_trigger_id: str,
        symbol: str,
        new_stop_price: float,
        quantity: int,
    ) -> Optional[str]:
        """Replace an existing GTT stop-loss with a new price.

        The new GTT is placed BEFORE the old one is deleted, so a failed
        placement leaves the existing stop standing instead of leaving the
        position unprotected (2026-10-11 audit M2). If both briefly exist and
        both fire, the second sell is rejected because the shares are gone.

        Args:
            old_trigger_id: Current GTT trigger ID to remove.
            symbol:         NSE trading symbol.
            new_stop_price: New stop price.
            quantity:       Shares in the position.

        Returns:
            New trigger_id, or None on failure (the old GTT is then kept).
        """
        new_id = self.place_gtt_stop_loss(symbol, new_stop_price, quantity)
        if not new_id:
            logger.error(
                "GTT update failed for %s — keeping existing GTT %s", symbol, old_trigger_id,
            )
            from alerts.telegram_alert import send_system_error
            send_system_error(
                "OrderManager",
                f"Could not raise GTT stop for {symbol} to {new_stop_price:.2f}; "
                f"the previous GTT {old_trigger_id} is still active.",
            )
            return None
        self._kite.delete_gtt(old_trigger_id)
        logger.info(
            "GTT updated for %s: old=%s new=%s @ %.2f",
            symbol, old_trigger_id, new_id, new_stop_price,
        )
        return new_id

    def sync_gtt_orders(self, expected: dict[str, str]) -> dict[str, bool]:
        """Verify that all expected GTT stop-losses are active on Kite.

        Args:
            expected: Dict mapping symbol → trigger_id.

        Returns:
            Dict mapping symbol → True if GTT is active, False if missing.
        """
        active_gtts = self._kite.get_gtts()
        active_ids = {str(g["id"]) for g in active_gtts if g.get("status") == "active"}
        result: dict[str, bool] = {}
        for symbol, trigger_id in expected.items():
            result[symbol] = str(trigger_id) in active_ids

        missing = [s for s, ok in result.items() if not ok]
        if missing:
            logger.warning("GTT orders missing for: %s", missing)
            from alerts.telegram_alert import send_gtt_sync_status
            active_count = sum(1 for ok in result.values() if ok)
            send_gtt_sync_status(active_count, len(expected))

        return result

    def get_limit_sell_price(self, symbol: str, offset_pct: float = None) -> float:
        """Return a limit sell price slightly below LTP to avoid slippage.

        Args:
            symbol:     NSE trading symbol.
            offset_pct: Fraction below LTP (default config.PARTIAL_SELL_LIMIT_DISCOUNT).

        Returns:
            Limit price float (rounded to nearest 0.05 for NSE tick size).
        """
        instruments = [f"NSE:{symbol}"]
        ltp_data = self._kite.get_ltp(instruments)
        ltp = ltp_data.get(f"NSE:{symbol}", 0.0)
        if ltp <= 0:
            return 0.0
        if offset_pct is None:
            offset_pct = config.PARTIAL_SELL_LIMIT_DISCOUNT
        limit = ltp * (1 - offset_pct)
        return round(limit / config.PRICE_TICK) * config.PRICE_TICK  # NSE tick

    def _wait_for_fill(self, order_id: str, timeout: int = _ORDER_STATUS_TIMEOUT) -> str:
        """Poll order status until it reaches a terminal state or timeout.

        Args:
            order_id: The order ID to monitor.
            timeout:  Maximum seconds to wait.

        Returns:
            Final status string: "COMPLETE", "REJECTED", "CANCELLED", or "TIMEOUT".
        """
        if config.PAPER_TRADING_MODE:
            return "COMPLETE"

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self._kite.get_order_history(order_id)
            if history:
                latest = history[-1]
                status = latest.get("status", "")
                if status in _TERMINAL_STATUSES:
                    if status == "REJECTED":
                        reason = latest.get("status_message", "Unknown reason")
                        logger.error("Order %s REJECTED: %s", order_id, reason)
                    return status
            time.sleep(_ORDER_STATUS_POLL_INTERVAL)

        logger.warning("Order %s status check timed out after %ds", order_id, timeout)
        return "TIMEOUT"

    def _check_margin(self, symbol: str, quantity: int, price: float) -> bool:
        """Return True if sufficient margin exists for the order.

        Args:
            symbol:   NSE trading symbol (used for LTP lookup if price is 0).
            quantity: Number of shares.
            price:    Estimated price per share (0 = use LTP).
        """
        if config.PAPER_TRADING_MODE:
            return True

        margins = self._kite.get_margins()
        available = margins.get("equity", {}).get("available", {}).get("live_balance", 0)

        if price <= 0:
            ltp_data = self._kite.get_ltp([f"NSE:{symbol}"])
            price = ltp_data.get(f"NSE:{symbol}", 0.0)

        required = quantity * price * config.MARGIN_BUFFER_MULT
        return available >= required
