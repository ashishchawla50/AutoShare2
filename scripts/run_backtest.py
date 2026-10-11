#!/usr/bin/env python3
"""Quick strategy backtest: seconds, local, on the reconstructed dataset.

    python scripts/run_backtest.py --window 6m --strategy mean_reversion --capital 100000 --ignore-regime
    python scripts/run_backtest.py --window 1y                       # vcp_v2, ₹50k
    python scripts/run_backtest.py --strategy momentum_v3 --window 3y --no-dd-halt
    python scripts/run_backtest.py --start 2025-01-01 --end 2026-06-30 --strategy breakout
    python scripts/run_backtest.py --window full --strategy all --dump-trades trades.csv

Windows count back from the last date in the dataset (not today's date), so "6m"
is always six months of real data.

vcp_v2 runs on backtest/fast/engine.py, proven trade-for-trade against the frozen
reference oracle (tests/test_fast_engine.py). The other strategies run on
backtest/fast/rule_engine.py. Their entry signals are checked row-by-row against
the strategy code (tests/test_fast_rules.py), but their portfolio loop has no
frozen oracle.

The slow reference engine is still available as `python backtest/backtest_engine.py`.
First run after a dataset or rule change builds caches (about a minute); later runs
take a few seconds.
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
warnings.filterwarnings("ignore", message="(?s).*pyarrow", category=DeprecationWarning)

import numpy as np
import pandas as pd

from backtest.fast.data import Universe, load_market_data
from backtest.fast.engine import FastBacktestEngine
from backtest.fast.rule_engine import RULE_STRATEGIES, FastRuleEngine
from backtest.fast.signals import load_signals
from backtest.performance_analyzer import print_report

STRATEGIES = ("vcp_v2",) + RULE_STRATEGIES
WINDOWS = {"1m": 1, "3m": 3, "6m": 6, "1y": 12, "2y": 24, "3y": 36, "5y": 60}
DATASET_FLOOR = date(2021, 9, 1)     # earliest start with a warmed-up regime benchmark


def _resolve_dates(args: argparse.Namespace, data_end: date) -> tuple[date, date]:
    end = date.fromisoformat(args.end) if args.end else data_end
    if args.start:
        start = date.fromisoformat(args.start)
    elif args.window == "full":
        start = DATASET_FLOOR
    else:
        start = (pd.Timestamp(end) - pd.DateOffset(months=WINDOWS[args.window])).date()
    if start < DATASET_FLOOR:
        print(f"Note: start {start} is before the dataset floor; using {DATASET_FLOOR}")
        start = DATASET_FLOOR
    if end > data_end:
        print(f"Note: dataset ends {data_end}; using that as the end date")
        end = data_end
    if start >= end:
        sys.exit(f"start {start} must be before end {end}")
    return start, end


def _funnel(strategy: str, s: dict) -> str:
    head = f"Funnel: scanned {s['data_ok']:,} | "
    tail = (f"signals {s['high_conf']} | gap-filtered {s['gap_filtered']} | "
            f"size-invalid {s['size_invalid']} | entered {s['entered']}")
    if strategy == "vcp_v2":
        return (head + f"trend-template fail {s['fail_tt']:,} | VCP fail {s['fail_vcp']:,} | "
                f"tightness fail {s['fail_tightness']:,} | pivots watched {s['pivot_watch_added']:,} "
                f"(expired {s['pivot_watch_expired']:,}) | " + tail)
    parts = []
    if strategy in ("mean_reversion", "all"):
        parts.append(f"MR fail: trend {s['mr_fail_trend_gate']:,} rsi {s['mr_fail_rsi_entry']:,} "
                     f"other {s['mr_fail_other']:,}")
    if strategy.startswith("momentum") or strategy == "all":
        parts.append(f"momentum fail: ema {s['mom_fail_ema']:,} volume {s['mom_fail_volume']:,} "
                     f"proximity {s['mom_fail_proximity']:,} rsi {s['mom_fail_rsi']:,}")
    if strategy in ("momentum_v3", "momentum_v4"):
        parts.append(f"v3 gates fail: stage2 {s['mom3_fail_stage2']:,} rs {s['mom3_fail_rs']:,}")
    if strategy.startswith("breakout") or strategy == "all":
        parts.append(f"breakout fail: new-high {s['bo_fail_new_high']:,} volume {s['bo_fail_volume']:,} "
                     f"ema {s['bo_fail_ema']:,} rsi {s['bo_fail_rsi']:,}")
    return head + " | ".join(parts) + " | " + tail


def main() -> None:
    p = argparse.ArgumentParser(description="Fast strategy backtest (reconstructed dataset)")
    p.add_argument("--strategy", choices=STRATEGIES, default="vcp_v2")
    p.add_argument("--window", choices=list(WINDOWS) + ["full"], default="full",
                   help="period ending on the dataset's last date (default: full history)")
    p.add_argument("--start", help="YYYY-MM-DD, overrides --window")
    p.add_argument("--end", help="YYYY-MM-DD (default: dataset's last date)")
    p.add_argument("--capital", type=float, default=50_000.0)
    p.add_argument("--ignore-regime", action="store_true",
                   help="trade every day at full size: BEAR no longer blocks, CAUTIOUS no longer halves")
    p.add_argument("--no-dd-halt", action="store_true",
                   help="disable the absorbing 20%% drawdown halt to measure the underlying edge")
    p.add_argument("--cost-model", choices=["cnc", "legacy"], default="cnc")
    p.add_argument("--dump-trades", metavar="CSV", help="write the trade ledger to CSV")
    p.add_argument("--rebuild-cache", action="store_true", help="rebuild array + signal caches")
    args = p.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # report uses → and ₹; Windows console is cp1252
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    t0 = time.perf_counter()
    md = load_market_data(rebuild=args.rebuild_cache)
    start, end = _resolve_dates(args, np.datetime64(int(md.dates.max()), "D").item())

    common = dict(start_date=start, end_date=end, starting_capital=args.capital,
                  cost_profile=args.cost_model, dd_halt=not args.no_dd_halt,
                  market=md, signals=load_signals(md, rebuild=args.rebuild_cache),
                  universe=Universe(), verbose=False, ignore_regime=args.ignore_regime)
    if args.strategy == "vcp_v2":
        engine = FastBacktestEngine(**common)
    else:
        from backtest.fast.rules import load_rule_signals
        engine = FastRuleEngine(strategy=args.strategy,
                                rules=load_rule_signals(md, rebuild=args.rebuild_cache), **common)

    print(f"{args.strategy} | {start} → {end} | ₹{args.capital:,.0f} | costs {args.cost_model} | "
          f"regime {'IGNORED' if args.ignore_regime else 'gated'} | "
          f"dd-halt {'off' if args.no_dd_halt else 'on'}")
    metrics = engine.run()
    print_report(metrics)

    s = engine._stats
    print(_funnel(args.strategy, s))
    print(f"Regime-blocked days: {s['regime_blocked_days']}"
          + (f" | BULL-only skipped days: {s['bull_only_skipped_days']}"
             if args.strategy == "momentum_v4" else "")
          + f" | drawdown-halt days: {s['dd_halted_days']}"
          + (f" (first {engine._dd_halted_on})" if engine._dd_halted_on else ""))
    print(f"Runtime: {time.perf_counter() - t0:.1f} s")

    if args.dump_trades:
        pd.DataFrame(engine._closed_trades).to_csv(args.dump_trades, index=False)
        print(f"Trades written to {args.dump_trades}")


if __name__ == "__main__":
    main()
