#!/usr/bin/env python3
"""Run a targeted strategy backtest with custom runtime speed optimizations."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import setup_logging
setup_logging()

logger = logging.getLogger("run_backtest")

from backtest.data_loader import load_ohlcv
from backtest.backtest_engine import BacktestEngine
from data_fetcher.ohlcv_fetcher import OHLCVFetcher

def _check_and_populate_data(start: date, end: date) -> bool:
    """Ensure baseline proxy arrays exist locally before starting backtest loops."""
    fetcher = OHLCVFetcher(kite=None)
    delta_days = (date.today() - (start - timedelta(days=200))).days
    
    # Secure tracking arrays for core verification assets
    for symbol in ["NIFTY50", "GOLDBEES", "RELIANCE", "TCS", "INFY"]:
        fetcher.get_ohlcv(symbol, 0, days=delta_days)
    return True

def main() -> None:
    parser = argparse.ArgumentParser(description="Run speed-controlled strategy backtest")
    parser.add_argument(
        "--window", choices=["1m", "6m", "1y", "5y", "custom"], default="1m",
        help="Predefined evaluation windows.",
    )
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--capital", type=float, default=100000.0,
        help="Starting capital deployment allocation rules (default: 1 Lakh)",
    )
    parser.add_argument(
        "--strategy", choices=["mean_reversion", "momentum", "breakout"], default="mean_reversion"
    )
    parser.add_argument(
        "--ignore-regime", action="store_true",
        help="Bypass the broad equity BEAR market filter block to force trade entries.",
    )
    
    args = parser.parse_args()

    today = date.today()
    if args.window == "1m":
        start_dt = today - timedelta(days=30)
        end_dt = today
    elif args.window == "6m":
        start_dt = today - timedelta(days=180)
        end_dt = today
    elif args.window == "1y":
        start_dt = today - timedelta(days=365)
        end_dt = today
    elif args.window == "5y":
        start_dt = today - timedelta(days=5 * 365)
        end_dt = today
    else:
        start_dt = date.fromisoformat(args.start)
        end_dt = date.fromisoformat(args.end)

    _check_and_populate_data(start_dt, end_dt)

    logger.info(
        "Executing target matrix run: %s -> %s | Window: %s | Ignore Regime Guard: %s",
        start_dt, end_dt, args.window, args.ignore_regime
    )

    engine = BacktestEngine(
        start_date=start_dt,
        end_date=end_dt,
        starting_capital=args.capital,
        strategy=args.strategy,
        cost_profile="cnc",
        ignore_regime=args.ignore_regime
    )
    
    metrics = engine.run()

if __name__ == "__main__":
    main()
