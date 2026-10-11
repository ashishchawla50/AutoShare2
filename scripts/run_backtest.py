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

from backtest.data_loader import load_nifty_ohlcv, load_ohlcv
from backtest.backtest_engine import BacktestEngine
from config.config import config
from data_fetcher.universe_fetcher import load_universe
from data_fetcher.ohlcv_fetcher import OHLCVFetcher


def _check_and_populate_data(start: date, end: date) -> bool:
    """Ensure data is loaded via free fetchers if local database cache is empty."""
    # Ensure baseline index asset exists for regime gates
    fetcher = OHLCVFetcher(kite=None)
    delta_days = (date.today() - (start - timedelta(days=365))).days
    
    fetcher.get_ohlcv("NIFTY50", 0, days=delta_days)
    fetcher.get_ohlcv("GOLDBEES", 0, days=delta_days)
    
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run speed-controlled strategy backtest")
    parser.add_argument(
        "--window", choices=["1m", "6m", "1y", "5y", "custom"], default="1m",
        help="Predefined historical evaluation speed windows. Overrides --start and --end strings.",
    )
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (Only active if --window custom)")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (Only active if --window custom)")
    parser.add_argument(
        "--capital", type=float, default=100000.0,
        help="Starting capital deployment allocation rules (default: 1 Lakh)",
    )
    parser.add_argument(
        "--strategy",
        choices=["vcp", "vcp_v2", "momentum", "momentum_v2", "momentum_v3", "momentum_v4", "breakout", "breakout_v2", "mean_reversion", "all"],
        default="mean_reversion",
    )
    parser.add_argument("--no-dd-halt", action="store_true", help="Disable structural drawdown lock out filters")
    parser.add_argument("--dump-trades", default=None, metavar="CSV", help="Write tracking sheet to local path file")
    
    args = parser.parse_args()

    # 1. Resolve date parameters based on selected window sizes
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
        if not args.start or not args.end:
            logger.error("Custom window mode requires explicit --start and --end arguments")
            sys.exit(1)
        start_dt = date.fromisoformat(args.start)
        end_dt = date.fromisoformat(args.end)

    if start_dt >= end_dt:
        logger.error("Start timeline validation bound must sit prior to end timeline boundary")
        sys.exit(1)

    # 2. Guarantee structural historical tables are populated for free
    _check_and_populate_data(start_dt, end_dt)

    logger.info(
        "Executing target matrix run: %s -> %s | Phase Window: %s | Active Capital Size: Rs %s",
        start_dt, end_dt, args.window, f"{args.capital:,.0f}",
    )

    engine = BacktestEngine(
        start_date=start_dt,
        end_date=end_dt,
        starting_capital=args.capital,
        strategy=args.strategy,
        events_filter=False,
        dd_halt=not args.no_dd_halt,
        cost_profile="cnc",  # Using real Zerodha delivery pricing layout
    )
    
    try:
        metrics = engine.run()
        if args.dump_trades and metrics.total_trades > 0:
            import pandas as pd
            pd.DataFrame(engine._closed_trades).to_csv(args.dump_trades, index=False)
    except Exception as e:
        logger.error("Engine generation runtime halted mid-cycle: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
