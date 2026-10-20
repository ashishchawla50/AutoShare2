"""NSE trading holiday calendar.

Hardcoded for the current year with a fetch fallback from NSE website.
NSE publishes the holiday list at the start of each calendar year.
"""

import datetime
from typing import Optional
import requests
import logging

logger = logging.getLogger(__name__)

# NSE equity segment holidays 2024
_NSE_HOLIDAYS_2024: set[datetime.date] = {
    datetime.date(2024, 1, 22),   # Ram Mandir Consecration (special holiday)
    datetime.date(2024, 1, 26),   # Republic Day
    datetime.date(2024, 3, 8),    # Mahashivratri
    datetime.date(2024, 3, 25),   # Holi
    datetime.date(2024, 3, 29),   # Good Friday
    datetime.date(2024, 4, 11),   # Id-Ul-Fitr (Ramzan Id)
    datetime.date(2024, 4, 14),   # Dr. Ambedkar Jayanti
    datetime.date(2024, 4, 17),   # Ram Navami
    datetime.date(2024, 4, 21),   # Mahavir Jayanti
    datetime.date(2024, 5, 23),   # Buddha Purnima
    datetime.date(2024, 6, 17),   # Bakri Id
    datetime.date(2024, 7, 17),   # Moharram
    datetime.date(2024, 8, 15),   # Independence Day
    datetime.date(2024, 10, 2),   # Mahatma Gandhi Jayanti
    datetime.date(2024, 11, 1),   # Diwali Laxmi Puja
    datetime.date(2024, 11, 15),  # Gurunanak Jayanti
    datetime.date(2024, 12, 25),  # Christmas
}

# NSE equity segment holidays 2025
_NSE_HOLIDAYS_2025: set[datetime.date] = {
    datetime.date(2025, 2, 26),   # Mahashivratri
    datetime.date(2025, 3, 14),   # Holi
    datetime.date(2025, 3, 31),   # Id-Ul-Fitr (Ramzan Id)
    datetime.date(2025, 4, 10),   # Shri Ram Navami
    datetime.date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    datetime.date(2025, 4, 18),   # Good Friday
    datetime.date(2025, 5, 1),    # Maharashtra Day
    datetime.date(2025, 5, 12),   # Buddha Purnima
    datetime.date(2025, 6, 7),    # Id-Ul-Zuha (Bakri Id)
    datetime.date(2025, 7, 6),    # Moharram
    datetime.date(2025, 8, 15),   # Independence Day
    datetime.date(2025, 8, 27),   # Ganesh Chaturthi
    datetime.date(2025, 10, 2),   # Mahatma Gandhi Jayanti / Dussehra
    datetime.date(2025, 10, 20),  # Diwali Amavasya (Laxmi Puja)
    datetime.date(2025, 10, 21),  # Diwali Balipratipada
    datetime.date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev Ji
    datetime.date(2025, 12, 25),  # Christmas
}

# NSE equity segment holidays 2026 — official list from NSE's holiday-master API
# (type=trading, CM segment), fetched 2026-10-19. Replaces an estimated list that had
# 7 real trading days marked as holidays (Feb 16, Mar 4, 20, 30, Apr 8, Jun 18, Jul 7)
# and missed Jan 15, Mar 3, 26, 31, Jun 26, Sep 14, Oct 20. Weekend entries are kept
# for completeness; they change nothing.
_NSE_HOLIDAYS_2026: set[datetime.date] = {
    datetime.date(2026, 1, 15),   # Municipal Corporation Election - Maharashtra
    datetime.date(2026, 1, 26),   # Republic Day
    datetime.date(2026, 2, 15),   # Mahashivratri (Sunday)
    datetime.date(2026, 3, 3),    # Holi
    datetime.date(2026, 3, 21),   # Id-Ul-Fitr (Ramadan Eid) (Saturday)
    datetime.date(2026, 3, 26),   # Shri Ram Navami
    datetime.date(2026, 3, 31),   # Shri Mahavir Jayanti
    datetime.date(2026, 4, 3),    # Good Friday
    datetime.date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    datetime.date(2026, 5, 1),    # Maharashtra Day
    datetime.date(2026, 5, 28),   # Bakri Id
    datetime.date(2026, 6, 26),   # Muharram
    datetime.date(2026, 8, 15),   # Independence Day (Saturday)
    datetime.date(2026, 9, 14),   # Ganesh Chaturthi
    datetime.date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    datetime.date(2026, 10, 20),  # Dussehra
    datetime.date(2026, 11, 8),   # Diwali Laxmi Pujan (Sunday; Muhurat session timing per NSE circular)
    datetime.date(2026, 11, 10),  # Diwali-Balipratipada
    datetime.date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    datetime.date(2026, 12, 25),  # Christmas
}

_ALL_HOLIDAYS: dict[int, set[datetime.date]] = {
    2024: _NSE_HOLIDAYS_2024,
    2025: _NSE_HOLIDAYS_2025,
    2026: _NSE_HOLIDAYS_2026,
}


def is_trading_day(date: datetime.date) -> bool:
    """Return True if NSE equity segment is open on the given date."""
    if date.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    year_holidays = _ALL_HOLIDAYS.get(date.year, set())
    return date not in year_holidays


def get_trading_days(
    start: datetime.date,
    end: datetime.date,
) -> list[datetime.date]:
    """Return list of NSE trading days between start and end (inclusive)."""
    days: list[datetime.date] = []
    current = start
    while current <= end:
        if is_trading_day(current):
            days.append(current)
        current += datetime.timedelta(days=1)
    return days


def next_trading_day(date: datetime.date) -> datetime.date:
    """Return the next trading day after the given date."""
    candidate = date + datetime.timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += datetime.timedelta(days=1)
    return candidate


def prev_trading_day(date: datetime.date) -> datetime.date:
    """Return the most recent trading day before the given date."""
    candidate = date - datetime.timedelta(days=1)
    while not is_trading_day(candidate):
        candidate -= datetime.timedelta(days=1)
    return candidate


def trading_days_between(start: datetime.date, end: datetime.date) -> int:
    """Count trading days between two dates (exclusive of start, inclusive of end)."""
    return len(get_trading_days(start + datetime.timedelta(days=1), end))


def try_fetch_nse_holidays(year: int) -> Optional[set[datetime.date]]:
    """Attempt to fetch the holiday list for a given year from NSE.

    Returns the set of holiday dates if successful, None on failure.
    The caller should fall back to the hardcoded list on None.
    """
    url = "https://www.nseindia.com/api/holiday-master?type=trading"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        session = requests.Session()
        # NSE requires a cookie from the homepage first
        session.get("https://www.nseindia.com/", headers=headers, timeout=10)
        resp = session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        holidays: set[datetime.date] = set()
        for entry in data.get("CM", []):  # CM = Capital Market segment
            try:
                dt = datetime.datetime.strptime(entry["tradingDate"], "%d-%b-%Y").date()
                if dt.year == year:
                    holidays.add(dt)
            except (KeyError, ValueError):
                continue
        if holidays:
            logger.info("Fetched %d NSE holidays for %d from API", len(holidays), year)
            return holidays
    except Exception as exc:
        logger.warning("Failed to fetch NSE holidays from API: %s", exc)
    return None
