"""Fetch and cache OHLCV data from Zerodha Kite Connect.

Primary source: Kite Connect historical API.
Cross-validation: nsepy for suspicious candles.
All data is persisted in market_data.db to avoid redundant API calls.
Rate limited to Kite's 200 historical calls/minute limit.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config
from utils.data_utils import ensure_datetime_index, flag_data_anomalies
from utils.indicator_utils import normalize_ohlcv_columns

logger = logging.getLogger("data.ohlcv")

_KITE_INTERVAL_DAY = "day"
_REQUEST_GAP = 60 / config.KITE_HISTORICAL_RATE_LIMIT  # seconds per request


class OHLCVFetcher:
    """Manages OHLCV data fetching, caching, and validation.

    Args:
        kite: An authenticated KiteConnect instance (or None for paper mode).
    """

    def __init__(self, kite=None) -> None:
        self._kite = kite
        self._db_path = Path(config.DB_PATH) / "market_data.db"
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._last_request_time: float = 0.0

    def _throttle(self) -> None:
        """Enforce the API rate limit between requests."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < _REQUEST_GAP:
            time.sleep(_REQUEST_GAP - elapsed)
        self._last_request_time = time.monotonic()

    def get_ohlcv(
        self,
        symbol: str,
        instrument_token: int,
        days: int = 500,
        force_refresh: bool = False,
    ) -> Optional[pd.DataFrame]:
        """Return OHLCV DataFrame for a symbol, using cache when available.

        Args:
            symbol:           NSE trading symbol.
            instrument_token: Kite instrument token for the symbol.
            days:             Number of calendar days of history to fetch.
            force_refresh:    Bypass cache and fetch fresh data.

        Returns:
            DataFrame with DatetimeIndex and columns [open, high, low, close, volume],
            or None if data cannot be fetched.
        """
        cached = self._load_from_cache(symbol)

        if not force_refresh and cached is not None:
            last_date = cached.index[-1].date()
            first_date = cached.index[0].date()
            today = date.today()
            days_missing = (today - last_date).days - 1
            expected_start = today - timedelta(days=days)
            # Cache is valid only if: data is current AND covers far enough back.
            # Without the second check, a 730-day cache blocks a 1500-day backfill
            # because days_missing==0 triggers an early return before older data is fetched.
            if days_missing <= 0 and first_date <= expected_start + timedelta(days=30):
                return cached

        fresh = self._fetch_from_kite(symbol, instrument_token, days)
        if fresh is not None:
            self._save_to_cache(symbol, fresh)
            issues = flag_data_anomalies(symbol, fresh)
            if issues:
                from alerts.telegram_alert import send_data_anomaly
                send_data_anomaly(symbol, "; ".join(issues))
            return fresh

        if cached is not None:
            logger.warning("Using cached data for %s (fetch failed)", symbol)
            return cached

        return None

    def _fetch_from_kite(
        self,
        symbol: str,
        instrument_token: int,
        days: int,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV directly from Kite Connect historical API."""
        if self._kite is None:
            logger.debug("Kite not connected — cannot fetch %s", symbol)
            return None

        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        self._throttle()
        try:
            records = self._kite.historical_data(
                instrument_token=instrument_token,
                from_date=start_date,
                to_date=end_date,
                interval=_KITE_INTERVAL_DAY,
                continuous=False,
                oi=False,
            )
        except Exception as exc:
            logger.error("Kite historical_data failed for %s: %s", symbol, exc)
            return None

        if not records:
            logger.warning("Empty data returned from Kite for %s", symbol)
            return None

        df = pd.DataFrame(records)
        df.rename(
            columns={"date": "date", "open": "open", "high": "high",
                     "low": "low", "close": "close", "volume": "volume"},
            inplace=True,
        )
        df.set_index("date", inplace=True)
        df = ensure_datetime_index(df)
        df = df[["open", "high", "low", "close", "volume"]].astype(
            {"open": float, "high": float, "low": float, "close": float, "volume": int}
        )
        logger.info("Fetched %d rows from Kite for %s", len(df), symbol)
        return df

    def _load_from_cache(self, symbol: str) -> Optional[pd.DataFrame]:
        """Load OHLCV data from the local SQLite cache."""
        if not self._db_path.exists():
            return None
        try:
            with self._engine.connect() as conn:
                df = pd.read_sql(
                    text(
                        "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
                        "WHERE symbol=:sym ORDER BY date"
                    ),
                    conn,
                    params={"sym": symbol.upper()},
                    parse_dates=["date"],
                )
            if df.empty:
                return None
            df.set_index("date", inplace=True)
            df = ensure_datetime_index(df)
            return df
        except Exception as exc:
            logger.error("Cache load failed for %s: %s", symbol, exc)
            return None

    def _save_to_cache(self, symbol: str, df: pd.DataFrame) -> None:
        """Upsert OHLCV rows into the SQLite cache."""
        rows = df.reset_index()
        rows["symbol"] = symbol.upper()
        rows["source"] = "kite"
        rows["validated"] = True
        rows["updated_at"] = datetime.now()
        rows.rename(columns={"date": "date"}, inplace=True)

        with self._engine.begin() as conn:
            for _, row in rows.iterrows():
                conn.execute(
                    text(
                        "INSERT OR REPLACE INTO ohlcv_daily "
                        "(symbol, date, open, high, low, close, volume, source, validated, updated_at) "
                        "VALUES (:sym, :dt, :o, :h, :l, :c, :v, :src, :val, :upd)"
                    ),
                    {
                        "sym": symbol.upper(),
                        "dt": row["date"].date() if hasattr(row["date"], "date") else row["date"],
                        "o": float(row["open"]),
                        "h": float(row["high"]),
                        "l": float(row["low"]),
                        "c": float(row["close"]),
                        "v": int(row["volume"]),
                        "src": "kite",
                        "val": 1,
                        "upd": datetime.now(),
                    },
                )

    def get_bulk_ohlcv(
        self,
        symbol_token_map: dict[str, int],
        days: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV for multiple symbols with rate limiting.

        Args:
            symbol_token_map: Dict mapping symbol → instrument_token.
            days:             Days of history per symbol.

        Returns:
            Dict mapping symbol → OHLCV DataFrame (missing symbols omitted).
        """
        result: dict[str, pd.DataFrame] = {}
        total = len(symbol_token_map)
        for i, (symbol, token) in enumerate(symbol_token_map.items()):
            df = self.get_ohlcv(symbol, token, days=days)
            if df is not None:
                result[symbol] = df
            if (i + 1) % 20 == 0:
                logger.info("OHLCV progress: %d/%d symbols fetched", i + 1, total)
        logger.info("Bulk OHLCV complete: %d/%d symbols available", len(result), total)
        return result
