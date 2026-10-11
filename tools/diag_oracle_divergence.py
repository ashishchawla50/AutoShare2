"""Diagnose an oracle parity failure in full detail. READ-ONLY.

Runs the OPTIMIZED engine once for one window, serialises its output to
`data/diag/<window>_optimized.json` so the run never has to be repeated, then diffs
it against the frozen oracle: portfolio, every counter, set-difference of trades,
and the first positional field-level divergence with surrounding context.

Never touches the reference engine, the fixtures, or any engine/strategy code.

Usage:
    python tools/diag_oracle_divergence.py --window multiyear
    python tools/diag_oracle_divergence.py --window full
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from config.config import config as cfg

cfg.DB_PATH = str(REPO / "data")     # conftest isolation is not active outside pytest

FIXTURE_DIR = REPO / "tests" / "fixtures" / "oracle"
OUT_DIR = REPO / "data" / "diag"

TRADE_FIELDS = ("isin", "symbol", "strategy", "entry_date", "exit_date",
                "entry_price", "exit_price", "shares", "position_value",
                "realised_pnl", "realised_pnl_pct", "exit_reason")
PRICE_TOL = 1e-6                     # same tolerance the parity test uses


def serialise(closed) -> list[dict]:
    """Exactly the projection tests/test_reference_parity.py::_run_optimized uses."""
    return [{
        "isin": t.get("isin"), "symbol": t.get("symbol"), "strategy": t.get("strategy"),
        "entry_date": str(t["entry_date"]), "exit_date": str(t["exit_date"]),
        "entry_price": round(float(t["entry_price"]), 6),
        "exit_price": round(float(t["exit_price"]), 6),
        "shares": int(t["shares"]),
        "position_value": round(float(t["entry_price"]) * int(t["shares"]), 6),
        "realised_pnl": round(float(t["realised_pnl"]), 6),
        "realised_pnl_pct": round(float(t["realised_pnl_pct"]), 6),
        "exit_reason": t["exit_reason"],
    } for t in closed]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True)
    ap.add_argument("--reuse", action="store_true",
                    help="diff against a previously saved optimized run")
    a = ap.parse_args()

    fx = json.loads((FIXTURE_DIR / f"{a.window}.json").read_text())
    md, c = fx["metadata"], fx["metadata"]["config"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = OUT_DIR / f"{a.window}_optimized.json"

    print(f"window {a.window}  {md['start_date']} -> {md['end_date']}", flush=True)
    print(f"oracle : {fx['portfolio']['n_trades']} trades, "
          f"final {fx['portfolio']['final_capital']}", flush=True)

    if a.reuse and saved.exists():
        got = json.loads(saved.read_text())
        print(f"reusing {saved}", flush=True)
    else:
        from backtest.backtest_engine import BacktestEngine
        t0 = time.time()
        e = BacktestEngine(
            start_date=date.fromisoformat(md["start_date"]),
            end_date=date.fromisoformat(md["end_date"]),
            starting_capital=c["starting_capital"],
            strategy=c["strategy"],
            cost_profile=c["cost_profile"],
            use_panel=True,
        )
        metrics = e.run()
        runtime = time.time() - t0
        got = {
            "runtime_s": round(runtime, 1),
            "portfolio": {"final_capital": round(e.capital, 6),
                          "n_trades": metrics.total_trades},
            "stats": dict(e._stats),
            "panel_stats": dict(getattr(e, "_panel_stats", {}) or {}),
            "trades": serialise(e._closed_trades),
        }
        saved.write_text(json.dumps(got, indent=1))
        print(f"optimized run: {runtime:.1f}s -> {saved}", flush=True)

    print("\n" + "=" * 78)
    print("PORTFOLIO")
    on, gn = fx["portfolio"]["n_trades"], got["portfolio"]["n_trades"]
    oc, gc = fx["portfolio"]["final_capital"], got["portfolio"]["final_capital"]
    print(f"  n_trades      oracle {on:>8}  optimized {gn:>8}   "
          f"{'MATCH' if on == gn else 'DIFFER'}")
    print(f"  final_capital oracle {oc:>14.6f}  optimized {gc:>14.6f}   "
          f"delta {gc - oc:+.6f}")

    print("\nCOUNTERS (oracle -> optimized)")
    diffs = 0
    for k, want in fx["stats"].items():
        g = got["stats"].get(k)
        if g != want:
            diffs += 1
            print(f"  DIFFER {k:<24} {want:>10} -> {g:>10}  ({g - want:+d})")
    if not diffs:
        print("  all counters identical")
    if got.get("panel_stats"):
        print(f"  panel gate stats: {json.dumps(got['panel_stats'], sort_keys=True)}")

    print("\nTRADES")
    want_tr, got_tr = fx["trades"], got["trades"]
    print(f"  count oracle {len(want_tr)}   optimized {len(got_tr)}")

    ident = lambda t: (t["isin"], t["entry_date"], t["exit_date"])
    wset, gset = {ident(t) for t in want_tr}, {ident(t) for t in got_tr}
    missing = [t for t in want_tr if ident(t) not in gset]
    extra = [t for t in got_tr if ident(t) not in wset]
    print(f"  in oracle but NOT in optimized : {len(missing)}")
    for t in missing[:15]:
        print(f"      {t['symbol']:<12} {t['isin']} {t['entry_date']} -> {t['exit_date']}"
              f"  pnl={t['realised_pnl']:>9.2f}  {t['exit_reason'][:32]}")
    print(f"  in optimized but NOT in oracle : {len(extra)}")
    for t in extra[:15]:
        print(f"      {t['symbol']:<12} {t['isin']} {t['entry_date']} -> {t['exit_date']}"
              f"  pnl={t['realised_pnl']:>9.2f}  {t['exit_reason'][:32]}")

    print("\nFIRST POSITIONAL FIELD DIVERGENCE (how the parity test compares)")
    first, ndiff = None, 0
    for i, (g, w) in enumerate(zip(got_tr, want_tr)):
        for f in TRADE_FIELDS:
            gv, wv = g.get(f), w.get(f)
            bad = (abs(gv - wv) >= PRICE_TOL) if isinstance(wv, float) else (gv != wv)
            if bad:
                ndiff += 1
                if first is None:
                    first = (i, f, wv, gv)
                break
    if first:
        i, f, wv, gv = first
        print(f"  index {i}, field {f}: oracle {wv!r} != optimized {gv!r}")
        print(f"  positions differing in >=1 field: {ndiff} of "
              f"{min(len(got_tr), len(want_tr))}")
        lo, hi = max(0, i - 2), min(max(len(want_tr), len(got_tr)), i + 4)
        print("\n  CONTEXT")
        for j in range(lo, hi):
            o = want_tr[j] if j < len(want_tr) else None
            p = got_tr[j] if j < len(got_tr) else None
            mk = ">>" if j == i else "  "
            print(f"  {mk} [{j}] oracle    " + (
                f"{o['symbol']:<12} {o['entry_date']} -> {o['exit_date']} "
                f"sh={o['shares']:<5} pnl={o['realised_pnl']:>9.2f} {o['exit_reason'][:30]}"
                if o else "<none>"))
            print(f"  {mk}      optimized " + (
                f"{p['symbol']:<12} {p['entry_date']} -> {p['exit_date']} "
                f"sh={p['shares']:<5} pnl={p['realised_pnl']:>9.2f} {p['exit_reason'][:30]}"
                if p else "<none>"))
        print(f"\n  earliest divergent entry_date: oracle {want_tr[i]['entry_date']} / "
              f"optimized {got_tr[i]['entry_date']}")
    else:
        print("  no positional field divergence in the overlapping prefix")

    # earliest point in TIME where the two ledgers disagree
    all_bad = sorted({t["entry_date"] for t in missing} | {t["entry_date"] for t in extra})
    if all_bad:
        print(f"\n  earliest disagreeing ENTRY date anywhere: {all_bad[0]}")
        print(f"  disagreeing entry dates (first 12): {all_bad[:12]}")
    print("\nDONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
