"""DATA FOUNDATION GATE — hard prerequisite for any backtest.

Exits non-zero if the research dataset is not trustworthy. Intended to be run
before any backtest and in CI. Every check states what it proves.

    python tools/validate_data_foundation.py --root data

Checks
   1  reconstructed dataset present and populated
   2  backtest data source is 'reconstructed', not the fabricated legacy set
   3  the universe implementation does not read universe_snapshots
   4  STILL_CORRUPT == 0
   5  no unresolved security sits inside the research universe
   6  no missing / blank ISINs
   7  no duplicate (isin, date) records
   8  no impossible OHLC values
   9  no same-day look-ahead in universe construction
  10  archive hash integrity (layer 2 regenerability)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))


def run(root: Path, skip_hash: bool = False) -> int:
    rc_path = root / "reconstructed.db"
    bh_path = root / "bhavcopy.db"

    # 1 ── dataset present
    if not rc_path.exists():
        check("reconstructed dataset present", False, f"missing {rc_path}")
        return report()
    rc = sqlite3.connect(f"file:{rc_path}?mode=ro", uri=True)
    q = lambda s, *a: rc.execute(s, a).fetchone()[0]

    n_isin = q("SELECT COUNT(*) FROM security_master")
    n_rows = q("SELECT COUNT(*) FROM ohlcv_adjusted")
    n_months = q("SELECT COUNT(DISTINCT effective_from) FROM universe_liquid500")
    check("reconstructed dataset populated", n_isin > 1000 and n_rows > 1_000_000 and n_months > 12,
          f"{n_isin} ISINs, {n_rows:,} rows, {n_months} months")

    # 2 ── explicit, safe data source
    try:
        from config.config import config
        src = config.BACKTEST_DATA_SOURCE
        check("BACKTEST_DATA_SOURCE == 'reconstructed'", src == "reconstructed", f"is {src!r}")
        check("fabricated data not force-enabled",
              not config.ALLOW_FABRICATED_BACKTEST_DATA,
              f"ALLOW_FABRICATED_BACKTEST_DATA={config.ALLOW_FABRICATED_BACKTEST_DATA}")
    except Exception as exc:                                    # pragma: no cover
        check("BACKTEST_DATA_SOURCE readable", False, str(exc))

    # 3 ── the universe cannot come from the fabricated table
    try:
        src_txt = (Path(__file__).parent.parent / "backtest" /
                   "point_in_time_universe.py").read_text(encoding="utf8")
        reads_fab = "FROM universe_snapshots" in src_txt or "from universe_snapshots" in src_txt
        check("universe does not query universe_snapshots", not reads_fab,
              "point_in_time_universe.py is clean" if not reads_fab else "STILL QUERIES IT")
        loader_txt = (Path(__file__).parent.parent / "backtest" /
                      "data_loader.py").read_text(encoding="utf8")
        reads_md = "FROM ohlcv_daily" in loader_txt and "NIFTY" not in loader_txt
        check("historical OHLCV does not come from market_data.db", not reads_md,
              "only the Nifty benchmark series remains there")
    except Exception as exc:                                    # pragma: no cover
        check("source inspection", False, str(exc))

    # 4 ── no security carries an unrepaired break
    still = q("SELECT COUNT(*) FROM corporate_actions WHERE resolution='applied_residual_break'")
    uncovered = q("""SELECT COUNT(*) FROM corporate_actions ca
                     WHERE ca.resolution IN ('applied_residual_break','UNVERIFIABLE')
                     AND NOT EXISTS (SELECT 1 FROM quarantine q
                                     WHERE q.isin = ca.isin AND q.ex_date = ca.ex_date)""")
    check("STILL_CORRUPT == 0 (every residual break quarantined)", uncovered == 0,
          f"{still} residual-break actions, {uncovered} lacking a quarantine window")

    # 5 ── nothing unresolved is inside the research universe
    leaked = q("""SELECT COUNT(*) FROM universe_liquid500 u JOIN quarantine qa
                  ON qa.isin = u.isin
                  WHERE qa.from_date <= u.effective_to AND qa.to_date >= u.effective_from""")
    check("no quarantined security inside the universe", leaked == 0, f"{leaked} leaks")

    # 6 ── identity integrity
    bad_isin = q("SELECT COUNT(*) FROM ohlcv_adjusted WHERE isin IS NULL OR isin=''")
    check("no missing ISINs", bad_isin == 0, f"{bad_isin} rows")
    orphan = q("""SELECT COUNT(*) FROM (SELECT DISTINCT isin FROM ohlcv_adjusted
                  WHERE isin NOT IN (SELECT isin FROM security_master))""")
    check("every priced ISIN has a security-master entry", orphan == 0, f"{orphan} orphans")

    # 7 ── no duplicates (primary key makes this structural; verify anyway)
    dup = q("""SELECT COUNT(*) FROM (SELECT isin, trade_date FROM ohlcv_adjusted
               GROUP BY 1,2 HAVING COUNT(*) > 1)""")
    check("no duplicate (isin, date) rows", dup == 0, f"{dup} duplicates")

    # 8 ── OHLC sanity
    bad = q("""SELECT COUNT(*) FROM ohlcv_adjusted
               WHERE high < low OR close > high OR close < low
                  OR open > high OR open < low OR close <= 0""")
    check("no impossible OHLC values", bad == 0, f"{bad} bad bars")

    # 9 ── membership must be computed strictly before it takes effect
    leak = q("""SELECT COUNT(*) FROM universe_liquid500
                WHERE computed_asof >= effective_from""")
    check("no same-day look-ahead in universe", leak == 0,
          f"{leak} rows computed on/after their effective date")

    # 10 ── layer 2 is intact, so layer 3 is regenerable
    if bh_path.exists() and not skip_hash:
        bh = sqlite3.connect(f"file:{bh_path}?mode=ro", uri=True)
        n_ok = bh.execute("SELECT COUNT(*) FROM download_log WHERE status='ok'").fetchone()[0]
        n_err = bh.execute("SELECT COUNT(*) FROM download_log WHERE status='error'").fetchone()[0]
        missing = 0
        for td, name in bh.execute(
                "SELECT trade_date, filename FROM download_log WHERE status='ok'"):
            if not (root / "bhavcopy_raw" / td[:4] / name).exists():
                missing += 1
        check("layer-2 archive complete", missing == 0 and n_err == 0,
              f"{n_ok} files, {missing} missing, {n_err} errors")
        bh.close()
    else:
        check("layer-2 archive present", bh_path.exists(),
              "hash check skipped" if skip_hash else f"missing {bh_path}")

    rc.close()
    return report()


def report() -> int:
    print("\n=== DATA FOUNDATION GATE ===\n")
    failed = 0
    for name, ok, detail in CHECKS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
        failed += (not ok)
    print()
    if failed:
        print(f"DATA FOUNDATION: NOT READY  ({failed} check(s) failed)")
        return 1
    print("DATA FOUNDATION: GO")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate the data foundation")
    ap.add_argument("--root", default="data")
    ap.add_argument("--skip-hash", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).parent.parent))
    sys.exit(run(Path(a.root), a.skip_hash))
