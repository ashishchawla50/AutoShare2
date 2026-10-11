"""Fetch and maintain the Nifty 200 stock universe.

NSE publishes the current index composition on their website.
We scrape it daily and store it in the fundamentals DB.
The composition changes monthly; we store history for survivorship-bias correction.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from sqlalchemy import Column, Date, Integer, MetaData, String, Table, text, create_engine

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config

logger = logging.getLogger("data.universe")

_NSE_NIFTY200_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20200"
_NSE_NIFTY200_CSV = "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv"
_NSE_NIFTY500_URL = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500"
_NSE_NIFTY500_CSV = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Comprehensive Nifty 200 fallback — used when NSE is unreachable from VPS.
# Covers Nifty 50 + Nifty Next 50 + key midcaps. Updated quarterly by NSE.
_FALLBACK_SYMBOLS = [
    # ── Nifty 50 ──────────────────────────────────────────────────────────────
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "BHARTIARTL",
    "ITC", "KOTAKBANK", "LT", "SBIN", "BAJFINANCE", "ASIANPAINT", "AXISBANK",
    "MARUTI", "SUNPHARMA", "TITAN", "WIPRO", "HCLTECH", "ULTRACEMCO",
    "NESTLEIND", "POWERGRID", "NTPC", "TATAMOTORS", "ADANIENT", "ADANIPORTS",
    "TECHM", "BAJAJFINSV", "DIVISLAB", "GRASIM", "CIPLA", "BRITANNIA", "DRREDDY",
    "HINDALCO", "JSWSTEEL", "TATASTEEL", "BAJAJ-AUTO", "EICHERMOT", "APOLLOHOSP",
    "HEROMOTOCO", "COALINDIA", "BPCL", "ONGC", "IOC", "M&M", "INDUSINDBK",
    "SHREECEM", "PIDILITIND", "TATACONSUM", "DABUR",
    # ── Banking & Finance ──────────────────────────────────────────────────────
    "BANKBARODA", "PNB", "CANBK", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK",
    "AUBANK", "RBLBANK", "ICICIPRULI", "SBILIFE", "HDFCLIFE", "MAXFINSERV",
    "BAJAJHLDNG", "CHOLAFIN", "MUTHOOTFIN", "SHRIRAMFIN", "MANAPPURAM",
    "CAMS", "CDSL", "HDFCAMC", "ABCAPITAL", "SBICARD", "POONAWALLA",
    # ── IT & Technology ────────────────────────────────────────────────────────
    "MPHASIS", "LTIM", "PERSISTENT", "COFORGE", "NAUKRI", "TATAELXSI",
    "KPITTECH", "LTTS", "OFSS", "MFSL",
    # ── Pharma & Healthcare ────────────────────────────────────────────────────
    "TORNTPHARM", "LUPIN", "AUROPHARMA", "ZYDUSLIFE", "ALKEM", "IPCALAB",
    "ABBOTINDIA", "SANOFI", "MANKIND", "LAURUSLABS",
    # ── Consumer & FMCG ───────────────────────────────────────────────────────
    "DMART", "TRENT", "GODREJCP", "COLPAL", "MARICO", "EMAMILTD", "VBL",
    "PGHH", "MCDOWELL-N", "RADICO", "KALYANKJIL", "SENCO", "PATANJALI",
    # ── Automobiles ───────────────────────────────────────────────────────────
    "TVSMOTORS", "MOTHERSON", "BOSCHLTD", "BHARATFORG", "APOLLOTYRE",
    "BALKRISIND", "TIINDIA", "SUNDRMFAST", "ENDURANCE", "MRF",
    # ── Capital Goods & Industrials ────────────────────────────────────────────
    "SIEMENS", "ABB", "CGPOWER", "HAVELLS", "POLYCAB", "CROMPTON",
    "VOLTAS", "BLUESTAR", "CUMMINSIND", "AIAENG", "SKF", "SCHAEFFLER",
    # ── Real Estate ────────────────────────────────────────────────────────────
    "DLF", "GODREJPROP", "LODHA", "PRESTIGE", "OBEROIRLTY", "PHOENIXLTD",
    # ── Energy & Power ────────────────────────────────────────────────────────
    "TATAPOWER", "TORNTPOWER", "ADANIGREEN", "SUZLON", "NHPC",
    "GAIL", "PETRONET", "IGL", "MGL", "RECLTD", "PFC", "IREDA",
    # ── Infra & Logistics ─────────────────────────────────────────────────────
    "RVNL", "IRFC", "CONCOR", "IRCTC", "TITAGARH", "HUDCO",
    # ── Chemicals ─────────────────────────────────────────────────────────────
    "SRF", "NAVINFLUOR", "AARTI", "DEEPAKNITRITE", "LINDEINDIA", "ATUL",
    # ── Diversified / Others ──────────────────────────────────────────────────
    "JIOFIN", "ZOMATO", "TATACOMM", "INDUSTOWER", "ASTRAL", "INDHOTEL",
    "PVRINOX", "MHRIL", "LEMONTREE", "DELHIVERY", "NYKAA", "PAYTM",
]


def _get_nse_session() -> requests.Session:
    """Create a session with NSE cookies (required for their API calls)."""
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get("https://www.nseindia.com/", timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("Could not establish NSE session: %s", exc)
    return session


def fetch_nifty200_symbols(dry_run: bool = False) -> list[str]:
    """Fetch current Nifty 200 constituents from NSE.

    Tries (in order):
      1. NSE archives CSV (no cookies needed, most reliable from VPS)
      2. NSE JSON API (requires cookie session)
      3. Hardcoded fallback (~200 well-known stocks)

    Args:
        dry_run: If True, return the fallback list without making HTTP calls.

    Returns:
        List of NSE trading symbols in the Nifty 200.
    """
    if dry_run:
        logger.info("Dry-run mode — returning fallback Nifty 200 symbols (%d stocks)", len(_FALLBACK_SYMBOLS))
        return list(_FALLBACK_SYMBOLS)

    # ── Attempt 1: NSE archives CSV (no authentication required) ─────────────
    try:
        resp = requests.get(_NSE_NIFTY200_CSV, headers=_HEADERS, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        symbols = []
        for line in lines[1:]:  # skip header
            parts = line.split(",")
            if len(parts) >= 3:
                sym = parts[2].strip().strip('"')
                if sym:
                    symbols.append(sym)
        if len(symbols) >= config.UNIVERSE_MIN_SYMBOLS["nifty200"]:
            logger.info("Fetched %d Nifty 200 symbols from NSE CSV", len(symbols))
            return symbols
        logger.warning("NSE CSV returned only %d symbols — trying JSON API", len(symbols))
    except Exception as exc:
        logger.warning("NSE CSV fetch failed: %s — trying JSON API", exc)

    # ── Attempt 2: NSE JSON API (needs cookie session) ────────────────────────
    session = _get_nse_session()
    time.sleep(config.NSE_SCRAPE_DELAY_SECONDS)
    try:
        resp = session.get(_NSE_NIFTY200_URL, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        symbols = [item["symbol"] for item in data.get("data", []) if "symbol" in item]
        if len(symbols) >= config.UNIVERSE_MIN_SYMBOLS["nifty200"]:
            logger.info("Fetched %d Nifty 200 symbols from NSE API", len(symbols))
            return symbols
        logger.warning("NSE API returned only %d symbols — using fallback", len(symbols))
    except Exception as exc:
        logger.error("Failed to fetch Nifty 200 from NSE API: %s — using fallback", exc)

    return list(_FALLBACK_SYMBOLS)


def fetch_nifty500_symbols(dry_run: bool = False) -> list[str]:
    """Fetch current Nifty 500 constituents from NSE.

    Same fallback ladder as the Nifty 200 fetcher. When both NSE sources
    fail, falls back to the Nifty 200 fallback list — a smaller universe is
    a safe degradation, a stale wide one is not.
    """
    if dry_run:
        return list(_FALLBACK_SYMBOLS)

    try:
        resp = requests.get(_NSE_NIFTY500_CSV, headers=_HEADERS, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        symbols = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 3:
                sym = parts[2].strip().strip('"')
                if sym:
                    symbols.append(sym)
        if len(symbols) >= config.UNIVERSE_MIN_SYMBOLS["nifty500"]:
            logger.info("Fetched %d Nifty 500 symbols from NSE CSV", len(symbols))
            return symbols
        logger.warning("NSE CSV returned only %d symbols — trying JSON API", len(symbols))
    except Exception as exc:
        logger.warning("NSE Nifty 500 CSV fetch failed: %s — trying JSON API", exc)

    session = _get_nse_session()
    time.sleep(config.NSE_SCRAPE_DELAY_SECONDS)
    try:
        resp = session.get(_NSE_NIFTY500_URL, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        symbols = [item["symbol"] for item in data.get("data", []) if "symbol" in item]
        if len(symbols) >= config.UNIVERSE_MIN_SYMBOLS["nifty500"]:
            logger.info("Fetched %d Nifty 500 symbols from NSE API", len(symbols))
            return symbols
        logger.warning("NSE API returned only %d symbols — falling back to Nifty 200", len(symbols))
    except Exception as exc:
        logger.error("Failed to fetch Nifty 500 from NSE API: %s — falling back to Nifty 200", exc)

    return fetch_nifty200_symbols()


def fetch_universe_symbols(dry_run: bool = False) -> list[str]:
    """Fetch the configured universe (config.UNIVERSE_INDEX)."""
    if config.UNIVERSE_INDEX == "nifty500":
        return fetch_nifty500_symbols(dry_run=dry_run)
    return fetch_nifty200_symbols(dry_run=dry_run)


def save_universe_snapshot(symbols: list[str], snapshot_date: Optional[date] = None) -> None:
    """Persist the universe snapshot to the fundamentals database.

    Args:
        symbols:       List of symbols in today's universe.
        snapshot_date: Date for this snapshot; defaults to today.
    """
    snap_date = snapshot_date or date.today()
    db_path = Path(config.DB_PATH) / "fundamentals.db"
    engine = create_engine(f"sqlite:///{db_path}")
    meta = MetaData()

    universe_table = Table(
        "universe_snapshots",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("snapshot_date", Date, nullable=False),
        Column("symbol", String(20), nullable=False),
    )
    meta.create_all(engine)

    with engine.begin() as conn:
        # Remove today's snapshot (idempotent re-run)
        conn.execute(
            universe_table.delete().where(universe_table.c.snapshot_date == snap_date)
        )
        rows = [{"snapshot_date": snap_date, "symbol": s} for s in symbols]
        conn.execute(universe_table.insert(), rows)

    logger.info("Saved %d symbols to universe snapshot for %s", len(symbols), snap_date)


def load_universe(snapshot_date: Optional[date] = None) -> list[str]:
    """Load the universe from the database for a given date.

    Args:
        snapshot_date: Date to load; defaults to the most recent available snapshot.

    Returns:
        List of symbol strings.
    """
    db_path = Path(config.DB_PATH) / "fundamentals.db"
    if not db_path.exists():
        logger.warning("fundamentals.db not found — returning fallback universe")
        return list(_FALLBACK_SYMBOLS)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        if snapshot_date:
            df = pd.read_sql(
                f"SELECT symbol FROM universe_snapshots WHERE snapshot_date = '{snapshot_date}'",
                engine,
            )
        else:
            df = pd.read_sql(
                "SELECT symbol FROM universe_snapshots "
                "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM universe_snapshots)",
                engine,
            )
        symbols = df["symbol"].tolist()
        if not symbols:
            logger.warning("No universe snapshot found — returning fallback")
            return list(_FALLBACK_SYMBOLS)
        return symbols
    except Exception as exc:
        logger.error("Failed to load universe from DB: %s", exc)
        return list(_FALLBACK_SYMBOLS)


if __name__ == "__main__":
    from utils.logger import setup_logging
    setup_logging()
    parser = argparse.ArgumentParser(description="Fetch and cache Nifty 200 universe")
    parser.add_argument("--dry-run", action="store_true", help="Use fallback symbols, no HTTP calls")
    args = parser.parse_args()
    symbols = fetch_nifty200_symbols(dry_run=args.dry_run)
    save_universe_snapshot(symbols)
    print(f"Universe: {len(symbols)} stocks")
    print(", ".join(symbols[:10]), "...")
