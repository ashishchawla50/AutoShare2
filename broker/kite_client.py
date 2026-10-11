"""Zerodha Kite Connect API wrapper with rate limiting and retry logic.

Wraps the raw kiteconnect library with:
- Token bucket rate limiter (10 orders/sec, 200 historical calls/min)
- Exponential backoff retry for network errors only
- Structured logging of every API interaction
- Paper trading intercept (returns simulated responses)
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Optional

from config.config import config

logger = logging.getLogger("orders.kite")


class TokenBucket:
    """Thread-safe token bucket for rate limiting.

    Args:
        capacity:  Maximum tokens in the bucket.
        refill_hz: Tokens added per second.
    """

    def __init__(self, capacity: int, refill_hz: float) -> None:
        self._capacity = capacity
        self._refill_hz = refill_hz
        self._tokens: float = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: int = 1) -> None:
        """Block until the requested number of tokens are available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
            time.sleep(config.KITE_RATE_LIMIT_POLL_SECONDS)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_hz)
        self._last_refill = now


# Global rate limiters (shared across all KiteClient instances)
_order_limiter = TokenBucket(capacity=config.KITE_ORDER_RATE_LIMIT, refill_hz=config.KITE_ORDER_RATE_LIMIT)
_historical_limiter = TokenBucket(
    capacity=config.KITE_HISTORICAL_RATE_LIMIT,
    refill_hz=config.KITE_HISTORICAL_RATE_LIMIT / 60,  # per-minute → per-second
)


class KiteClient:
    """Thin wrapper around kiteconnect.KiteConnect with rate limiting and retry.

    In paper trading mode, order placement is intercepted and simulated.
    Historical data, quotes, and portfolio reads always call the real API
    even in paper mode (we need real data to make paper decisions).
    """

    def __init__(self) -> None:
        self._kite: Optional[Any] = None  # kiteconnect.KiteConnect
        self._connected = False

    def connect(self, access_token: str) -> None:
        """Initialise the KiteConnect session with an access token.

        Args:
            access_token: Today's Zerodha session token.
        """
        try:
            from kiteconnect import KiteConnect  # type: ignore[import]
            self._kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
            self._kite.set_access_token(access_token)
            profile = self._kite.profile()
            self._connected = True
            logger.info(
                "Kite connected as %s (%s)",
                profile.get("user_name"),
                profile.get("user_id"),
            )
        except ImportError:
            logger.error("kiteconnect package not installed")
            raise
        except Exception as exc:
            logger.error("Kite connect failed: %s", exc)
            raise

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def raw(self):
        """Return the underlying KiteConnect instance for direct access."""
        return self._kite

    # ── Quotes / market data ──────────────────────────────────────────────────

    def get_ltp(self, instruments: list[str]) -> dict[str, float]:
        """Return last traded prices for a list of instruments.

        Args:
            instruments: List of "NSE:SYMBOL" formatted strings.

        Returns:
            Dict mapping instrument string to LTP float.
        """
        _historical_limiter.acquire()
        result: dict[str, float] = {}
        try:
            data = self._kite.ltp(instruments)
            for key, val in data.items():
                result[key] = val["last_price"]
        except Exception as exc:
            logger.error("get_ltp failed: %s", exc)
        return result

    def get_quote(self, instruments: list[str]) -> dict:
        """Return full quote data for instruments."""
        _historical_limiter.acquire()
        try:
            return self._kite.quote(instruments)
        except Exception as exc:
            logger.error("get_quote failed: %s", exc)
            return {}

    def historical_data(
        self,
        instrument_token: int,
        from_date: date,
        to_date: date,
        interval: str = "day",
    ) -> list[dict]:
        """Fetch OHLCV historical data with rate limiting.

        Args:
            instrument_token: Kite instrument token.
            from_date:        Start date.
            to_date:          End date.
            interval:         Kite interval string: "minute", "day", "week", etc.

        Returns:
            List of dicts with keys: date, open, high, low, close, volume.
        """
        _historical_limiter.acquire()
        try:
            return self._kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                continuous=False,
                oi=False,
            )
        except Exception as exc:
            logger.error(
                "historical_data failed for token %d: %s", instrument_token, exc
            )
            return []

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def get_positions(self) -> dict:
        """Return current intraday and overnight positions."""
        try:
            return self._kite.positions()
        except Exception as exc:
            logger.error("get_positions failed: %s", exc)
            return {}

    def get_holdings(self) -> list[dict]:
        """Return current equity holdings (CNC positions)."""
        try:
            return self._kite.holdings()
        except Exception as exc:
            logger.error("get_holdings failed: %s", exc)
            return []

    def get_margins(self) -> dict:
        """Return available margin / funds."""
        try:
            return self._kite.margins()
        except Exception as exc:
            logger.error("get_margins failed: %s", exc)
            return {}

    # ── Instruments ───────────────────────────────────────────────────────────

    def get_instruments(self, exchange: str = "NSE") -> list[dict]:
        """Return the full instrument master for the given exchange."""
        try:
            return self._kite.instruments(exchange=exchange)
        except Exception as exc:
            logger.error("get_instruments failed: %s", exc)
            return []

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        exchange: str = "NSE",
        product: str = "CNC",
        validity: str = "DAY",
        tag: str = "",
    ) -> Optional[str]:
        """Place an order with rate limiting. Returns order_id or None on failure.

        In paper trading mode, returns a fake order ID without calling the API.
        """
        _order_limiter.acquire()

        if config.PAPER_TRADING_MODE:
            fake_id = f"PAPER_{tradingsymbol}_{int(time.time())}"
            logger.info(
                "[PAPER] Order placed: %s %s %d @ %s price=%.2f",
                transaction_type, tradingsymbol, quantity, order_type, price,
            )
            return fake_id

        for attempt in range(1, config.KITE_MAX_ATTEMPTS + 1):
            try:
                kwargs: dict[str, Any] = dict(
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    product=product,
                    order_type=order_type,
                    validity=validity,
                )
                if order_type == "LIMIT" and price > 0:
                    kwargs["price"] = price
                if tag:
                    kwargs["tag"] = tag

                order_id = self._kite.place_order(variety="regular", **kwargs)
                logger.info(
                    "Order placed: %s %s %d order_id=%s",
                    transaction_type, tradingsymbol, quantity, order_id,
                )
                return str(order_id)
            except Exception as exc:
                err_str = str(exc)
                # Don't retry exchange rejections — they won't self-heal
                if any(k in err_str for k in ("InputException", "NetworkException")) and attempt < config.KITE_MAX_ATTEMPTS:
                    delay = config.KITE_RETRY_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Order attempt %d failed (%s) — retrying in %ds", attempt, exc, delay
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        "Order placement failed for %s (attempt %d): %s",
                        tradingsymbol, attempt, exc,
                    )
                    return None
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if config.PAPER_TRADING_MODE:
            logger.info("[PAPER] Cancel order %s", order_id)
            return True
        try:
            self._kite.cancel_order(variety="regular", order_id=order_id)
            return True
        except Exception as exc:
            logger.error("Cancel order %s failed: %s", order_id, exc)
            return False

    def get_order_history(self, order_id: str) -> list[dict]:
        """Return the full status history of an order."""
        if config.PAPER_TRADING_MODE:
            return [{"status": "COMPLETE", "order_id": order_id}]
        try:
            return self._kite.order_history(order_id=order_id)
        except Exception as exc:
            logger.error("get_order_history failed for %s: %s", order_id, exc)
            return []

    def get_orders(self) -> list[dict]:
        """Return all orders for today's session."""
        if config.PAPER_TRADING_MODE:
            return []
        try:
            return self._kite.orders()
        except Exception as exc:
            logger.error("get_orders failed: %s", exc)
            return []

    # ── GTT (Good Till Triggered) ─────────────────────────────────────────────

    def place_gtt(
        self,
        tradingsymbol: str,
        trigger_price: float,
        quantity: int,
        transaction_type: str = "SELL",
        exchange: str = "NSE",
    ) -> Optional[str]:
        """Place a GTT stop-loss order. Returns trigger_id or None.

        GTT orders survive bot restarts and internet outages — critical for safety.
        """
        if config.PAPER_TRADING_MODE:
            fake_id = f"GTT_PAPER_{tradingsymbol}_{int(time.time())}"
            logger.info("[PAPER] GTT placed: %s @ %.2f qty=%d", tradingsymbol, trigger_price, quantity)
            return fake_id

        try:
            trigger_id = self._kite.place_gtt(
                trigger_type=self._kite.GTT_TYPE_SINGLE,
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                trigger_values=[trigger_price],
                last_price=trigger_price,
                orders=[{
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "transaction_type": transaction_type,
                    "quantity": quantity,
                    "order_type": "MARKET",
                    "product": "CNC",
                    "price": trigger_price,
                }],
            )
            logger.info("GTT placed: %s @ %.2f id=%s", tradingsymbol, trigger_price, trigger_id)
            return str(trigger_id)
        except Exception as exc:
            logger.error("GTT placement failed for %s: %s", tradingsymbol, exc)
            return None

    def get_gtts(self) -> list[dict]:
        """Return all active GTT orders."""
        if config.PAPER_TRADING_MODE:
            return []
        try:
            return self._kite.get_gtts()
        except Exception as exc:
            logger.error("get_gtts failed: %s", exc)
            return []

    def delete_gtt(self, trigger_id: str) -> bool:
        """Delete a GTT order. Returns True on success."""
        if config.PAPER_TRADING_MODE:
            return True
        try:
            self._kite.delete_gtt(trigger_id=int(trigger_id))
            return True
        except Exception as exc:
            logger.error("delete_gtt failed for %s: %s", trigger_id, exc)
            return False
