"""Fetch NSE corporate events: results dates, dividends, bonus issues, splits.

We use a ±3-day buffer around results dates and a 7-day buffer around
ex-dividend / bonus / split dates to avoid trading into price discontinuities.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config

logger = logging.getLogger("data.events")

EventType = Literal["results", "dividend", "bonus", "split"]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

_CORP_ACTIONS_URL = (
    "https://www.nseindia.com/api/corporates-corporateActions"
    "?index=equities&from_date={from_date}&to_date={to_date}"
)


def _nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get("https://www.nseindia.com/", timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        time.sleep(config.NSE_SCRAPE_DELAY_SECONDS)
    except requests.RequestException:
        pass
    return session


def fetch_corporate_events(days_ahead: int = None) -> list[dict]:
    """Fetch corporate events from NSE for the next N days.

    Args:
        days_ahead: Number of calendar days ahead to fetch events for.

    Returns:
        List of event dicts with keys: symbol, event_type, ex_date, record_date, details.
    """
    today = date.today()
    if days_ahead is None:
        days_ahead = config.EVENTS_FETCH_DAYS_AHEAD
    end_date = today + timedelta(days=days_ahead)
    url = _CORP_ACTIONS_URL.format(
        from_date=today.strftime("%d-%m-%Y"),
        to_date=end_date.strftime("%d-%m-%Y"),
    )

    session = _nse_session()
    events: list[dict] = []

    try:
        resp = session.get(url, timeout=config.SCRAPE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()

        for item in data:
            event_type = _classify_event(item.get("subject", ""))
            if event_type is None:
                continue

            ex_date = _parse_nse_date(item.get("exDate") or item.get("ex_date"))
            record_date = _parse_nse_date(item.get("recDate") or item.get("record_date"))
            symbol = item.get("symbol", "").upper()

            if not symbol:
                continue

            events.append(
                {
                    "symbol": symbol,
                    "event_type": event_type,
                    "ex_date": ex_date,
                    "record_date": record_date,
                    "announcement_date": today,
                    "details": str(item.get("subject", "")),
                }
            )

        logger.info("Fetched %d corporate events from NSE", len(events))
    except Exception as exc:
        logger.error("Failed to fetch corporate events: %s", exc)

    return events


def save_events(events: list[dict]) -> None:
    """Persist corporate events to the database."""
    if not events:
        return
    db_path = Path(config.DB_PATH) / config.CORPORATE_EVENTS_DB
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now()

    with engine.begin() as conn:
        for ev in events:
            conn.execute(
                text(
                    "INSERT OR REPLACE INTO corporate_events "
                    "(symbol, event_type, ex_date, record_date, announcement_date, details, fetched_at) "
                    "VALUES (:sym, :etype, :exd, :recd, :annd, :det, :now)"
                ),
                {
                    "sym": ev["symbol"],
                    "etype": ev["event_type"],
                    "exd": ev.get("ex_date"),
                    "recd": ev.get("record_date"),
                    "annd": ev.get("announcement_date"),
                    "det": ev.get("details", ""),
                    "now": now,
                },
            )
    logger.info("Saved %d corporate events to DB", len(events))


def has_upcoming_event(
    symbol: str,
    check_date: Optional[date] = None,
    results_buffer_days: int = None,
    other_event_buffer_days: int = None,
) -> tuple[bool, str]:
    """Check if a symbol has a corporate event within the buffer window.

    Args:
        symbol:                  NSE trading symbol.
        check_date:              Date to check from; defaults to today.
        results_buffer_days:     Days before/after results to exclude.
        other_event_buffer_days: Days before dividend/bonus/split ex-date.

    Returns:
        (has_event: bool, reason: str) — reason is empty string if no event.
    """
    ref_date = check_date or date.today()
    if results_buffer_days is None:
        results_buffer_days = config.RESULTS_BUFFER_DAYS
    if other_event_buffer_days is None:
        other_event_buffer_days = config.OTHER_EVENT_BUFFER_DAYS
    db_path = Path(config.DB_PATH) / config.CORPORATE_EVENTS_DB
    if not db_path.exists():
        return False, ""

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        df = pd.read_sql(
            f"SELECT * FROM corporate_events WHERE symbol='{symbol.upper()}'", engine
        )
    except Exception:
        return False, ""

    if df.empty:
        return False, ""

    for _, row in df.iterrows():
        ex_date = _safe_date(row.get("ex_date"))
        if ex_date is None:
            continue

        event_type = str(row.get("event_type", ""))

        if event_type == "results":
            buffer = results_buffer_days
            if abs((ex_date - ref_date).days) <= buffer:
                return True, f"results date {ex_date} within {buffer} days"
        else:
            buffer = other_event_buffer_days
            if 0 <= (ex_date - ref_date).days <= buffer:
                return True, f"{event_type} ex-date {ex_date} within {buffer} days"

    return False, ""


def _classify_event(subject: str) -> Optional[EventType]:
    s = subject.lower()
    if any(k in s for k in ("quarterly results", "financial results", "board meeting")):
        return "results"
    if "dividend" in s:
        return "dividend"
    if "bonus" in s:
        return "bonus"
    if "split" in s or "sub-division" in s:
        return "split"
    return None


def _parse_nse_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    for fmt in ("%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _safe_date(val: object) -> Optional[date]:
    if isinstance(val, date):
        return val
    if isinstance(val, str) and val:
        return _parse_nse_date(val)
    return None
