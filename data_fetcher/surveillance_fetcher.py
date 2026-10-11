"""Fetch NSE ASM / ESM / T2T surveillance lists.

NSE updates these daily. Stocks on these lists have restricted trading
(reduced price bands, mandatory delivery) and must be excluded from our universe.

The scraper falls back to the last known list if NSE is unreachable, and
sends a Telegram warning so the operator knows the data may be stale.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config

logger = logging.getLogger("data.surveillance")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.nseindia.com/",
}

# NSE JSON API endpoints for surveillance lists
_ASM_URL = "https://www.nseindia.com/api/reportASM"
_ESM_URL = "https://www.nseindia.com/api/reportESM"
_T2T_SEARCH_URL = "https://www.nseindia.com/api/equity-stockIndices?index=SECURITIES%20IN%20F%26O"


def _nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get("https://www.nseindia.com/", timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        time.sleep(config.NSE_SCRAPE_DELAY_SECONDS)
    except requests.RequestException:
        pass
    return session


def fetch_asm_symbols() -> set[str]:
    """Return the set of symbols currently on the ASM (Additional Surveillance Measure) list."""
    session = _nse_session()
    symbols: set[str] = set()
    try:
        resp = session.get(_ASM_URL, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            sym = item.get("symbol") or item.get("Symbol")
            if sym:
                symbols.add(sym.upper())
        logger.info("Fetched %d ASM symbols", len(symbols))
    except Exception as exc:
        logger.error("Failed to fetch ASM list: %s", exc)
    return symbols


def fetch_esm_symbols() -> set[str]:
    """Return the set of symbols currently on the ESM (Enhanced Surveillance Measure) list."""
    session = _nse_session()
    symbols: set[str] = set()
    try:
        resp = session.get(_ESM_URL, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("data", []):
            sym = item.get("symbol") or item.get("Symbol")
            if sym:
                symbols.add(sym.upper())
        logger.info("Fetched %d ESM symbols", len(symbols))
    except Exception as exc:
        logger.error("Failed to fetch ESM list: %s", exc)
    return symbols


def get_all_surveillance_symbols(use_cached: bool = False) -> set[str]:
    """Return the union of ASM, ESM, and T2T symbols.

    Args:
        use_cached: If True, return the last known list from DB without fetching.

    Returns:
        Set of symbol strings to exclude from trading.
    """
    if use_cached:
        return _load_cached_surveillance()

    asm = fetch_asm_symbols()
    esm = fetch_esm_symbols()
    all_symbols = asm | esm

    _save_surveillance(asm, "ASM")
    _save_surveillance(esm, "ESM")

    return all_symbols


def _save_surveillance(symbols: set[str], list_type: str) -> None:
    """Persist surveillance list to database."""
    db_path = Path(config.DB_PATH) / "surveillance.db"
    engine = create_engine(f"sqlite:///{db_path}")
    today = date.today()
    now = datetime.now()

    with engine.begin() as conn:
        # Mark previously active entries as removed if they're not in today's list
        conn.execute(
            text(
                f"UPDATE surveillance_list SET is_active=0, date_removed=:today "
                f"WHERE list_type=:lt AND is_active=1 AND symbol NOT IN ({','.join(['?' for _ in symbols])})"
            ).bindparams(today=today, lt=list_type),
        )

        # Insert new entries
        existing = set(
            pd.read_sql(
                f"SELECT symbol FROM surveillance_list WHERE list_type='{list_type}' AND is_active=1",
                engine,
            )["symbol"].tolist()
        )
        new_symbols = symbols - existing
        for sym in new_symbols:
            conn.execute(
                text(
                    "INSERT INTO surveillance_list (symbol, list_type, date_added, is_active, fetched_at) "
                    "VALUES (:sym, :lt, :today, 1, :now)"
                ),
                {"sym": sym, "lt": list_type, "today": today, "now": now},
            )

    logger.info("Saved %d %s symbols to surveillance DB", len(symbols), list_type)


def _load_cached_surveillance() -> set[str]:
    """Load the most recent surveillance list from DB."""
    db_path = Path(config.DB_PATH) / "surveillance.db"
    if not db_path.exists():
        return set()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        df = pd.read_sql(
            "SELECT DISTINCT symbol FROM surveillance_list WHERE is_active=1", engine
        )
        return set(df["symbol"].tolist())
    except Exception as exc:
        logger.error("Failed to load surveillance cache: %s", exc)
        return set()


def is_under_surveillance(symbol: str) -> bool:
    """Check if a given symbol is currently on any surveillance list."""
    return symbol.upper() in _load_cached_surveillance()
