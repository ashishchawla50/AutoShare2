"""Per-symbol-day comparison of the panel pre-gate against the reference trend template.

READ-ONLY. Modifies no engine, strategy, or gate code. It replicates the engine's own
iteration exactly — same universe, same warmup, same row slicing — and for every
(symbol, decision-date) pair the engine would evaluate it asks both implementations
the same question and records where they disagree.

Engine path being mirrored (backtest_engine.py):
    _preload_data : symbols = UNION of PIT universe over every trading day
                    data_start = start_date - 730d ; keep df if len(df) >= 210
    _scan_entries : n = _rows_upto(symbol, sim_date) ; skip if n < 210
                    sim_df = df.iloc[:n] ; prev_day_df = sim_df.iloc[:-1]
    line 707      : gate = trend_template_gate(load_panel(isin), prev_day_df.index[-1])
                    reference = analyse_stock(symbol, prev_day_df) -> passes_trend_template

A disagreement here is a genuine behavioural divergence: gate=False makes the engine
record fail_tt and skip the pivot-watch add, where the reference would have gone on to
VCP detection and possibly opened a watch (and later a trade).

Usage:
    python tools/diag_gate_agreement.py --window 1month
    python tools/diag_gate_agreement.py --start 2024-01-01 --end 2024-01-31
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from config.config import config as cfg

cfg.DB_PATH = str(REPO / "data")

import numpy as np

from backtest.data_loader import load_ohlcv
from backtest.point_in_time_universe import PointInTimeUniverse
from config.holidays import get_trading_days
from research.fast_gate import trend_template_gate
from research.indicator_panel import load_panel
from strategy.trend_template import passes_trend_template
from tools.capture_reference_oracle import WINDOWS

OUT_DIR = REPO / "data" / "diag"
WARMUP_DAYS = 730          # backtest_engine.py:1033
MIN_ROWS = 210             # backtest_engine.py:506,511


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default=None, help="named oracle window")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--max-report", type=int, default=40)
    a = ap.parse_args()

    if a.window:
        s, e = WINDOWS[a.window]
        tag = a.window
    else:
        s, e = a.start, a.end
        tag = f"{s}_{e}"
    start, end = date.fromisoformat(s), date.fromisoformat(e)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trading_days = get_trading_days(start, end)
    pit = PointInTimeUniverse()

    print(f"window        : {start} -> {end}")
    print(f"trading days  : {len(trading_days)}")

    symbols, seen = [], set()
    for d in trading_days:
        for sym in pit.get_universe(d):
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)
    print(f"universe union: {len(symbols)} isins")

    data_start = start - timedelta(days=WARMUP_DAYS)
    print(f"data_start    : {data_start} (warmup {WARMUP_DAYS}d)")

    t0 = time.time()
    cache, ords = {}, {}
    for sym in symbols:
        df = load_ohlcv(sym, data_start, end)
        if df is not None and len(df) >= MIN_ROWS:
            cache[sym] = df
            ords[sym] = df.index.values.astype("datetime64[D]").astype(np.int64)
    print(f"loaded        : {len(cache)}/{len(symbols)} isins  ({time.time()-t0:.0f}s)")

    stats = {"evaluated": 0, "gate_abstain": 0, "gate_pass": 0, "gate_reject": 0,
             "agree": 0, "disagree": 0,
             "disagree_gate_rejects_ref_passes": 0,
             "disagree_gate_passes_ref_rejects": 0}
    rows = []

    t0 = time.time()
    for sim_date in trading_days:
        for sym in pit.get_universe(sim_date):
            df = cache.get(sym)
            if df is None:
                continue
            n = int(np.searchsorted(ords[sym],
                                    np.datetime64(sim_date, "D").astype(np.int64),
                                    side="right"))
            if n < MIN_ROWS:
                continue
            prev = df.iloc[:n - 1]                      # engine: sim_df.iloc[:-1]
            if len(prev) == 0:
                continue
            stats["evaluated"] += 1

            g = trend_template_gate(load_panel(sym), prev.index[-1])
            if not g.decided:
                stats["gate_abstain"] += 1
                continue
            stats["gate_pass" if g.passes else "gate_reject"] += 1

            ref = passes_trend_template(prev)
            if bool(ref.passes) == bool(g.passes):
                stats["agree"] += 1
                continue

            stats["disagree"] += 1
            key = ("disagree_gate_rejects_ref_passes" if ref.passes
                   else "disagree_gate_passes_ref_rejects")
            stats[key] += 1

            panel = load_panel(sym)
            prow = panel.loc[prev.index[-1]]
            rows.append({
                "date": str(sim_date),
                "decision_bar": str(prev.index[-1].date()),
                "isin": sym,
                "symbol": pit.symbol_at(sym, sim_date) or sym,
                "gate_passes": bool(g.passes),
                "reference_passes": bool(ref.passes),
                "gate_failing": list(g.failing),
                "reference_failing": [k for k, v in ref.conditions.items() if not v],
                "rows_loaded": int(len(prev)),
                "panel_bars_available": float(prow.get("bars_available", float("nan"))),
                "panel": {c: (None if prow.get(c) is None else float(prow.get(c)))
                          for c in ("close", "ema_50", "ema_150", "ema_200",
                                    "ema_200_20d_ago", "close_lo_252", "close_hi_252")},
                "reference_conditions": {k: bool(v) for k, v in ref.conditions.items()},
                "reference_current_price": float(ref.current_price),
            })
        if len(rows) >= a.max_report * 4:
            print(f"  (stopping early at {sim_date}: {len(rows)} disagreements collected)")
            break

    elapsed = time.time() - t0
    print(f"scan          : {elapsed:.0f}s\n")
    print(json.dumps(stats, indent=1))

    out = OUT_DIR / f"gate_agreement_{tag}.json"
    out.write_text(json.dumps({"window": [s, e], "stats": stats,
                               "disagreements": rows}, indent=1))
    print(f"\nwrote {out}")

    if rows:
        rows.sort(key=lambda r: (r["date"], r["isin"]))
        print(f"\nFIRST {min(a.max_report, len(rows))} DISAGREEMENTS "
              f"(earliest first)\n" + "=" * 78)
        for r in rows[:a.max_report]:
            print(f"{r['date']}  {r['symbol']:<12} {r['isin']}  "
                  f"gate={'PASS' if r['gate_passes'] else 'REJECT'}  "
                  f"reference={'PASS' if r['reference_passes'] else 'REJECT'}")
            print(f"    decision bar {r['decision_bar']}  rows_loaded={r['rows_loaded']}  "
                  f"panel_bars_available={r['panel_bars_available']:.0f}")
            print(f"    gate failing      : {r['gate_failing']}")
            print(f"    reference failing : {r['reference_failing']}")
            p = r["panel"]
            print(f"    panel  close={p['close']:.4f} ema50={p['ema_50']:.4f} "
                  f"ema150={p['ema_150']:.4f} ema200={p['ema_200']:.4f}")
            print(f"           ema200_20d_ago={p['ema_200_20d_ago']:.4f} "
                  f"lo252={p['close_lo_252']:.4f} hi252={p['close_hi_252']:.4f}")
            print(f"    reference close={r['reference_current_price']:.4f}")
    else:
        print("\nNO DISAGREEMENTS — gate is verdict-identical to the reference "
              "on every decided symbol-day in this window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
