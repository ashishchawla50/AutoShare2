#!/usr/bin/env python3
"""D1 (docs/TEST_PLAN_2026_10.md): match live trades against a backtest of the same period.

    python tools/compare_live_backtest.py --db path/to/portfolio_state.db
    python tools/compare_live_backtest.py --db vps_copy.db --capital 50000 --since 2026-07-20

Copy the VPS database first (read-only), e.g.
    scp -i <key> ubuntu@<vps>:~/AutoShare/data/portfolio_state.db data/vps_portfolio_state.db

Live trades (is_paper = 0, strategy vcp/vcp_v2) are matched to backtest trades on ISIN +
entry date. The backtest starts 120 days before the first live trade so its pivot
watchlist is warm, and only its trades on or after the first live entry are compared.
Limits: the backtest's dataset (reconstructed.db) ends on its build date, so live trades
after that cannot be matched until the dataset is rebuilt.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np
import pandas as pd

from backtest.fast.data import Universe, load_market_data
from backtest.fast.engine import FastBacktestEngine
from backtest.fast.signals import load_signals


def load_live(db: Path, since: str | None) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        df = pd.read_sql("SELECT symbol, entry_date, entry_price, shares, exit_date, exit_price, "
                         "exit_reason, realised_pnl, realised_pnl_pct, strategy, is_paper FROM trades", con)
    finally:
        con.close()
    df = df[(df.is_paper == 0) & df.strategy.fillna("vcp").str.startswith("vcp")]
    df["entry_date"] = pd.to_datetime(df.entry_date).dt.date
    if since:
        df = df[df.entry_date >= date.fromisoformat(since)]
    return df.reset_index(drop=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", required=True, help="portfolio_state.db (copy from the VPS)")
    p.add_argument("--capital", type=float, default=50_000.0)
    p.add_argument("--since", help="only live trades entered on/after YYYY-MM-DD")
    args = p.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    live = load_live(Path(args.db), args.since)
    if live.empty:
        print("No live vcp trades in that database (is_paper = 0). Nothing to compare yet.")
        return

    md = load_market_data()
    uni = Universe()
    data_end = np.datetime64(int(md.dates.max()), "D").item()
    first = min(live.entry_date)
    sym_to_isin = {}
    for isin, spans in uni._symbols.items():
        for f, t, sym in spans:
            sym_to_isin.setdefault(sym, []).append((f, t, isin))

    def isin_of(sym: str, d: date):
        for f, t, isin in sym_to_isin.get(sym, ()):
            if f <= d.isoformat() <= t:
                return isin
        return None

    live["isin"] = [isin_of(s, d) for s, d in zip(live.symbol, live.entry_date)]
    eng = FastBacktestEngine(start_date=first - timedelta(days=120), end_date=data_end,
                             starting_capital=args.capital, market=md, signals=load_signals(md),
                             universe=uni, verbose=False, dd_halt=True)
    eng.run()
    bt = pd.DataFrame(eng._closed_trades)
    bt = bt[bt.entry_date >= first] if not bt.empty else bt

    comparable = live[live.entry_date <= data_end]
    print(f"Live vcp trades: {len(live)} ({len(comparable)} inside the dataset, which ends {data_end})")
    print(f"Backtest trades from {first}: {len(bt)}\n")
    key_bt = {(r.isin, r.entry_date): r for r in bt.itertuples()} if not bt.empty else {}
    matched = 0
    for r in comparable.itertuples():
        b = key_bt.pop((r.isin, r.entry_date), None)
        if b is None:
            print(f"  LIVE ONLY   {r.symbol:12s} {r.entry_date}  live P&L {r.realised_pnl_pct or 0:+.1f}%")
            continue
        matched += 1
        print(f"  MATCH       {r.symbol:12s} {r.entry_date}  entry live {r.entry_price:.2f} / bt {b.entry_price:.2f} | "
              f"exit live {r.exit_date} {r.exit_reason} / bt {b.exit_date} {b.exit_reason} | "
              f"P&L live {r.realised_pnl_pct or 0:+.1f}% / bt {b.realised_pnl_pct:+.1f}%")
    for (isin, d), b in sorted(key_bt.items(), key=lambda kv: kv[0][1]):
        print(f"  BACKTEST ONLY {b.symbol or isin:10s} {d}  bt P&L {b.realised_pnl_pct:+.1f}% ({b.exit_reason})")
    print(f"\nMatched {matched} of {len(comparable)} live trades; "
          f"{len(key_bt)} backtest trades had no live counterpart.")


if __name__ == "__main__":
    main()
