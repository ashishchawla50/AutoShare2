#!/usr/bin/env python3
"""Run a vcp_v2 backtest in seconds on the local reconstructed dataset.

Results are identical to the reference engine (backtest/backtest_engine.py) — proven
trade-for-trade against its frozen fixtures by tests/test_fast_engine.py.

    python scripts/fast_backtest.py                                # full 2021-09 → 2026-08, ₹50k
    python scripts/fast_backtest.py --start 2025-01-01 --end 2026-06-30
    python scripts/fast_backtest.py --start 2021-06-01 --end 2024-12-31 --no-dd-halt
    python scripts/fast_backtest.py --dump-trades trades.csv
    python scripts/fast_backtest.py --rebuild-cache                # after rebuilding reconstructed.db

The first run builds array caches under data/cache/fast (~45 s); later runs reuse them.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import warnings
warnings.filterwarnings("ignore", message=".*pyarrow.*", category=DeprecationWarning)

from backtest.fast.data import Universe, load_market_data
from backtest.fast.engine import FastBacktestEngine
from backtest.fast.signals import load_signals
from backtest.performance_analyzer import print_report
from config.config import config
from scripts.run_backtest import _parity, _parity_line


def main() -> None:
    p = argparse.ArgumentParser(description="Fast vcp_v2 backtest (reconstructed dataset)")
    p.add_argument("--start", default=config.BACKTEST_DATASET_FLOOR,
                   help=f"YYYY-MM-DD (default {config.BACKTEST_DATASET_FLOOR})")
    p.add_argument("--end", default="2026-08-10", help="YYYY-MM-DD (default 2026-08-10, dataset end)")
    p.add_argument("--capital", type=float, default=config.STARTING_CAPITAL)
    p.add_argument("--cost-model", choices=["cnc", "legacy"], default="cnc")
    p.add_argument("--no-dd-halt", action="store_true",
                   help="disable the absorbing 20%% drawdown halt to measure the underlying edge")
    p.add_argument("--dump-trades", metavar="CSV", help="write the trade ledger to CSV")
    p.add_argument("--rebuild-cache", action="store_true", help="rebuild array + signal caches")
    p.add_argument("--parity-off", metavar="RULES", default="",
                   help="vcp_v2: switch off live-parity rules, comma-separated "
                        "(open_cap,regime_exit,vix,dd_pause,loss_limits,live_filters) or 'all' "
                        "for the pre-2026-10-19 engine. Default: all on")
    args = p.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # report uses → and ₹; Windows console is cp1252
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    t0 = time.perf_counter()
    md = load_market_data(rebuild=args.rebuild_cache)
    sig = load_signals(md, rebuild=args.rebuild_cache)
    engine = FastBacktestEngine(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        starting_capital=args.capital,
        cost_profile=args.cost_model,
        dd_halt=not args.no_dd_halt,
        market=md, signals=sig, universe=Universe(),
        verbose=False, parity=_parity(args.parity_off),
    )
    metrics = engine.run()
    print_report(metrics)

    s = engine._stats
    print(f"Funnel: scanned {s['data_ok']:,} | trend-template fail {s['fail_tt']:,} | "
          f"VCP fail {s['fail_vcp']:,} | tightness fail {s['fail_tightness']:,} | "
          f"pivots watched {s['pivot_watch_added']:,} (expired {s['pivot_watch_expired']:,}) | "
          f"breakouts {s['high_conf']} | gap-filtered {s['gap_filtered']} | "
          f"size-invalid {s['size_invalid']} | entered {s['entered']}")
    print(f"Regime-blocked days: {s['regime_blocked_days']} | "
          f"drawdown-halt days: {s['dd_halted_days']}"
          + (f" (first {engine._dd_halted_on})" if engine._dd_halted_on else ""))
    print(_parity_line(engine))
    print(f"Runtime: {time.perf_counter() - t0:.1f} s  "
          + "  ".join(f"{k}={v:.2f}s" for k, v in engine.timings.items()))

    if args.dump_trades:
        import pandas as pd
        pd.DataFrame(engine._closed_trades).to_csv(args.dump_trades, index=False)
        print(f"Trades written to {args.dump_trades}")


if __name__ == "__main__":
    main()
