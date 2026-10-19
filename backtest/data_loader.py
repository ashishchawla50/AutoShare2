"""Load historical OHLCV for backtesting — ISIN-keyed, from the reconstructed dataset.

REPOINTED 2026-08-11. This module previously read `market_data.db`, which holds
only 502 symbols, all of them CURRENT Nifty 500 members. 36% of the real
historical candidate pool (measured on 2022-10-27) was absent, including names as
large as HDFC. It also carried corporate-action artifacts — NMDC showed +85.7% on
2022-10-27 where NSE recorded −20.2%.

The source is now data/reconstructed.db `ohlcv_adjusted`: built from the immutable
NSE bhavcopy archive, back-adjusted only where a corporate action is corroborated
by the NSE events feed, with unresolved discontinuities quarantined.

Identity is ISIN. `load_ohlcv` takes an ISIN. Symbols are time-dependent
attributes and are resolved via PointInTimeUniverse.symbol_at().

To (re)build the dataset:
    python tools/fetch_bhavcopy.py --start 2021-06-01 --end <today>
    python tools/reconstruct_history.py --root data --all
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from config.config import config
from utils.data_utils import ensure_datetime_index

logger = logging.getLogger("backtest.data")

_NIFTY_ISIN_PROXY = "NIFTY50"       # index series, stored by symbol not ISIN


class FabricatedDataSourceError(RuntimeError):
    """Raised when a backtest tries to read the old survivorship-biased dataset."""


def _guard() -> None:
    """Fail loudly rather than silently backtesting on fabricated history."""
    if config.BACKTEST_DATA_SOURCE == "reconstructed":
        return
    if config.BACKTEST_DATA_SOURCE == "legacy_fabricated":
        if config.ALLOW_FABRICATED_BACKTEST_DATA:
            logger.critical(
                "READING THE FABRICATED LEGACY DATASET. Survivorship- and "
                "inclusion-biased; every number produced is void. Explicitly "
                "enabled via ALLOW_FABRICATED_BACKTEST_DATA.")
            return
        raise FabricatedDataSourceError(
            "BACKTEST_DATA_SOURCE='legacy_fabricated' but "
            "ALLOW_FABRICATED_BACKTEST_DATA is not set. market_data.db is "
            "survivorship-biased (36% of the historical pool missing) and its "
            "universe snapshot is backdated. Refusing to produce void results. "
            "See docs/DATA_FOUNDATION_REVIEW.md.")
    raise FabricatedDataSourceError(
        f"Unknown BACKTEST_DATA_SOURCE={config.BACKTEST_DATA_SOURCE!r}. "
        "Expected 'reconstructed' or 'legacy_fabricated'.")


def _db() -> Path:
    return Path(config.DB_PATH) / config.RECONSTRUCTED_DB


def _connect() -> sqlite3.Connection:
    p = _db()
    if not p.exists():
        raise FileNotFoundError(
            f"Reconstructed dataset not found at {p}. Build it with "
            "tools/fetch_bhavcopy.py then tools/reconstruct_history.py --all")
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def load_ohlcv(isin: str, start: date, end: date) -> Optional[pd.DataFrame]:
    """Load corporate-action-adjusted OHLCV for an ISIN.

    Args:
        isin:  Canonical security identity (NOT a ticker).
        start: Start date (inclusive).
        end:   End date (inclusive).

    Returns:
        DataFrame indexed by date with [open, high, low, close, volume], or None.
    """
    _guard()
    con = _connect()
    try:
        df = pd.read_sql(
            "SELECT trade_date AS date, open, high, low, close, volume "
            "FROM ohlcv_adjusted WHERE isin = ? AND trade_date >= ? AND trade_date <= ? "
            "ORDER BY trade_date",
            con, params=(isin, start.isoformat(), end.isoformat()), parse_dates=["date"])
    finally:
        con.close()
    if df.empty:
        return None
    df.set_index("date", inplace=True)
    return ensure_datetime_index(df)


def load_ohlcv_by_symbol(symbol: str, on: date, start: date, end: date) -> Optional[pd.DataFrame]:
    """Convenience wrapper: resolve a ticker to its ISIN *as of a date*, then load.

    Prefer load_ohlcv(). This exists only for call sites that genuinely start from
    a ticker, and it still routes through ISIN so a rename cannot split a history.
    """
    _guard()
    con = _connect()
    try:
        r = con.execute(
            "SELECT isin FROM symbol_history WHERE symbol=? AND from_date<=? AND to_date>=? "
            "ORDER BY from_date LIMIT 1",
            (symbol, on.isoformat(), on.isoformat())).fetchone()
    finally:
        con.close()
    if not r:
        logger.debug("No ISIN for symbol %s on %s", symbol, on)
        return None
    return load_ohlcv(r[0], start, end)


def load_nifty_ohlcv(start: date, end: date) -> Optional[pd.DataFrame]:
    """Nifty 50 index series for regime and benchmark calculations.

    The index is not an ISIN-bearing security, so it is still stored by symbol in
    market_data.db. This is a benchmark series only: it is never a trade
    candidate, so it carries no survivorship or inclusion bias.
    """
    p = Path(config.DB_PATH) / "market_data.db"
    if not p.exists():
        logger.warning("market_data.db missing — Nifty benchmark unavailable")
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        for sym in (_NIFTY_ISIN_PROXY, "NIFTY 50"):
            df = pd.read_sql(
                "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
                "WHERE symbol = ? AND date >= ? AND date <= ? ORDER BY date",
                con, params=(sym, start.isoformat(), end.isoformat()), parse_dates=["date"])
            if not df.empty:
                df.set_index("date", inplace=True)
                return ensure_datetime_index(df)
    finally:
        con.close()
    logger.warning("Nifty OHLCV not cached for %s→%s", start, end)
    return None


def load_india_vix() -> pd.Series:
    """India VIX daily close, date-indexed, from config.INDIA_VIX_CSV.

    Raises FileNotFoundError when the file is absent: a run that asked for the
    VIX tiers must never silently fall back to the VIX-less gate.
    """
    path = Path(config.DB_PATH) / config.INDIA_VIX_CSV
    if not path.exists():
        raise FileNotFoundError(
            f"India VIX history not found at {path}. Fetch it with tools/fetch_india_vix.py "
            "or run without the VIX tiers.")
    df = pd.read_csv(path, parse_dates=["date"])
    return df.set_index("date")["close"].astype(float).sort_index()
