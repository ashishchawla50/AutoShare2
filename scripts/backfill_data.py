"""Backfill historical OHLCV data for all universe stocks.

Run once during initial setup AFTER the Kite access token has been obtained.
Fetches 2 years of daily OHLCV for every Nifty 200 stock and caches to SQLite.

Usage:
    python scripts/backfill_data.py
    python scripts/backfill_data.py --days 500   # override lookback days
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import setup_logging
setup_logging()

logger = logging.getLogger("data.backfill")

from broker.auth_manager import AuthManager
from broker.kite_client import KiteClient
from config.config import config
from data_fetcher.ohlcv_fetcher import OHLCVFetcher
from data_fetcher.universe_fetcher import fetch_universe_symbols


def backfill(days: int = 730) -> None:
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    logger.info("Backfill: %s → %s (%d days)", start_date, end_date, days)

    # Load universe
    symbols = fetch_universe_symbols()
    logger.info("Universe: %d symbols", len(symbols))

    # Always connect to Kite for data — paper mode only blocks real orders, not data fetching
    auth = AuthManager()
    token = auth.get_access_token()
    if not token:
        print("ERROR: No access token. Run auth flow first (python broker/auth_server.py).")
        sys.exit(1)
    kite = KiteClient()
    kite.connect(token)
    fetcher = OHLCVFetcher(kite.raw)

    # Load instrument master to map symbols → tokens
    instruments = kite.get_instruments("NSE")
    token_map = {
        inst["tradingsymbol"]: inst["instrument_token"]
        for inst in instruments
        if inst["instrument_type"] == "EQ"
    }
    logger.info("Instrument master loaded: %d EQ instruments", len(token_map))

    failed = []
    for i, symbol in enumerate(symbols, 1):
        try:
            inst_token = token_map.get(symbol)
            if not inst_token:
                logger.warning("No instrument token for %s — skipping", symbol)
                continue
            df = fetcher.get_ohlcv(symbol=symbol, instrument_token=inst_token, days=days)
            row_count = 0 if df is None else len(df)
            logger.info("[%d/%d] %s: %d rows", i, len(symbols), symbol, row_count)
            print(f"[{i}/{len(symbols)}] {symbol}: {row_count} rows")
            time.sleep(0.4)  # stay well within 200 historical calls/min
        except Exception as exc:
            logger.error("Failed to fetch %s: %s", symbol, exc)
            failed.append(symbol)
            time.sleep(2)

    print(f"\nBackfill complete: {len(symbols) - len(failed)}/{len(symbols)} symbols")
    if failed:
        print(f"Failed: {failed}")

    # Fetch Nifty 50 index separately — needed by load_nifty_ohlcv() for benchmark/RS calculations
    # Kite lists indices with segment "INDICES" and instrument_type "EQ" —
    # filtering on instrument_type "INDEX" silently finds nothing
    nifty_token = next(
        (inst["instrument_token"] for inst in instruments
         if inst.get("tradingsymbol") in ("NIFTY 50", "NIFTY50")
         and inst.get("segment") == "INDICES"),
        None,
    )
    if nifty_token:
        df = fetcher.get_ohlcv(symbol="NIFTY50", instrument_token=nifty_token, days=days)
        row_count = 0 if df is None else len(df)
        print(f"NIFTY50 index: {row_count} rows")
        logger.info("Nifty 50 backfill: %d rows", row_count)
    else:
        logger.warning("Nifty 50 index token not found in instrument master — skipping benchmark data")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical OHLCV data")
    parser.add_argument("--days", type=int, default=730, help="Lookback days (default: 730)")
    args = parser.parse_args()
    backfill(days=args.days)
