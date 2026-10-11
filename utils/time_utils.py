"""Market hours, timezone, and IST-aware time utilities."""

import datetime
from zoneinfo import ZoneInfo

import pytz

from config.config import config
from config.holidays import is_trading_day

IST = ZoneInfo("Asia/Kolkata")
IST_PYTZ = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime.datetime:
    """Return the current time in IST."""
    return datetime.datetime.now(tz=IST)


def today_ist() -> datetime.date:
    """Return today's date in IST."""
    return now_ist().date()


def is_market_open() -> bool:
    """Return True if NSE is currently in session (9:15–15:30 IST, trading day)."""
    now = now_ist()
    if not is_trading_day(now.date()):
        return False
    market_open = _parse_time(config.MARKET_OPEN)
    market_close = _parse_time(config.MARKET_CLOSE)
    current_time = now.time().replace(second=0, microsecond=0)
    return market_open <= current_time <= market_close


def is_entry_window() -> bool:
    """Return True if within the valid entry window (9:30–10:30 IST)."""
    now = now_ist()
    if not is_trading_day(now.date()):
        return False
    start = _parse_time(config.ENTRY_WINDOW_START)
    end = _parse_time(config.ENTRY_WINDOW_END)
    current_time = now.time().replace(second=0, microsecond=0)
    return start <= current_time <= end


def is_pre_market() -> bool:
    """Return True between BOT_DAY_START and MARKET_OPEN (IST) on a trading day."""
    now = now_ist()
    if not is_trading_day(now.date()):
        return False
    current_time = now.time().replace(second=0, microsecond=0)
    return _parse_time(config.BOT_DAY_START) <= current_time < _parse_time(config.MARKET_OPEN)


def minutes_to_market_close() -> int:
    """Return minutes remaining until market close; negative if market is closed."""
    now = now_ist()
    close_t = _parse_time(config.MARKET_CLOSE)
    close_today = now.replace(
        hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0
    )
    delta = close_today - now
    return int(delta.total_seconds() / 60)


def ist_to_utc(dt: datetime.datetime) -> datetime.datetime:
    """Convert an IST-aware datetime to UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(datetime.timezone.utc)


def utc_to_ist(dt: datetime.datetime) -> datetime.datetime:
    """Convert a UTC-aware datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(IST)


def make_ist_datetime(date: datetime.date, time_str: str) -> datetime.datetime:
    """Create an IST-aware datetime from a date and HH:MM string."""
    t = _parse_time(time_str)
    return datetime.datetime(date.year, date.month, date.day, t.hour, t.minute, tzinfo=IST)


def _parse_time(time_str: str) -> datetime.time:
    """Parse 'HH:MM' into a time object."""
    h, m = time_str.split(":")
    return datetime.time(int(h), int(m))


def trading_day_start(date: datetime.date) -> datetime.datetime:
    """Return 9:15 AM IST on the given trading date."""
    return make_ist_datetime(date, config.MARKET_OPEN)


def trading_day_end(date: datetime.date) -> datetime.datetime:
    """Return 3:30 PM IST on the given trading date."""
    return make_ist_datetime(date, config.MARKET_CLOSE)
