#!/usr/bin/env python3
"""Run the pre-registered October 2026 test plan (docs/TEST_PLAN_2026_10.md).

    python tools/run_test_plan_2026_10.py groupA        # A1-A3
    python tools/run_test_plan_2026_10.py groupC        # C1-C4 (needs groupA's stop)
    python tools/run_test_plan_2026_10.py b1            # B1 exploration, DEV only
    python tools/run_test_plan_2026_10.py b3            # B3a-d
    python tools/run_test_plan_2026_10.py b2            # B2 (after Part 2 is committed)

Results print to stdout and are saved as JSON under data/test_plan_2026_10/.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import warnings
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)

import numpy as np
import pandas as pd

from backtest.fast.data import Universe, load_market_data
from backtest.fast.engine import FastBacktestEngine
from backtest.fast.features import FeatureBook, rs_filter
from backtest.fast.signals import load_signals
from config.config import config

DEV = (date(2021, 9, 1), date(2024, 12, 31))
OOS = (date(2025, 1, 1), date(2026, 6, 30))
FULL = (date(2021, 9, 1), date(2026, 6, 30))
OUT = Path(config.DB_PATH) / "test_plan_2026_10"
OUT.mkdir(parents=True, exist_ok=True)

MD = load_market_data()
UNI = Universe()
_SIG: dict = {}


def signals():
    L = config.VCP_LOOKBACK_DAYS
    if L not in _SIG:
        _SIG[L] = load_signals(MD)
    return _SIG[L]


@contextlib.contextmanager
def cfg(**overrides):
    old = {k: getattr(config, k) for k in overrides}
    for k, v in overrides.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def run(window, **opts):
    a, b = window
    e = FastBacktestEngine(start_date=a, end_date=b, starting_capital=50_000.0, cost_profile="cnc",
                           dd_halt=False, market=MD, signals=signals(), universe=UNI,
                           verbose=False, **opts)
    m = e.run()
    return m, e


def summ(m) -> dict:
    return {"trades": m.total_trades, "pf": round(m.profit_factor, 3),
            "return_pct": round(m.total_return_pct, 2), "cagr_pct": round(m.cagr_pct, 2),
            "max_dd_pct": round(m.max_drawdown_pct, 2), "win_pct": round(m.win_rate_pct, 1)}


def line(name, d, o=None):
    s = (f"  {name:34s} DEV trades {d['trades']:3d} PF {d['pf']:5.2f} ret {d['return_pct']:+6.1f}% "
         f"DD {d['max_dd_pct']:+6.1f}%")
    if o:
        s += f" | OOS trades {o['trades']:3d} PF {o['pf']:5.2f} ret {o['return_pct']:+6.1f}% DD {o['max_dd_pct']:+6.1f}%"
    print(s)


def pair(**opts):
    return summ(run(DEV, **opts)[0]), summ(run(OOS, **opts)[0])


def rule_r(cand, base) -> tuple[bool, str]:
    (cd, co), (bd, bo) = cand, base
    checks = {
        "PF higher both": cd["pf"] > bd["pf"] and co["pf"] > bo["pf"],
        "return higher both": cd["return_pct"] > bd["return_pct"] and co["return_pct"] > bo["return_pct"],
        "DEV DD above -20%": cd["max_dd_pct"] > -20.0,
    }
    return all(checks.values()), ", ".join(f"{k}: {'yes' if v else 'NO'}" for k, v in checks.items())


def save(name, obj):
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))


def chosen_stop() -> float:
    p = OUT / "groupA.json"
    if not p.exists():
        sys.exit("run groupA first — Groups B/C use A1's chosen stop")
    return json.loads(p.read_text())["chosen_stop"]


# ── Group A ──────────────────────────────────────────────────────────────────

def group_a():
    res = {}
    print("A1 — first stop -10% vs -7% (B0)")
    with cfg(HARD_STOP_PCT=0.07):
        b0 = pair()
    with cfg(HARD_STOP_PCT=0.10):
        a1 = pair()
    line("B0 (-7%)", *b0); line("A1 (-10%)", *a1)
    ok, why = rule_r(a1, b0)
    stop = 0.10 if ok else 0.07
    print(f"  -> A1 {'PASSES' if ok else 'FAILS'} rule R ({why}); Groups B/C use {stop:.0%}")
    res["A1"] = {"base": b0, "cand": a1, "pass": ok, "why": why}
    res["chosen_stop"] = stop

    with cfg(HARD_STOP_PCT=stop):
        base = pair()
        print(f"\nA2 — RS >= 70 filter (base: stop {stop:.0%})")
        a2 = pair(entry_filter=rs_filter(70.0))
        line("base", *base); line("A2 RS>=70", *a2)
        ok2, why2 = rule_r(a2, base)
        print(f"  -> A2 {'PASSES' if ok2 else 'FAILS'} ({why2})")
        print(f"\nA3 — live partial profits 33% @+20%, 33% @+30% (base: stop {stop:.0%})")
        a3 = pair(exit_mode="vcp_partials")
        line("base", *base); line("A3 partials", *a3)
        ok3, why3 = rule_r(a3, base)
        print(f"  -> A3 {'PASSES' if ok3 else 'FAILS'} ({why3})")
    res["base_for_A2_A3"] = base
    res["A2"] = {"cand": a2, "pass": ok2, "why": why2}
    res["A3"] = {"cand": a3, "pass": ok3, "why": why3}
    save("groupA", res)


# ── Group C ──────────────────────────────────────────────────────────────────

def group_c():
    stop = chosen_stop()
    res = {"stop": stop}
    with cfg(HARD_STOP_PCT=stop):
        m, e = run(FULL)
    base_pf = m.profit_factor
    trades = pd.DataFrame(e._closed_trades)
    trades["year"] = pd.to_datetime(trades["entry_date"]).dt.year
    print(f"C1 — year by year (full window, stop {stop:.0%}): overall PF {base_pf:.2f}, "
          f"{m.total_trades} trades, return {m.total_return_pct:+.1f}%")
    years = {}
    for y, t in trades.groupby("year"):
        w, l = t.realised_pnl[t.realised_pnl > 0].sum(), -t.realised_pnl[t.realised_pnl < 0].sum()
        pf = w / l if l > 0 else float("inf")
        years[int(y)] = {"trades": len(t), "pf": round(pf, 2), "pnl": round(t.realised_pnl.sum())}
        print(f"  {y}: {len(t):3d} trades  PF {pf:5.2f}  P&L Rs {t.realised_pnl.sum():+9,.0f}")
    profitable = sum(v["pf"] > 1 for v in years.values())
    print(f"  -> profitable years: {profitable} of {len(years)} (healthy if >= 3)")
    res["C1"] = {"years": years, "healthy": profitable >= 3, "full": summ(m)}

    print("\nC2 — sensitivity, each setting x0.8 / x1.2 (full window PF)")
    nudges = {
        "MIN_BREAKOUT_VOLUME_MULTIPLIER": (1.6, 2.4),
        # a ratio threshold: nudge its distance from 1 (0.04 -> 0.032 / 0.048)
        "STRONG_CLOSE_THRESHOLD": (1 - 0.04 * 0.8, 1 - 0.04 * 1.2),
        "MAX_ENTRY_EXTENSION_PCT": (0.04, 0.06),
        "VCP2_PIVOT_WATCH_DAYS": (24, 36),
        "TIME_STOP_DAYS": (12, 18),
        "TIME_STOP_MIN_PROFIT_PCT": (0.056, 0.084),
        "HARD_STOP_PCT": (round(stop * 0.8, 4), round(stop * 1.2, 4)),
        "VCP_LOOKBACK_DAYS": (48, 72),
    }
    c2 = {}
    for k, vals in nudges.items():
        for v in vals:
            over = {"HARD_STOP_PCT": stop, k: v}
            with cfg(**over):
                pf = run(FULL)[0].profit_factor
            c2[f"{k}={v}"] = round(pf, 3)
            print(f"  {k:32s} = {v:<7} PF {pf:5.2f}  ({pf - base_pf:+.2f})")
    pfs = list(c2.values())
    healthy = min(pfs) >= 1.0 and abs(float(np.median(pfs)) - base_pf) <= 0.2
    print(f"  -> min PF {min(pfs):.2f}, median {np.median(pfs):.2f} vs base {base_pf:.2f}: "
          f"{'healthy' if healthy else 'NOT healthy'}")
    res["C2"] = {"base_pf": round(base_pf, 3), "runs": c2, "healthy": healthy}

    print("\nC3 — slippage per side (full window)")
    c3 = {}
    with cfg(HARD_STOP_PCT=stop):
        for s in (0.001, 0.0025, 0.005):
            mm = run(FULL, slippage=s)[0]
            c3[s] = summ(mm)
            print(f"  {s:.2%} per side: PF {mm.profit_factor:5.2f}  return {mm.total_return_pct:+6.1f}%")
    res["C3"] = {"runs": c3, "healthy": c3[0.0025]["pf"] >= 1.0}
    print(f"  -> {'healthy' if res['C3']['healthy'] else 'NOT healthy'} at 0.25%")

    print("\nC4 — Monte Carlo drawdown (10,000 reshuffles of the full-window trade P&L)")
    pnl = trades.realised_pnl.to_numpy()
    rng = np.random.default_rng(42)
    dds = np.empty(10_000)
    for i in range(10_000):
        eq = 50_000 + np.cumsum(rng.permutation(pnl))
        peak = np.maximum.accumulate(np.concatenate([[50_000], eq]))[1:]
        dds[i] = ((eq - peak) / peak).min()
    p20 = float(np.mean(dds <= -0.20))
    print(f"  median worst DD {np.median(dds):+.1%}, 95th pct {np.percentile(dds, 5):+.1%}, "
          f"P(DD >= 20%) = {p20:.1%} -> {'healthy' if p20 < 0.10 else 'NOT healthy'}")
    res["C4"] = {"median_dd": float(np.median(dds)), "dd_95": float(np.percentile(dds, 5)),
                 "p_dd20": p20, "healthy": p20 < 0.10}
    save("groupC", res)


# ── Group B ──────────────────────────────────────────────────────────────────

def b1():
    """Every DEV breakout (book never fills) with entry-day features and its outcome under
    the A1-chosen current exits, simulated per signal (same model as WINNING_STRATEGY.md)."""
    stop = chosen_stop()
    rows, book = [], {}

    def recorder(engine, slot, p_row, ti, pend):
        if "fb" not in book:
            book["fb"] = FeatureBook(engine)
        f = book["fb"].features(slot, p_row, ti, pend)
        f.update(entry_row=p_row + 1, end_row=int(engine._hi[slot]), date=str(engine._sim_date))
        rows.append(f)
        return False

    with cfg(HARD_STOP_PCT=stop):
        run(DEV, entry_filter=recorder)
    sig = signals()
    for f in rows:
        f["ret"] = _current_exit(f["entry_row"], f["end_row"], stop, sig) - 0.004  # ~CNC costs
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "b1_dev_signals.csv", index=False)
    print(f"B1 — {len(df)} DEV breakout signals, overall avg {df.ret.mean():+.2%}, "
          f"PF {_pf(df.ret):.2f}\n")
    feats = ["rs_pct", "vol_mult", "close_vs_pivot", "price", "nifty_vs_ema50", "breadth", "gap",
             "atr_pct", "from_52w_high", "close_vs_ema50", "watch_days", "rel_63"]
    out = {}
    for c in feats:
        q = pd.qcut(df[c].rank(method="first"), 5, labels=False)
        g = df.groupby(q)
        tab = [{"lo": float(df[c][q == i].min()), "hi": float(df[c][q == i].max()),
                "n": int((q == i).sum()), "avg": float(df.ret[q == i].mean()),
                "pf": float(_pf(df.ret[q == i]))} for i in range(5)]
        out[c] = tab
        print(f"{c:15s} " + " | ".join(f"Q{i+1} [{t['lo']:.3g}..{t['hi']:.3g}] PF {t['pf']:4.2f} avg {t['avg']:+.1%}"
                                         for i, t in enumerate(tab)))
    by_reg = {r: {"n": int(len(t)), "pf": float(_pf(t.ret)), "avg": float(t.ret.mean())}
              for r, t in df.groupby("regime")}
    print("regime          " + " | ".join(f"{r}: n {v['n']} PF {v['pf']:.2f} avg {v['avg']:+.1%}"
                                         for r, v in by_reg.items()))
    out["regime"] = by_reg
    save("b1", out)


def _pf(r) -> float:
    r = np.asarray(r)
    l = -r[r < 0].sum()
    return r[r > 0].sum() / l if l > 0 else float("inf")


def _current_exit(r0, end, sl, sig) -> float:
    """Per-signal model of the current exits (WINNING_STRATEGY.md): stop, ratchet table,
    10-day EMA x 0.97 floor, 15-day / +7% time stop. No breakdown exits."""
    o_, l_, c_ = MD.open, MD.low, MD.close
    e = o_[r0]; st = e * (1 - sl)
    for i in range(r0, min(end, r0 + 400)):
        if l_[i] <= st:
            return (min(st, o_[i]) if i > r0 else st) / e - 1
        pr = c_[i] / e - 1
        for p, t in ((0.30, .20), (0.20, .10), (0.15, .05), (0.10, 0.0)):
            if pr >= p:
                st = max(st, e * (1 + t)); break
        st = max(st, sig.ema10[i] * 0.97)
        if i - r0 >= 15 and pr < 0.07:
            return pr
    return c_[min(end, r0 + 400) - 1] / e - 1


def b3():
    stop = chosen_stop()
    res = {}
    with cfg(HARD_STOP_PCT=stop):
        base = pair()
        line("base", *base)
        cands = {"B3a pivot watch 45d": ({"VCP2_PIVOT_WATCH_DAYS": 45}, {}),
                 "B3b pivot watch 20d": ({"VCP2_PIVOT_WATCH_DAYS": 20}, {}),
                 "B3c volume 1.5x": ({"MIN_BREAKOUT_VOLUME_MULTIPLIER": 1.5}, {}),
                 "B3d CAUTIOUS full size": ({}, {"caution_full_size": True})}
        for name, (over, opts) in cands.items():
            with cfg(**over):
                c = pair(**opts)
            ok, why = rule_r(c, base)
            line(name, *c)
            print(f"    -> {'PASSES' if ok else 'fails'} ({why})")
            res[name] = {"cand": c, "pass": ok, "why": why}
    res["base"] = base
    save("b3", res)


def b2():
    """Filters pre-registered in docs/TEST_PLAN_2026_10.md Part 2 (filled in after B1)."""
    from tools.b2_filters import FILTERS          # written with Part 2, committed before running
    stop = chosen_stop()
    res = {}
    with cfg(HARD_STOP_PCT=stop):
        base = pair()
        line("base", *base)
        for name, flt in FILTERS.items():
            c = pair(entry_filter=flt())
            ok, why = rule_r(c, base)
            line(name, *c)
            print(f"    -> {'PASSES' if ok else 'fails'} ({why})")
            res[name] = {"cand": c, "pass": ok, "why": why}
    res["base"] = base
    save("b2", res)


if __name__ == "__main__":
    {"groupA": group_a, "groupC": group_c, "b1": b1, "b3": b3, "b2": b2}[sys.argv[1]]()
