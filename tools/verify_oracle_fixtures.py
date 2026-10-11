"""Independent integrity audit of the frozen oracle fixtures.

Read-only. Does NOT run either engine — it inspects the frozen JSON artifacts and
cross-checks them against the live research dataset. This is deliberately a second
implementation of the checks in `tests/test_reference_parity.py`: if the fixture
format and the test that guards it were written by the same code path, a bug in
that path would hide itself.

What it proves for every fixture:
  * content checksum recomputes (nobody hand-edited the artifact)
  * dataset + config fingerprints exist and still match this machine's dataset
  * the file is complete: metadata, portfolio, all 16 counters, every trade field
  * ledger self-consistency: sum(trade pnl) == final - starting, ledger_error ~ 0
  * no duplicate trades, no reordering (exit dates non-decreasing)
  * per-trade sanity: inside the window, exit >= entry, value == price x shares,
    realised pnl never exceeds gross (costs can only reduce)

Usage:
    python tools/verify_oracle_fixtures.py
    python tools/verify_oracle_fixtures.py --window full

Exit code 0 = every fixture clean; 1 = at least one problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

from config.config import config
from tools.capture_reference_oracle import (STATS, WINDOWS, canonical_hash,
                                            config_fingerprint, dataset_fingerprint)

FIXTURE_DIR = REPO / "tests" / "fixtures" / "oracle"

TRADE_FIELDS = ("isin", "symbol", "strategy", "entry_date", "exit_date",
                "entry_price", "exit_price", "shares", "position_value",
                "realised_pnl", "realised_pnl_pct", "exit_reason")

# Per-trade values are stored rounded to 6dp, so summing N of them drifts from the
# separately-rounded total by up to N*5e-7. Anything beyond that is a real defect.
def _sum_tolerance(n: int) -> float:
    return max(1e-6, n * 5e-7 * 2)


def audit(path: Path, live_ds: dict, live_cfg: dict, con: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    ok = lambda c: "OK  " if c else "FAIL"
    say = lambda label, cond, extra="": print(f"  {label:<22}: {ok(cond)} {extra}")

    try:
        fx = json.loads(path.read_text())
    except Exception as e:
        print(f"  UNPARSEABLE / TRUNCATED: {e}")
        return [f"{path.name}: unparseable"]

    md = fx.get("metadata", {})

    actual = canonical_hash(fx)
    c_ok = actual == fx.get("content_sha256")
    say("content_sha256", c_ok, actual[:24])
    if not c_ok:
        problems.append(f"{path.name}: content hash mismatch (fixture was edited)")

    top_missing = [k for k in ("metadata", "portfolio", "stats", "trades", "content_sha256")
                   if k not in fx]
    md_missing = [k for k in ("fixture_version", "window", "start_date", "end_date",
                              "git_commit", "created_utc", "engine", "reference_runtime_s",
                              "dataset", "config") if k not in md]
    say("required keys", not (top_missing or md_missing), f"{top_missing}{md_missing}"
        if (top_missing or md_missing) else "")
    if top_missing or md_missing:
        problems.append(f"{path.name}: missing keys {top_missing}{md_missing}")

    ds_ok = md.get("dataset", {}).get("sha256") == live_ds["sha256"]
    cf_ok = md.get("config", {}).get("sha256") == live_cfg["sha256"]
    say("dataset fingerprint", ds_ok, f"{md.get('dataset', {}).get('sha256')} "
        f"(rows={md.get('dataset', {}).get('adjusted_rows')})")
    say("config fingerprint", cf_ok, f"{md.get('config', {}).get('sha256')} "
        f"panel_v={md.get('config', {}).get('panel_version')}")
    if not ds_ok:
        problems.append(f"{path.name}: captured against a different dataset")
    if not cf_ok:
        problems.append(f"{path.name}: captured under a different configuration")

    w = md.get("window")
    w_ok = WINDOWS.get(w) == (md.get("start_date"), md.get("end_date")) and path.stem == w
    say("window consistency", w_ok, f"{w} {md.get('start_date')} -> {md.get('end_date')}")
    if not w_ok:
        problems.append(f"{path.name}: window metadata inconsistent")

    eng_ok = "reference" in md.get("engine", "") and "use_panel=False" in md.get("engine", "")
    say("engine = reference", eng_ok, repr(md.get("engine")))
    if not eng_ok:
        problems.append(f"{path.name}: not captured by the reference engine")

    rt = md.get("reference_runtime_s")
    say("reference runtime", isinstance(rt, (int, float)) and rt > 0,
        f"{rt}s (commit {md.get('git_commit')}, {md.get('created_utc')}Z)")

    td, sd = con.execute(
        "SELECT COUNT(DISTINCT trade_date), COUNT(*) FROM ohlcv_adjusted "
        "WHERE trade_date BETWEEN ? AND ?", (md["start_date"], md["end_date"])).fetchone()
    print(f"  {'trading days':<22}: {td}")
    print(f"  {'symbol-days':<22}: {sd}")

    pf, tr, st = fx.get("portfolio", {}), fx.get("trades", []), fx.get("stats", {})

    pf_missing = [k for k in ("starting_capital", "final_capital", "n_trades",
                              "sum_realised_pnl", "ledger_error", "open_positions_at_end")
                  if k not in pf]
    say("portfolio block", not pf_missing,
        f"start={pf.get('starting_capital')} final={pf.get('final_capital')} "
        f"n={pf.get('n_trades')} err={pf.get('ledger_error')}")
    if pf_missing:
        problems.append(f"{path.name}: portfolio keys missing {pf_missing}")

    delta = pf["final_capital"] - pf["starting_capital"]
    led_ok = (abs(pf["ledger_error"] - abs(pf["sum_realised_pnl"] - delta)) < 1e-6
              and pf["ledger_error"] < 5.0)
    say("ledger consistent", led_ok, f"delta={delta:.6f} pnl={pf['sum_realised_pnl']}")
    if not led_ok:
        problems.append(f"{path.name}: ledger inconsistent")

    tsum = sum(t["realised_pnl"] for t in tr)
    tol = _sum_tolerance(len(tr))
    tsum_ok = abs(tsum - pf["sum_realised_pnl"]) <= tol
    say("trades sum == pnl", tsum_ok, f"{tsum:.6f} (tol {tol:.1e})")
    if not tsum_ok:
        problems.append(f"{path.name}: trade pnl sum != portfolio pnl")

    miss_st = [k for k in STATS if k not in st]
    say("rejection counters", not miss_st,
        f"{len(st)}/{len(STATS)}" + (f" missing={miss_st}" if miss_st else ""))
    if miss_st:
        problems.append(f"{path.name}: missing counters {miss_st}")
    print(f"       {json.dumps(st, sort_keys=True)}")

    ent_ok = st.get("entered", 0) >= len(tr)
    say("entered >= closed", ent_ok, f"{st.get('entered')} >= {len(tr)}")
    if not ent_ok:
        problems.append(f"{path.name}: entered < closed trades")

    cnt_ok = len(tr) == pf["n_trades"]
    say("trade count", cnt_ok, f"{len(tr)} (portfolio says {pf['n_trades']})")
    if not cnt_ok:
        problems.append(f"{path.name}: trade count != n_trades")

    fmiss = {i: [f for f in TRADE_FIELDS if t.get(f) is None] for i, t in enumerate(tr)}
    fmiss = {i: v for i, v in fmiss.items() if v}
    say("all trade fields", not fmiss, str(fmiss)[:120] if fmiss else "")
    if fmiss:
        problems.append(f"{path.name}: trade fields missing")

    keys = [tuple(t.get(f) for f in TRADE_FIELDS) for t in tr]
    dupes = [k for k, c in Counter(keys).items() if c > 1]
    say("duplicate trades", not dupes, f"{len(dupes)} dupes")
    if dupes:
        problems.append(f"{path.name}: {len(dupes)} duplicate trades")

    ex = [t["exit_date"] for t in tr]
    order_ok = all(ex[i] <= ex[i + 1] for i in range(len(ex) - 1))
    say("ordering (exit_date)", order_ok, "non-decreasing" if order_ok else "OUT OF ORDER")
    if not order_ok:
        problems.append(f"{path.name}: trades out of exit order")

    bad = []
    for i, t in enumerate(tr):
        if not (md["start_date"] <= t["entry_date"] <= md["end_date"]):
            bad.append((i, "entry outside window"))
        if not (md["start_date"] <= t["exit_date"] <= md["end_date"]):
            bad.append((i, "exit outside window"))
        if t["entry_date"] > t["exit_date"]:
            bad.append((i, "exit before entry"))
        if t["shares"] <= 0:
            bad.append((i, "non-positive shares"))
        if abs(t["position_value"] - t["entry_price"] * t["shares"]) > 1e-4:
            bad.append((i, "position_value != price*shares"))
        if t["realised_pnl"] > (t["exit_price"] - t["entry_price"]) * t["shares"] + 1e-6:
            bad.append((i, "pnl exceeds gross"))
    say("per-trade sanity", not bad, str(bad[:5]) if bad else "")
    if bad:
        problems.append(f"{path.name}: {len(bad)} per-trade anomalies")

    if tr:
        print(f"  {'first trade':<22}: {tr[0]['symbol']} {tr[0]['entry_date']}"
              f"->{tr[0]['exit_date']} pnl={tr[0]['realised_pnl']}")
        print(f"  {'last trade':<22}: {tr[-1]['symbol']} {tr[-1]['entry_date']}"
              f"->{tr[-1]['exit_date']} pnl={tr[-1]['realised_pnl']}")
        print(f"  {'order digest':<22}: "
              f"{hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit the frozen oracle fixtures")
    ap.add_argument("--window", default=None, help="audit one window instead of all")
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--strategy", default="vcp_v2")
    ap.add_argument("--cost-profile", default="cnc")
    a = ap.parse_args()

    live_ds = dataset_fingerprint()
    if "error" in live_ds:
        print(f"cannot audit: {live_ds['error']}")
        return 1
    live_cfg = config_fingerprint(a.capital, a.strategy, a.cost_profile)
    print(f"live dataset fingerprint : {live_ds['sha256']}  "
          f"({live_ds['adjusted_rows']} rows, {live_ds['isins']} isins, "
          f"max {live_ds['max_trade_date']})")
    print(f"live config  fingerprint : {live_cfg['sha256']}")

    db = Path(config.DB_PATH) / config.RECONSTRUCTED_DB
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

    paths = sorted(p for p in FIXTURE_DIR.glob("*.json") if "superseded" not in p.name)
    if a.window:
        paths = [p for p in paths if p.stem == a.window]
        if not paths:
            print(f"no fixture for window {a.window!r}")
            return 1

    problems: list[str] = []
    for p in paths:
        print("\n" + "=" * 78)
        print(p.name)
        problems += audit(p, live_ds, live_cfg, con)
    con.close()

    print("\n" + "=" * 78)
    missing = [w for w in WINDOWS if not (FIXTURE_DIR / f"{w}.json").exists()]
    stray = [q.name for q in FIXTURE_DIR.iterdir() if q.suffix != ".json"]
    print(f"captured windows  : {[p.stem for p in paths]}")
    print(f"missing windows   : {missing or 'none'}")
    print(f"partial artifacts : {stray or 'none'}")
    print(f"superseded        : {[q.name for q in FIXTURE_DIR.glob('*superseded*')] or 'none'}")
    print(f"\nRESULT: {'ALL CLEAN' if not problems else str(len(problems)) + ' PROBLEM(S)'}")
    for p in problems:
        print(f"  - {p}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
