#!/usr/bin/env python3
"""Backfill historical NSE corporate events for backtesting the events filter.

The live corporate-events pond filter (scanner/stock_filter.py) reads
corporate_events.db, which the daily job only populates ~10 days ahead —
so it has no history before the bot's first deploy. Backtesting the filter
over 2021-2026 needs the actual historical ex-dates.

Fetches month-by-month from NSE:
  - corporates-corporateActions  → dividend / bonus / split ex-dates
  - corporate-board-meetings     → results (board meeting) dates

Rows are stored in the same corporate_events table the live filter reads
(deduplicated on symbol+event_type+ex_date; the table has no unique
constraint, so dedupe happens here). Historical rows are invisible to the
live filter — it only looks within ±7/3 days of today.

Usage:
    python scripts/backfill_events.py --start 2021-01-01 --end 2026-07-01
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import setup_logging
setup_logging()

from config.config import config
from data_fetcher.event_fetcher import _classify_event, _parse_nse_date, _HEADERS

logger = logging.getLogger("data.events_backfill")

_CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&from_date={f}&to_date={t}"
)
_BOARD_MEETINGS_URL = (
    "https://www.nseindia.com/api/corporate-board-meetings"
    "?index=equities&from_date={f}&to_date={t}"
)


def _fresh_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    try:
        s.get("https://www.nseindia.com/", timeout=15)
        time.sleep(2)
    except requests.RequestException:
        pass
    return s


def _get_json(session: requests.Session, url: str) -> list:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = data.get("data", [])
    return data if isinstance(data, list) else []


def _month_ranges(start: date, end: date):
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def _fetch_month(session: requests.Session, f: date, t: date) -> list[dict]:
    fmt = "%d-%m-%Y"
    events: list[dict] = []

    for item in _get_json(session, _CORP_ACTIONS_URL.format(f=f.strftime(fmt), t=t.strftime(fmt))):
        etype = _classify_event(item.get("subject", ""))
        symbol = str(item.get("symbol", "")).upper()
        ex_date = _parse_nse_date(item.get("exDate") or item.get("ex_date"))
        if etype and symbol and ex_date:
            events.append({"symbol": symbol, "event_type": etype, "ex_date": ex_date,
                           "details": str(item.get("subject", ""))})
    time.sleep(config.NSE_SCRAPE_DELAY_SECONDS)

    try:
        for item in _get_json(session, _BOARD_MEETINGS_URL.format(f=f.strftime(fmt), t=t.strftime(fmt))):
            purpose = str(item.get("bm_purpose") or item.get("purpose") or item.get("bm_desc") or "")
            if "result" not in purpose.lower():
                continue
            symbol = str(item.get("bm_symbol") or item.get("symbol") or "").upper()
            bm_date = _parse_nse_date(item.get("bm_date") or item.get("meetingDate"))
            if symbol and bm_date:
                events.append({"symbol": symbol, "event_type": "results", "ex_date": bm_date,
                               "details": purpose[:200]})
    except Exception as exc:
        # Board meetings endpoint is best-effort — corporate actions alone
        # still cover the dividend/bonus/split part of the filter
        logger.warning("Board meetings fetch failed for %s→%s: %s", f, t, exc)
    time.sleep(config.NSE_SCRAPE_DELAY_SECONDS)

    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical NSE corporate events")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--db", default="corporate_events_full.db",
        help="Target DB filename inside the data dir. Defaults to a backtest-only "
             "snapshot — writing multi-year history into the LIVE corporate_events.db "
             "would silently change the live pond (the filter's results half is "
             "otherwise inert; see DUST_FLOOR_DECISION.md / 2026-08-07).",
    )
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    db_path = Path(config.DB_PATH) / args.db
    if not db_path.exists():
        logger.info("Creating %s", db_path)
        import sqlite3
        sqlite3.connect(db_path).executescript(
            "CREATE TABLE IF NOT EXISTS corporate_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol VARCHAR(20) NOT NULL, "
            "event_type VARCHAR(30) NOT NULL, ex_date DATE, record_date DATE, "
            "announcement_date DATE, details TEXT, fetched_at DATETIME NOT NULL);"
            "CREATE INDEX IF NOT EXISTS idx_events_symbol_date "
            "ON corporate_events (symbol, ex_date);"
        )
    engine = create_engine(f"sqlite:///{db_path}")

    with engine.connect() as conn:
        existing = {
            (r[0], r[1], str(r[2]))
            for r in conn.execute(text("SELECT symbol, event_type, ex_date FROM corporate_events"))
        }
    logger.info("%d existing event rows", len(existing))

    session = _fresh_session()
    total_new = 0
    for i, (f, t) in enumerate(_month_ranges(start, end)):
        if i and i % 12 == 0:
            session = _fresh_session()  # NSE cookies go stale
        try:
            events = _fetch_month(session, f, t)
        except Exception as exc:
            logger.error("Month %s→%s FAILED: %s — retrying with fresh session", f, t, exc)
            time.sleep(10)
            session = _fresh_session()
            try:
                events = _fetch_month(session, f, t)
            except Exception as exc2:
                logger.error("Month %s→%s failed twice, skipping: %s", f, t, exc2)
                continue

        new_rows = [
            ev for ev in events
            if (ev["symbol"], ev["event_type"], str(ev["ex_date"])) not in existing
        ]
        now = datetime.now()
        with engine.begin() as conn:
            for ev in new_rows:
                conn.execute(
                    text(
                        "INSERT INTO corporate_events "
                        "(symbol, event_type, ex_date, record_date, announcement_date, details, fetched_at) "
                        "VALUES (:sym, :etype, :exd, NULL, NULL, :det, :now)"
                    ),
                    {"sym": ev["symbol"], "etype": ev["event_type"], "exd": ev["ex_date"],
                     "det": ev["details"], "now": now},
                )
                existing.add((ev["symbol"], ev["event_type"], str(ev["ex_date"])))
        total_new += len(new_rows)
        logger.info("%s → %s: %d fetched, %d new (running total %d)",
                    f, t, len(events), len(new_rows), total_new)

    logger.info("Backfill complete: %d new event rows", total_new)


if __name__ == "__main__":
    main()
