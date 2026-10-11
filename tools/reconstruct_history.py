"""Rebuild a corrected, point-in-time historical dataset — LAYER 3.

    LAYER 1  market_data.db.orig-*        original vendor data, never touched
    LAYER 2  bhavcopy_raw/ + bhavcopy.db  raw NSE files, never edited
    LAYER 3  reconstructed.db             THIS MODULE — fully regenerable from layer 2

Stages
  1  security_master / symbol_history  ISIN is the canonical identity; symbol and
                                       series are time-dependent attributes
  2  corporate_actions                 generalised detection; anything not
                                       confidently resolved is marked UNVERIFIABLE
                                       and is NEVER silently adjusted
  3  ohlcv_adjusted                    back-adjusted series built from raw prices
                                       plus the ledger from stage 2
  4  universe_liquid500                Historical Liquid-500, built only from
                                       information available before each month

Design notes
  - NSE bhavcopy PREVCLOSE was tested as a corporate-action detector and REJECTED:
    it carries the raw prior close, unadjusted (verified against NMDC 2022-10-27
    and ABFRL 2025-05-22). Detection uses market-relative discontinuity instead.
  - Each stock's move is measured against the cross-sectional median move that
    day, so market-wide crashes (2020-03-23) do not generate false positives.
  - The VPS has <1 GB RAM, so every stage streams per-ISIN groups and inserts in
    batches. Nothing holds the full ~2.8M-row table in memory.

Usage:
    python tools/reconstruct_history.py --root data --all
    python tools/reconstruct_history.py --root data --stage 4
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import date
from itertools import groupby
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger("tools.reconstruct")

COMMON_RATIOS = [2, 2.5, 3, 4, 5, 6, 10, 20, 1.5, 1.2, 1.25, 1.3333, 1.6667, 7.5]
RATIO_TOL = 0.03
EXCESS_THRESHOLD = 0.20      # move vs market median that triggers investigation
MIN_HISTORY_DAYS = 60        # listing-age proxy for universe eligibility
TURNOVER_LOOKBACK = 63       # ~3 months of sessions for the turnover median
MIN_DAYS_IN_WINDOW = 15      # must genuinely trade to be eligible
UNIVERSE_SIZE = 500
INACTIVE_SESSIONS = 30       # silent this long at the end => delisted/suspended
BATCH = 20_000

DDL = """
CREATE TABLE IF NOT EXISTS security_master (
    isin TEXT PRIMARY KEY, first_date TEXT, last_date TEXT, n_days INTEGER,
    latest_symbol TEXT, n_symbols INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS symbol_history (
    isin TEXT NOT NULL, symbol TEXT NOT NULL, series TEXT,
    from_date TEXT NOT NULL, to_date TEXT NOT NULL, n_days INTEGER,
    PRIMARY KEY (isin, symbol, from_date)
);
CREATE INDEX IF NOT EXISTS ix_sh_sym  ON symbol_history(symbol, from_date, to_date);
CREATE INDEX IF NOT EXISTS ix_sh_isin ON symbol_history(isin);

CREATE TABLE IF NOT EXISTS corporate_actions (
    isin TEXT NOT NULL, symbol TEXT, ex_date TEXT NOT NULL,
    action_type TEXT, ratio REAL,
    prev_close REAL, next_close REAL, raw_move REAL, excess_move REAL,
    source TEXT, confidence TEXT, resolution TEXT, note TEXT,
    PRIMARY KEY (isin, ex_date, action_type)
);
CREATE TABLE IF NOT EXISTS ohlcv_adjusted (
    isin TEXT NOT NULL, trade_date TEXT NOT NULL, symbol TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL,
    adj_factor REAL,
    PRIMARY KEY (isin, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_adj_date ON ohlcv_adjusted(trade_date);
CREATE INDEX IF NOT EXISTS ix_adj_sym  ON ohlcv_adjusted(symbol, trade_date);

CREATE TABLE IF NOT EXISTS universe_liquid500 (
    effective_from TEXT NOT NULL, effective_to TEXT NOT NULL,
    isin TEXT NOT NULL, symbol TEXT, rank INTEGER, median_turnover REAL,
    computed_asof TEXT,
    PRIMARY KEY (effective_from, isin)
);
CREATE INDEX IF NOT EXISTS ix_uni_range ON universe_liquid500(effective_from, effective_to);

CREATE TABLE IF NOT EXISTS market_daily_move (
    trade_date TEXT PRIMARY KEY, median_move REAL, n_stocks INTEGER
);

-- Securities excluded from the universe around an unresolved discontinuity.
-- A break at date D corrupts every indicator whose lookback spans D, so the
-- exclusion runs forward far enough to cover the longest one (EMA200).
CREATE TABLE IF NOT EXISTS quarantine (
    isin TEXT NOT NULL, from_date TEXT NOT NULL, to_date TEXT NOT NULL,
    ex_date TEXT, reason TEXT,
    PRIMARY KEY (isin, from_date, to_date)
);
CREATE INDEX IF NOT EXISTS ix_q_isin ON quarantine(isin, from_date, to_date);
"""

QUARANTINE_BACK = 60     # sessions before the break (covers holding + entry decisions)
QUARANTINE_FWD = 200     # sessions after — covers the EMA200 / trend-template window
TAIL_QUARANTINE = 60     # sessions before a series ends prematurely

SQL_EQ = ("SELECT isin, trade_date, symbol, series, open, high, low, close, qty, turnover "
          "FROM bhav_daily WHERE series='EQ' AND isin IS NOT NULL AND isin != '' "
          "AND close IS NOT NULL AND close > 0 ORDER BY isin, trade_date")


def _groups(bh: sqlite3.Connection) -> Iterator[tuple[str, list]]:
    """Stream (isin, [rows...]) without materialising the whole table."""
    cur = bh.execute(SQL_EQ)
    for isin, rows in groupby(cur, key=lambda r: r[0]):
        yield isin, list(rows)


def _flush(rc, sql, buf):
    if buf:
        rc.executemany(sql, buf)
        rc.commit()
        buf.clear()


def _parse_ratio(etype: str, details: str) -> Optional[float]:
    d = (details or "").strip()
    if etype == "split":
        # "R[se]" — Indian usage writes one rupee as "Re 1/-", not "Rs 1/-".
        # Matching only "Rs" silently dropped every split down to Re 1 face
        # value (10->1, 5->1, 2->1), which is the most common split of all.
        f = re.findall(r"R[se]\.?\s*([\d.]+)\s*/?-?\s*Per Share", d, re.I)
        if len(f) >= 2 and float(f[1]) > 0:
            return float(f[0]) / float(f[1])
    elif etype == "bonus":
        m = re.search(r"Bonus\s*(\d+)\s*:\s*(\d+)", d, re.I)
        if m and int(m.group(2)) > 0:
            return (int(m.group(1)) + int(m.group(2))) / int(m.group(2))
    return None


def _near_common(ratio: float) -> Optional[float]:
    for c in COMMON_RATIOS:
        if abs(ratio - c) / c < RATIO_TOL:
            return c
    return None


def _all_days(bh) -> list[str]:
    return [r[0] for r in bh.execute(
        "SELECT DISTINCT trade_date FROM bhav_daily ORDER BY trade_date")]


# ── stage 1 ────────────────────────────────────────────────────────────────────

def stage1_identity(bh, rc) -> None:
    logger.info("stage 1: ISIN security master")
    days = _all_days(bh)
    cutoff = days[max(0, len(days) - 1 - INACTIVE_SESSIONS)]

    rc.execute("DELETE FROM symbol_history")
    rc.execute("DELETE FROM security_master")
    sh_buf, sm_buf = [], []
    n_isin = n_changed = n_delisted = 0

    for isin, rows in _groups(bh):
        n_isin += 1
        spans: dict[str, list] = {}
        for _i, d, sym, ser, *_rest in rows:
            if sym not in spans:
                spans[sym] = [d, d, ser, 0]
            spans[sym][1] = d
            spans[sym][3] += 1
        for sym, (f, t, ser, n) in spans.items():
            sh_buf.append((isin, sym, ser, f, t, n))
        first, last = rows[0][1], rows[-1][1]
        latest = max(spans.items(), key=lambda kv: kv[1][1])[0]
        status = "active" if last >= cutoff else "delisted_or_suspended"
        if status != "active":
            n_delisted += 1
        if len(spans) > 1:
            n_changed += 1
        sm_buf.append((isin, first, last, len(rows), latest, len(spans), status))

        if len(sh_buf) >= BATCH:
            _flush(rc, "INSERT OR REPLACE INTO symbol_history VALUES (?,?,?,?,?,?)", sh_buf)
        if len(sm_buf) >= BATCH:
            _flush(rc, "INSERT OR REPLACE INTO security_master VALUES (?,?,?,?,?,?,?)", sm_buf)

    _flush(rc, "INSERT OR REPLACE INTO symbol_history VALUES (?,?,?,?,?,?)", sh_buf)
    _flush(rc, "INSERT OR REPLACE INTO security_master VALUES (?,?,?,?,?,?,?)", sm_buf)
    logger.warning("stage 1 done: %d ISINs | %d with symbol changes | %d delisted/suspended",
                   n_isin, n_changed, n_delisted)


# ── stage 2 ────────────────────────────────────────────────────────────────────

def _market_moves(bh, rc) -> dict[str, float]:
    """Cross-sectional median daily move — the crash-day guard."""
    cached = {r[0]: r[1] for r in rc.execute("SELECT trade_date, median_move FROM market_daily_move")}
    if cached:
        return cached
    logger.info("  computing cross-sectional median move per day")
    acc: dict[str, list] = defaultdict(list)
    for _isin, rows in _groups(bh):
        for i in range(1, len(rows)):
            p, c = rows[i - 1][7], rows[i][7]
            if p and p > 0:
                acc[rows[i][1]].append(c / p - 1)
    out = {}
    buf = []
    for d, v in acc.items():
        if len(v) >= 30:
            out[d] = statistics.median(v)
            buf.append((d, out[d], len(v)))
    rc.execute("DELETE FROM market_daily_move")
    _flush(rc, "INSERT OR REPLACE INTO market_daily_move VALUES (?,?,?)", buf)
    return out


def stage2_actions(bh, rc, events_db: Optional[Path]) -> None:
    logger.info("stage 2: corporate-action ledger")
    known: dict[str, list] = defaultdict(list)
    if events_db and events_db.exists():
        ev = sqlite3.connect(events_db)
        for sym, et, ex, det in ev.execute(
                "SELECT symbol,event_type,ex_date,details FROM corporate_events "
                "WHERE event_type IN ('split','bonus')"):
            r = _parse_ratio(et, det)
            if r and r > 1.02:
                known[sym.upper()].append((str(ex)[:10], et, r, (det or "").strip()))
        ev.close()
        logger.info("  %d symbols carry a parseable split/bonus ratio", len(known))

    market = _market_moves(bh, rc)
    rc.execute("DELETE FROM corporate_actions")
    buf = []
    n_hi = n_md = n_un = n_rb = 0

    for isin, rows in _groups(bh):
        for i in range(1, len(rows)):
            pc, cc = rows[i - 1][7], rows[i][7]
            cd, sym = rows[i][1], rows[i][2]
            if not pc or pc <= 0 or not cc or cc <= 0:
                continue
            r = cc / pc - 1
            excess = r - market.get(cd, 0.0)
            if abs(excess) < EXCESS_THRESHOLD:
                continue

            implied = pc / cc
            match = [k for k in known.get(sym, [])
                     if abs((date.fromisoformat(k[0]) - date.fromisoformat(cd)).days) <= 4]
            if match:
                # A company can run several actions on ONE ex-date (BAJAJFINSV
                # 2022-09-13 was a 1:5 split AND a 1:1 bonus). Taking only the
                # first left the other unadjusted and a ~50% break in the series.
                ratio = 1.0
                for _ex, _et, _r, _d in match:
                    ratio *= _r
                et = "+".join(sorted({m[1] for m in match}))
                det = "; ".join(m[3] for m in match)
                # Does the corroborated ratio actually explain the move? After
                # adjustment the move becomes ratio/implied - 1. If a large break
                # survives, the feed's ratio is wrong or misdated, so the action
                # is recorded but the security is quarantined rather than trusted.
                # Measured the same way the audit measures it — market-relative —
                # so the two can never disagree about what counts as unrepaired.
                residual = (ratio / implied - 1 if implied else 0.0) - market.get(cd, 0.0)
                if abs(residual) >= EXCESS_THRESHOLD:
                    buf.append((isin, sym, cd, et, ratio, pc, cc, r, excess,
                                "events_db", "high", "applied_residual_break",
                                f"ratio {ratio:g} leaves {residual:+.1%} residual — quarantined"))
                    n_rb += 1
                else:
                    buf.append((isin, sym, cd, et, ratio, pc, cc, r, excess,
                                "events_db", "high", "applied", det[:120]))
                    n_hi += 1
            else:
                near = _near_common(implied)
                if near and implied > 1.05:
                    # Ratio proximity ALONE is not proof. NMDC's demerger implied
                    # 1.254 and ABFRL's implied ~3.0 — both landed on textbook
                    # split ratios by coincidence and were silently "repaired" in
                    # the first run. Nothing uncorroborated is adjusted any more:
                    # it is recorded, quarantined, and left for a human.
                    buf.append((isin, sym, cd, "probable_split_bonus", near, pc, cc, r, excess,
                                "detected", "medium", "UNVERIFIABLE",
                                f"implied {implied:.3f} ~ {near} but absent from events db "
                                f"— NOT applied, ratio match is not corroboration"))
                    n_md += 1
                else:
                    # Demergers, mergers, unexplained gaps: recorded, NEVER adjusted.
                    buf.append((isin, sym, cd, "unknown_discontinuity", None, pc, cc, r, excess,
                                "detected", "none", "UNVERIFIABLE",
                                f"implied {implied:.3f} matches no standard ratio"))
                    n_un += 1
            if len(buf) >= BATCH:
                _flush(rc, "INSERT OR REPLACE INTO corporate_actions "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
    _flush(rc, "INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
    logger.warning("stage 2 done: %d discontinuities | applied=%d residual_break=%d "
                   "UNVERIFIABLE: probable=%d unknown=%d",
                   n_hi + n_md + n_un + n_rb, n_hi, n_rb, n_md, n_un)

    # Quarantine every unresolved break.
    days = _all_days(bh)
    idx = {d: i for i, d in enumerate(days)}
    rc.execute("DELETE FROM quarantine")
    qbuf = []
    for isin, ex, at in rc.execute(
            "SELECT isin, ex_date, action_type FROM corporate_actions "
            "WHERE resolution IN ('UNVERIFIABLE','applied_residual_break')"):
        i = idx.get(ex)
        if i is None:
            continue
        qbuf.append((isin, days[max(0, i - QUARANTINE_BACK)],
                     days[min(len(days) - 1, i + QUARANTINE_FWD)], ex,
                     f"unresolved {at}"))
    # Any security whose corporate action we could NOT repair is excluded for its
    # ENTIRE history, not merely around the break. If the transformation is
    # unknown we cannot vouch for the price level on either side of it, so the
    # conservative reading is that the security is untradeable in research.
    for (isin,) in rc.execute("SELECT DISTINCT isin FROM corporate_actions "
                              "WHERE resolution='applied_residual_break'").fetchall():
        r = rc.execute("SELECT first_date, last_date FROM security_master WHERE isin=?",
                       (isin,)).fetchone()
        if r:
            qbuf.append((isin, r[0], r[1], None, "unrepairable corporate action — full history"))

    # Delisting / merger / ISIN-change tails. 672 securities have a series that
    # simply stops. Without this a backtest could enter days before the data
    # vanishes and book a fabricated forced exit, so the run-up to any premature
    # series end is quarantined too.
    dataset_end = days[-1]
    cutoff_idx = max(0, len(days) - 1 - INACTIVE_SESSIONS)
    for isin, last in rc.execute("SELECT isin, last_date FROM security_master").fetchall():
        if last >= days[cutoff_idx]:
            continue                      # still trading at the end of the window
        i = idx.get(last)
        if i is None:
            continue
        qbuf.append((isin, days[max(0, i - TAIL_QUARANTINE)], last, last,
                     "series ends (delisted / merged / ISIN change)"))

    n_q, n_qs = len(qbuf), len({q[0] for q in qbuf})   # _flush clears the buffer
    _flush(rc, "INSERT OR REPLACE INTO quarantine VALUES (?,?,?,?,?)", qbuf)
    logger.warning("  quarantined %d (isin, window) pairs across %d securities", n_q, n_qs)


# ── stage 3 ────────────────────────────────────────────────────────────────────

def stage3_adjust(bh, rc) -> None:
    logger.info("stage 3: back-adjusting prices")
    applied: dict[str, list] = defaultdict(list)
    for isin, ex, ratio in rc.execute(
            "SELECT isin, ex_date, ratio FROM corporate_actions "
            "WHERE resolution IN ('applied','applied_residual_break') "
            "AND ratio IS NOT NULL"):
        applied[isin].append((ex, ratio))

    rc.execute("DELETE FROM ohlcv_adjusted")
    buf = []
    n_rows = n_isin = 0
    for isin, rows in _groups(bh):
        n_isin += 1
        acts = sorted(applied.get(isin, []), reverse=True)
        cum, ai = 1.0, 0
        out = []
        # Backwards: everything before an ex-date is divided by its ratio, so the
        # adjusted series is continuous across the action.
        for _i, d, sym, _ser, o, h, l, c, q, t in reversed(rows):
            while ai < len(acts) and d < acts[ai][0]:
                cum /= acts[ai][1]
                ai += 1
            out.append((isin, d, sym, (o or 0) * cum, (h or 0) * cum, (l or 0) * cum,
                        (c or 0) * cum, (q or 0) / cum if cum else q, t, cum))
        out.reverse()
        buf.extend(out)
        n_rows += len(out)
        if len(buf) >= BATCH:
            _flush(rc, "INSERT OR REPLACE INTO ohlcv_adjusted VALUES (?,?,?,?,?,?,?,?,?,?)", buf)
    _flush(rc, "INSERT OR REPLACE INTO ohlcv_adjusted VALUES (?,?,?,?,?,?,?,?,?,?)", buf)
    logger.warning("stage 3 done: %d adjusted rows across %d ISINs", n_rows, n_isin)


# ── stage 4 ────────────────────────────────────────────────────────────────────

def stage4_universe(bh, rc) -> None:
    """Historical Liquid-500.

    Membership for calendar month M is computed from sessions up to and including
    the LAST SESSION OF MONTH M-1. NSE publishes that bhavcopy ~18:00 IST the same
    evening, so the list is fully known before the first trade of month M.
    Same-day turnover can never affect same-day eligibility.
    """
    logger.info("stage 4: Historical Liquid-500")
    days = _all_days(bh)
    idx = {d: i for i, d in enumerate(days)}
    months: dict[str, list] = defaultdict(list)
    for d in days:
        months[d[:7]].append(d)
    keys = sorted(months)

    rc.execute("DELETE FROM universe_liquid500")
    buf = []
    # Queried per month rather than held in memory: the VPS has <1 GB RAM and the
    # full per-ISIN turnover series would not fit alongside SQLite's page cache.
    for mi in range(1, len(keys)):
        month = keys[mi]
        asof = months[keys[mi - 1]][-1]
        end = idx[asof] + 1
        d_turn = days[max(0, end - TURNOVER_LOOKBACK)]     # turnover window start
        d_hist = days[max(0, end - 250)]                   # listing-age window start

        turn: dict[str, list] = defaultdict(list)
        sym_of: dict[str, str] = {}
        for isin, sym, t in bh.execute(
                "SELECT isin, symbol, turnover FROM bhav_daily "
                "WHERE series='EQ' AND isin IS NOT NULL AND isin != '' "
                "AND close IS NOT NULL AND close > 0 "
                "AND trade_date >= ? AND trade_date <= ?", (d_turn, asof)):
            turn[isin].append(t or 0.0)
            sym_of[isin] = sym
        hist: dict[str, int] = defaultdict(int)
        for isin, n in bh.execute(
                "SELECT isin, COUNT(*) FROM bhav_daily "
                "WHERE series='EQ' AND isin IS NOT NULL AND isin != '' "
                "AND close IS NOT NULL AND close > 0 "
                "AND trade_date >= ? AND trade_date <= ? GROUP BY isin", (d_hist, asof)):
            hist[isin] = n

        # Securities with an unresolved discontinuity overlapping this month are
        # excluded outright rather than traded on data we cannot vouch for.
        ef0, et0 = months[month][0], months[month][-1]
        quar = {r[0] for r in rc.execute(
            "SELECT DISTINCT isin FROM quarantine WHERE from_date <= ? AND to_date >= ?",
            (et0, ef0))}

        cands = []
        for isin, tv in turn.items():
            if isin in quar:
                continue
            if len(tv) < MIN_DAYS_IN_WINDOW:
                continue
            if hist.get(isin, 0) < MIN_HISTORY_DAYS:
                continue
            cands.append((statistics.median(tv), isin))
        cands.sort(reverse=True)
        ef, et = months[month][0], months[month][-1]
        for rank, (mt, isin) in enumerate(cands[:UNIVERSE_SIZE], 1):
            buf.append((ef, et, isin, sym_of.get(isin), rank, mt, asof))
        if len(buf) >= BATCH:
            _flush(rc, "INSERT OR REPLACE INTO universe_liquid500 VALUES (?,?,?,?,?,?,?)", buf)
    _flush(rc, "INSERT OR REPLACE INTO universe_liquid500 VALUES (?,?,?,?,?,?,?)", buf)
    logger.warning("stage 4 done: %d monthly universes", len(keys) - 1)


# ── point-in-time identity queries ─────────────────────────────────────────────

def symbol_at(rc, isin: str, on: str) -> Optional[str]:
    r = rc.execute("SELECT symbol FROM symbol_history WHERE isin=? AND from_date<=? "
                   "AND to_date>=? LIMIT 1", (isin, on, on)).fetchone()
    return r[0] if r else None


def isin_at(rc, symbol: str, on: str) -> Optional[str]:
    r = rc.execute("SELECT isin FROM symbol_history WHERE symbol=? AND from_date<=? "
                   "AND to_date>=? LIMIT 1", (symbol, on, on)).fetchone()
    return r[0] if r else None


def universe_at(rc, on: str) -> list[str]:
    return [r[0] for r in rc.execute(
        "SELECT isin FROM universe_liquid500 WHERE effective_from<=? AND effective_to>=? "
        "ORDER BY rank", (on, on))]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser(description="Rebuild corrected history (layer 3)")
    ap.add_argument("--root", default="data")
    ap.add_argument("--stage", type=int, choices=[1, 2, 3, 4])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--events-db", default="corporate_events_full.db")
    a = ap.parse_args()

    root = Path(a.root)
    bh = sqlite3.connect(root / "bhavcopy.db")
    rc = sqlite3.connect(root / "reconstructed.db")
    rc.executescript(DDL); rc.commit()
    ev = root / a.events_db

    for s in ([1, 2, 3, 4] if a.all else [a.stage]):
        {1: lambda: stage1_identity(bh, rc),
         2: lambda: stage2_actions(bh, rc, ev),
         3: lambda: stage3_adjust(bh, rc),
         4: lambda: stage4_universe(bh, rc)}[s]()
    bh.close(); rc.close()
