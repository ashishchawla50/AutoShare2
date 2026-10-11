"""Capture the reference engine's output ONCE and freeze it as a regression fixture.

The problem this solves: every parity check re-executed the slow reference engine, so
verifying the fast path cost a full slow run each time (a 1-month window exceeds 570 s
locally). That makes iterative development impractical.

    reference engine  --(run once, slowly)-->  frozen JSON fixture
                                                     |
    optimized engine  --(run fast)-------------> compare  (seconds)

The reference engine is NOT modified, cached, or vectorised — it stays an independent
correctness oracle. This tool only observes it.

Fixtures are immutable artifacts. Never hand-edit one to make a test pass; if
reference behaviour intentionally changes, capture a NEW version alongside the old.

Usage:
    python tools/capture_reference_oracle.py --window smoke
    python tools/capture_reference_oracle.py --window smoke,1month,1year
    python tools/capture_reference_oracle.py --verify tests/fixtures/oracle/smoke.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config

logger = logging.getLogger("tools.oracle")

FIXTURE_VERSION = "1"
FIXTURE_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "oracle"

# Progressive windows. Deliberately ordered cheapest-first so a long capture job can
# be stopped at any point and still leave usable fixtures behind.
WINDOWS: dict[str, tuple[str, str]] = {
    "smoke":    ("2023-01-02", "2023-03-31"),   # the known pre-Step-2 oracle window
    "1month":   ("2023-01-02", "2023-01-31"),
    "3month":   ("2023-04-03", "2023-06-30"),
    "1year":    ("2023-01-02", "2023-12-29"),
    "multiyear": ("2022-01-03", "2024-12-31"),
    "full":     ("2021-09-01", "2026-08-10"),
}

# Independently known values for the smoke window, captured BEFORE the Step-2
# data-access rewrite. Reproducing these proves the rewrite preserved semantics.
KNOWN_SMOKE = {
    "final_capital": 49255.590597,
    "n_trades": 3,
    "fail_tt": 19008,
    "fail_vcp": 5624,
}

STATS = ("data_ok", "analyse_called", "fail_tt", "fail_vcp", "fail_tightness",
         "fail_breakout_confirm", "fail_other", "pivot_watch_added",
         "pivot_watch_expired", "event_filtered", "high_conf", "gap_filtered",
         "size_invalid", "entered", "regime_blocked_days", "dd_halted_days")


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10,
                              cwd=Path(__file__).parent.parent).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def dataset_fingerprint() -> dict:
    """Reuse the Layer-3 fingerprint inputs rather than inventing a second scheme."""
    db = Path(config.DB_PATH) / config.RECONSTRUCTED_DB
    if not db.exists():
        return {"error": f"missing {db}"}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    q = lambda s: con.execute(s).fetchone()[0]
    parts = {
        "adjusted_rows": q("SELECT COUNT(*) FROM ohlcv_adjusted"),
        "max_trade_date": str(q("SELECT MAX(trade_date) FROM ohlcv_adjusted")),
        "isins": q("SELECT COUNT(*) FROM security_master"),
        "quarantine_rows": q("SELECT COUNT(*) FROM quarantine"),
        "applied_actions": q("SELECT COUNT(*) FROM corporate_actions WHERE resolution='applied'"),
        "universe_months": q("SELECT COUNT(DISTINCT effective_from) FROM universe_liquid500"),
    }
    con.close()
    parts["sha256"] = hashlib.sha256(
        "|".join(f"{k}={v}" for k, v in sorted(parts.items())).encode()).hexdigest()[:16]
    return parts


def config_fingerprint(capital: float, strategy: str, cost_profile: str) -> dict:
    """Every knob that can move a trade. Frozen with the fixture."""
    from research.indicator_panel import PANEL_VERSION
    parts = {
        "strategy": strategy,
        "starting_capital": capital,
        "cost_profile": cost_profile,
        "data_source": config.BACKTEST_DATA_SOURCE,
        "panel_version": PANEL_VERSION,
        "risk_per_trade_pct": config.RISK_PER_TRADE_PCT,
        "max_position_size_pct": config.MAX_POSITION_SIZE_PCT,
        "max_positions": config.MAX_POSITIONS,
        "min_cash_reserve_pct": config.MIN_CASH_RESERVE_PCT,
        "min_position_value": config.MIN_POSITION_VALUE,
        "hard_stop_pct": config.HARD_STOP_PCT,
        "time_stop_days": config.TIME_STOP_DAYS,
        "vcp2_pivot_watch_days": config.VCP2_PIVOT_WATCH_DAYS,
        "max_gap_up_pct": config.MAX_GAP_UP_PCT,
        "enable_partial_profit": config.ENABLE_PARTIAL_PROFIT,
    }
    parts["sha256"] = hashlib.sha256(
        "|".join(f"{k}={v}" for k, v in sorted(parts.items())).encode()).hexdigest()[:16]
    return parts


def capture(window: str, capital: float = 50000.0, strategy: str = "vcp_v2",
            cost_profile: str = "cnc") -> dict:
    """Run the REFERENCE engine (use_panel=False) and serialise its outputs."""
    from backtest.backtest_engine import BacktestEngine

    start_s, end_s = WINDOWS[window]
    start, end = date.fromisoformat(start_s), date.fromisoformat(end_s)
    logger.warning("capturing oracle '%s' %s -> %s (reference engine)", window, start, end)

    t0 = time.time()
    engine = BacktestEngine(start_date=start, end_date=end, starting_capital=capital,
                            strategy=strategy, cost_profile=cost_profile,
                            use_panel=False)          # <- the oracle path
    metrics = engine.run()
    runtime = time.time() - t0

    total_pnl = sum(t["realised_pnl"] for t in engine._closed_trades)
    trades = [{
        "isin": t.get("isin"),
        "symbol": t.get("symbol"),
        "strategy": t.get("strategy"),
        "entry_date": str(t["entry_date"]),
        "exit_date": str(t["exit_date"]),
        "entry_price": round(float(t["entry_price"]), 6),
        "exit_price": round(float(t["exit_price"]), 6),
        "shares": int(t["shares"]),
        "position_value": round(float(t["entry_price"]) * int(t["shares"]), 6),
        "realised_pnl": round(float(t["realised_pnl"]), 6),
        "realised_pnl_pct": round(float(t["realised_pnl_pct"]), 6),
        "exit_reason": t["exit_reason"],
    } for t in engine._closed_trades]           # ordering preserved deliberately

    return {
        "metadata": {
            "fixture_version": FIXTURE_VERSION,
            "window": window,
            "start_date": start_s,
            "end_date": end_s,
            "git_commit": _git_commit(),
            "created_utc": datetime.utcnow().isoformat(timespec="seconds"),
            "engine": "reference (use_panel=False)",
            "reference_runtime_s": round(runtime, 1),
            "dataset": dataset_fingerprint(),
            "config": config_fingerprint(capital, strategy, cost_profile),
        },
        "portfolio": {
            "starting_capital": capital,
            "final_capital": round(engine.capital, 6),
            "n_trades": metrics.total_trades,
            "sum_realised_pnl": round(total_pnl, 6),
            "ledger_error": round(abs(total_pnl - (engine.capital - capital)), 6),
            "open_positions_at_end": len(engine._open_positions),
        },
        "stats": {k: engine._stats[k] for k in STATS if k in engine._stats},
        "trades": trades,
    }


def canonical_hash(fx: dict) -> str:
    """Hash of the RESULT content only — metadata (timestamps, commit) excluded so
    the same behaviour hashes identically across captures."""
    payload = {"portfolio": fx["portfolio"], "stats": fx["stats"], "trades": fx["trades"]}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_known(fx: dict) -> list[str]:
    """The smoke window has independently known expected values. Check, never edit."""
    if fx["metadata"]["window"] != "smoke":
        return []
    problems = []
    if abs(fx["portfolio"]["final_capital"] - KNOWN_SMOKE["final_capital"]) > 1e-6:
        problems.append(f"final_capital {fx['portfolio']['final_capital']} "
                        f"!= {KNOWN_SMOKE['final_capital']}")
    if fx["portfolio"]["n_trades"] != KNOWN_SMOKE["n_trades"]:
        problems.append(f"n_trades {fx['portfolio']['n_trades']} != {KNOWN_SMOKE['n_trades']}")
    for k in ("fail_tt", "fail_vcp"):
        if fx["stats"].get(k) != KNOWN_SMOKE[k]:
            problems.append(f"{k} {fx['stats'].get(k)} != {KNOWN_SMOKE[k]}")
    return problems


def write_fixture(fx: dict, out_dir: Path = FIXTURE_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fx["content_sha256"] = canonical_hash(fx)
    path = out_dir / f"{fx['metadata']['window']}.json"
    if path.exists():
        # Never silently overwrite a frozen artifact.
        prev = json.loads(path.read_text())
        if prev.get("content_sha256") != fx["content_sha256"]:
            arch = out_dir / f"{fx['metadata']['window']}.superseded-{int(time.time())}.json"
            path.rename(arch)
            logger.warning("existing fixture differed — archived to %s", arch.name)
    path.write_text(json.dumps(fx, indent=1))
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser(description="Capture / verify the reference oracle")
    ap.add_argument("--window", default="smoke",
                    help="comma-separated: " + ",".join(WINDOWS))
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--strategy", default="vcp_v2")
    ap.add_argument("--cost-profile", default="cnc")
    ap.add_argument("--verify", default=None, help="verify an existing fixture file")
    a = ap.parse_args()

    if a.verify:
        fx = json.loads(Path(a.verify).read_text())
        stored, actual = fx.get("content_sha256"), canonical_hash(fx)
        print(f"window        : {fx['metadata']['window']}")
        print(f"content hash  : {'OK' if stored == actual else 'MISMATCH'} ({actual[:16]})")
        print(f"final capital : {fx['portfolio']['final_capital']}")
        print(f"trades        : {fx['portfolio']['n_trades']}")
        probs = verify_known(fx)
        print(f"known values  : {'OK' if not probs else probs}")
        sys.exit(0 if stored == actual and not probs else 1)

    for w in [x.strip() for x in a.window.split(",") if x.strip()]:
        if w not in WINDOWS:
            print(f"unknown window {w!r}; known: {list(WINDOWS)}"); continue
        fx = capture(w, a.capital, a.strategy, a.cost_profile)
        probs = verify_known(fx)
        p = write_fixture(fx)
        print(f"\n=== {w} ===", flush=True)
        print(f"  runtime      : {fx['metadata']['reference_runtime_s']}s")
        print(f"  final capital: {fx['portfolio']['final_capital']}")
        print(f"  trades       : {fx['portfolio']['n_trades']}")
        print(f"  ledger error : {fx['portfolio']['ledger_error']}")
        print(f"  fail_tt      : {fx['stats'].get('fail_tt')}")
        print(f"  fail_vcp     : {fx['stats'].get('fail_vcp')}")
        print(f"  known-value check: {'PASS' if not probs else 'FAIL ' + str(probs)}")
        print(f"  written      : {p}", flush=True)
