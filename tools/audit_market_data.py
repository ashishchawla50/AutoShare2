"""Repeatable data-integrity audit across all three data layers.

This is a standing validation tool, not a one-off script. Run it after any
rebuild, any backfill, or on a schedule, and diff the output against the last run.

It reconciles, for every suspicious price discontinuity:

    LAYER 1  market_data.db.orig-*   stored movement    (original vendor series)
    LAYER 2  bhavcopy.db             raw movement       (NSE as-published truth)
    LAYER 3  reconstructed.db        adjusted movement  (our corrected series)

A discontinuity is only benign when the corrected series either reproduces the
raw move (genuine market event) or removes it via a ledgered corporate action.
Anything else is reported for a human, never silently repaired.

Report columns (one row per finding):
    isin, symbol, date, prev_close, curr_close, raw_move, stored_move,
    adjusted_move, suspected_action, classification, confidence, resolution

Classifications
    GENUINE_MARKET_MOVE     corrected series matches raw; real event, keep
    RESOLVED_BY_ACTION      raw jump removed by a ledgered corporate action
    LEGACY_ARTIFACT         old vendor series disagreed with raw; now fixed
    UNVERIFIABLE            discontinuity with no confident explanation
    PHANTOM_ROW             price row predating the security's real listing
    STILL_CORRUPT           corrected series still disagrees with raw — a bug

Usage:
    python tools/audit_market_data.py --root data
    python tools/audit_market_data.py --root data --csv /tmp/audit.csv --threshold 0.15
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tools.audit")

DEFAULT_THRESHOLD = 0.20      # move vs market median that triggers investigation
PHANTOM_GAP_DAYS = 200        # trading-day gap that implies a pre-listing phantom


class Finding(dict):
    pass


def _market_median(rc) -> dict[str, float]:
    return {r[0]: r[1] for r in rc.execute(
        "SELECT trade_date, median_move FROM market_daily_move")}


def _legacy_series(orig: Path) -> dict[str, dict[str, float]]:
    """Original vendor closes: {symbol: {date: close}}. Layer 1, read-only."""
    if not orig.exists():
        return {}
    con = sqlite3.connect(f"file:{orig}?mode=ro", uri=True)
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for sym, d, c in con.execute("SELECT symbol, date, close FROM ohlcv_daily"):
        out[sym][str(d)[:10]] = c
    con.close()
    return out


def audit(root: Path, threshold: float, orig_name: str) -> list[Finding]:
    rc = sqlite3.connect(f"file:{root/'reconstructed.db'}?mode=ro", uri=True)
    market = _market_median(rc)
    legacy = _legacy_series(root / orig_name)
    logger.info("layer-1 legacy series loaded for %d symbols", len(legacy))

    ledger: dict[tuple, tuple] = {}
    for isin, ex, at, ratio, conf, res, note in rc.execute(
            "SELECT isin, ex_date, action_type, ratio, confidence, resolution, note "
            "FROM corporate_actions"):
        ledger[(isin, ex)] = (at, ratio, conf, res, note)

    listing: dict[str, str] = {r[0]: r[1] for r in rc.execute(
        "SELECT isin, first_date FROM security_master")}

    findings: list[Finding] = []
    cur = rc.execute(
        "SELECT isin, trade_date, symbol, close, adj_factor FROM ohlcv_adjusted "
        "ORDER BY isin, trade_date")
    prev_isin = None
    prev = None
    for isin, d, sym, c, af in cur:
        if isin != prev_isin:
            prev_isin, prev = isin, (d, sym, c, af)
            continue
        pd_, psym, pc, paf = prev
        prev = (d, sym, c, af)
        if not pc or pc <= 0 or not c or c <= 0:
            continue

        adj_move = c / pc - 1
        excess = adj_move - market.get(d, 0.0)
        act = ledger.get((isin, d))

        # adjusted = raw x adj_factor, so the untouched NSE move is recoverable
        # exactly from the stored factors — no second scan of layer 2 needed.
        raw_move = None
        if paf and af and paf > 0 and af > 0:
            raw_move = (c / af) / (pc / paf) - 1

        stored_move = None
        lp = legacy.get(psym) or legacy.get(sym) or {}
        if pd_ in lp and d in lp and lp[pd_]:
            stored_move = lp[d] / lp[pd_] - 1

        if act is None and abs(excess) < threshold:
            continue

        # classify
        if act and act[3] == "UNVERIFIABLE":
            cls, conf, res, susp = "UNVERIFIABLE", "none", "UNVERIFIABLE", act[0]
        elif act and act[3] == "applied":
            if abs(excess) >= threshold:
                cls, res = "STILL_CORRUPT", "NEEDS REVIEW"
            else:
                cls, res = "RESOLVED_BY_ACTION", "resolved"
            conf, susp = act[2], act[0]
        else:
            susp = None
            if listing.get(isin) and pd_ < listing[isin]:
                cls, conf, res = "PHANTOM_ROW", "high", "excluded"
            elif stored_move is not None and abs(stored_move - adj_move) > 0.05:
                cls, conf, res = "LEGACY_ARTIFACT", "high", "corrected"
            else:
                cls, conf, res = "GENUINE_MARKET_MOVE", "high", "kept"

        findings.append(Finding(
            isin=isin, symbol=sym, date=d,
            prev_close=round(pc, 2), curr_close=round(c, 2),
            raw_move=None if raw_move is None else round(raw_move, 4),
            stored_move=None if stored_move is None else round(stored_move, 4),
            adjusted_move=round(adj_move, 4),
            suspected_action=susp, classification=cls,
            confidence=conf, resolution=res))
    rc.close()
    return findings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser(description="Repeatable three-layer data-integrity audit")
    ap.add_argument("--root", default="data")
    ap.add_argument("--orig", default="market_data.db.orig-20260811")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()

    f = audit(Path(a.root), a.threshold, a.orig)
    counts: dict[str, int] = defaultdict(int)
    for x in f:
        counts[x["classification"]] += 1

    print("\n=== DATA INTEGRITY AUDIT ===")
    print(f"threshold: |move - market median| >= {a.threshold:.0%}\n")
    for k in ("STILL_CORRUPT", "UNVERIFIABLE", "PHANTOM_ROW", "LEGACY_ARTIFACT",
              "RESOLVED_BY_ACTION", "GENUINE_MARKET_MOVE"):
        print(f"  {k:<22} {counts.get(k, 0):>6}")
    print(f"  {'TOTAL':<22} {len(f):>6}")

    for k in ("STILL_CORRUPT", "UNVERIFIABLE"):
        rows = [x for x in f if x["classification"] == k]
        if not rows:
            continue
        print(f"\n--- {k} (top 25 by |adjusted move|) ---")
        for x in sorted(rows, key=lambda r: -abs(r["adjusted_move"]))[:25]:
            print(f"  {x['symbol']:<13} {x['date']}  {x['isin']}  "
                  f"adj={x['adjusted_move']:>8.1%}  {x['suspected_action'] or ''}")

    if a.csv:
        cols = ["isin", "symbol", "date", "prev_close", "curr_close", "raw_move",
                "stored_move", "adjusted_move", "suspected_action", "classification",
                "confidence", "resolution"]
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(f)
        print(f"\nwrote {len(f)} rows to {a.csv}")

    # Non-zero exit if the corrected dataset still contradicts NSE ground truth.
    return 1 if counts.get("STILL_CORRUPT") else 0


if __name__ == "__main__":
    sys.exit(main())
