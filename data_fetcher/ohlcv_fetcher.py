"""Fetch and cache OHLCV data from free sources (Yahoo Finance) or Zerodha Kite Connect.

Primary source: yfinance (Free) if Kite is absent.
All data is persisted in market_data.db to avoid redundant API calls.
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
_REQUEST_GAP = 60 / config.KITE_HISTORICAL_RATE_LIMIT

class OHLCVFetcher:
    """Manages OHLCV data fetching, caching, and validation."""

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
        """Return OHLCV DataFrame for a symbol, using cache or free sources when available."""
        cached = self._load_from_cache(symbol)

        if not force_refresh and cached is not None:
            last_date = cached.index[-1].date()
            first_date = cached.index[0].date()
            today = date.today()
            days_missing = (today - last_date).days - 1
            expected_start = today - timedelta(days=days)
            if days_missing <= 0 and first_date <= expected_start + timedelta(days=30):
                return cached

        # Try fetching from active Kite API if available; otherwise use Free yfinance path
        if self._kite is not None:
            fresh = self._fetch_from_kite(symbol, instrument_token, days)
        else:
            fresh = self._fetch_from_yfinance(symbol, days)

        if fresh is not None:
            self._save_to_cache(symbol, fresh)
            issues = flag_data_anomalies(symbol, fresh)
            if issues:
                try:
                    from alerts.telegram_alert import send_data_anomaly
                    send_data_anomaly(symbol, "; ".join(issues))
                except ImportError:
                    pass
            return fresh

        if cached is not None:
            logger.warning("Using cached data for %s (fetch failed)", symbol)
            return cached

        return None

    def _fetch_from_yfinance(self, symbol: str, days: int) -> Optional[pd.DataFrame]:
        """Fetch historical data for free using Yahoo Finance API wrapper."""
        import yfinance as yf
        
        # Convert NSE standard tracker to Yahoo formatting (e.g. GOLDBEES -> GOLDBEES.NS)
        yf_symbol = symbol if symbol.endswith((".NS", ".BO")) else f"{symbol}.NS"
        
        end_date = date.today()
        start_date = end_date - timedelta(days=days)
        
        logger.info("Fetching free data from yfinance for %s (%s to %s)", symbol, start_date, end_date)
        try:
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(start=start_date, end=end_date, interval="1d")
            if df.empty:
                # Fallback for index tokens like NIFTY 50
                if symbol in ("NIFTY50", "NIFTY 50"):
                    df = yf.Ticker("^NSEI").history(start=start_date, end=end_date, interval="1d")
            
            if df.empty:
                logger.warning("No data found on yfinance for ticker %s", yf_symbol)
                return None
                
            df.reset_index(inplace=True)
            df.rename(
                columns={"Date": "date", "Open": "open", "High": "high",
                         "Low": "low", "Close": "close", "Volume": "volume"},
                inplace=True,
            )
            df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
            df.set_index("date", inplace=True)
            df = ensure_datetime_index(df)
            
            # 🔥 THE CRITICAL FIX: Drop empty/corrupt holiday or non-trading NaN rows before caching
            df.dropna(subset=["open", "high", "low", "close"], inplace=True)
            
            if df.empty:
                logger.warning("All fetched rows for %s contained NaN prices.", symbol)
                return None

            df = df[["open", "high", "low", "close", "volume"]].astype(
                {"open": float, "high": float, "low": float, "close": float, "volume": int}
            )
            return df
        except Exception as exc:
            logger.error("yfinance download extraction failed for %s: %s", symbol, exc)
            return None

    def _fetch_from_kite(
        self,
        symbol: str,
        instrument_token: int,
        days: int,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV directly from Kite Connect historical API."""
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
        source_name = "yfinance" if self._kite is None else "kite"
        rows["source"] = source_name
        rows["validated"] = True
        rows["updated_at"] = datetime.now()

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
                        "src": source_name,
                        "val": 1,
                        "upd": datetime.now(),
                    },
                )

    def get_bulk_ohlcv(
        self,
        symbol_token_map: dict[str, int],
        days: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV for multiple symbols with rate limiting."""
        result: dict[str, pd.DataFrame] = {}
        total = len(symbol_token_map)
        for i, (symbol, token) in enumerate(symbol_token_map.items()):
            df = self.get_ohlcv(symbol, token, days=days)
            if df is not None:
                result[symbol] = df
            if (i + 1) % 20 == 0:
                logger.info("OHLCV progress: %d/%d symbols fetched", i + 1, total)
        return result
