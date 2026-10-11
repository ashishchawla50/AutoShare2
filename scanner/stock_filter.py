"""All 9 stock universe filters applied sequentially.

Each filter is a pure function that takes OHLCV + metadata and returns
(passes: bool, reason: str). The orchestrator runs them all and
returns only stocks that pass every filter.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config
from data_fetcher.event_fetcher import has_upcoming_event
from data_fetcher.surveillance_fetcher import get_all_surveillance_symbols
from utils.data_utils import count_missing_trading_days

logger = logging.getLogger("scanner.filter")


@dataclass
class FilterResult:
    symbol: str
    passed: bool
    failed_at: str = ""       # name of the first filter that failed
    fail_reason: str = ""
    filter_details: dict = field(default_factory=dict)


def filter_price(symbol: str, close_price: float) -> tuple[bool, str]:
    """Filter 1: Minimum stock price ≥ ₹100."""
    if close_price < config.MIN_STOCK_PRICE:
        return False, f"Price ₹{close_price:.2f} < ₹{config.MIN_STOCK_PRICE:.0f}"
    return True, ""


def filter_market_cap(symbol: str, market_cap_cr: Optional[float]) -> tuple[bool, str]:
    """Filter 2: Minimum market cap ≥ ₹5,000 Cr."""
    if market_cap_cr is None:
        # Screener.in unreachable from Oracle Cloud VPS — passing by default.
        # Nifty 200 membership implies large-cap; false-positive risk is low.
        # Log at WARNING so this is visible, not invisible.
        logger.warning("%s: market_cap data unavailable — passing filter by default", symbol)
        return True, ""
    if market_cap_cr < config.MIN_MARKET_CAP_CR:
        return False, f"Market cap ₹{market_cap_cr:.0f}Cr < ₹{config.MIN_MARKET_CAP_CR:.0f}Cr"
    return True, ""


def filter_liquidity(
    symbol: str,
    daily_ohlcv: pd.DataFrame,
    lookback_days: int = None,
) -> tuple[bool, str]:
    """Filter 3: Average daily traded value ≥ config.MIN_DAILY_TRADED_VALUE_CR.

    Traded value (price × volume) is the liquidity measure that matters for
    fills; a raw share-count floor double-counts it and rejects high-priced
    liquid stocks, so value is the only test here (since 2026-07-29).
    """
    lookback_days = lookback_days or config.LIQUIDITY_LOOKBACK_DAYS
    if len(daily_ohlcv) < lookback_days:
        return False, f"Insufficient data: {len(daily_ohlcv)} days < {lookback_days}"

    recent = daily_ohlcv.tail(lookback_days)
    avg_volume = float(recent["volume"].mean())
    avg_price = float(recent["close"].mean())
    avg_traded_value_cr = (avg_volume * avg_price) / 1e7  # convert to Crores

    if avg_traded_value_cr < config.MIN_DAILY_TRADED_VALUE_CR:
        return False, f"Avg daily value ₹{avg_traded_value_cr:.1f}Cr < ₹{config.MIN_DAILY_TRADED_VALUE_CR:.0f}Cr"
    return True, ""


def filter_surveillance(
    symbol: str,
    surveillance_symbols: set[str],
    instrument_category: str = "",
) -> tuple[bool, str]:
    """Filter 4: Exclude ASM/ESM/T2T surveillance stocks and Z-category."""
    if symbol.upper() in surveillance_symbols:
        return False, "On ASM/ESM/T2T surveillance list"
    if instrument_category.upper() in ("BE", "BZ", "Z"):
        return False, f"Z/BE-category instrument (category={instrument_category})"
    return True, ""


# Event-exclusion buffers — shared with the backtest engine's events filter
# so both engines apply identical pond filtering (fidelity audit 2026-08-07).
# The values live in config; these names are kept for existing importers.
RESULTS_BUFFER_DAYS = config.RESULTS_BUFFER_DAYS
OTHER_EVENT_BUFFER_DAYS = config.OTHER_EVENT_BUFFER_DAYS


def filter_corporate_events(
    symbol: str,
    check_date: Optional[date] = None,
) -> tuple[bool, str]:
    """Filter 5: Exclude if results within 7 days or ex-dividend/bonus/split within 3 days.

    7 days = actual gap risk window for large-cap NSE stocks around earnings.
    3 days = minimal buffer before ex-date to avoid turbulence.
    The original 21-day block was eliminating ~33% of stock-days unnecessarily;
    Nifty 200 stocks don't gap meaningfully on routine dividends beyond 3 days.
    """
    has_event, reason = has_upcoming_event(
        symbol,
        check_date=check_date,
        results_buffer_days=RESULTS_BUFFER_DAYS,
        other_event_buffer_days=OTHER_EVENT_BUFFER_DAYS,
    )
    if has_event:
        return False, f"Corporate event: {reason}"
    return True, ""


def filter_circuit_breaker(
    symbol: str,
    daily_ohlcv: pd.DataFrame,
    lookback_days: int = None,
) -> tuple[bool, str]:
    """Filter 6: Exclude if more than MAX_CIRCUIT_HITS_60D circuit hits in the lookback."""
    lookback_days = lookback_days or config.CIRCUIT_LOOKBACK_DAYS
    if len(daily_ohlcv) < lookback_days + 1:
        return True, ""  # insufficient data → don't exclude

    recent = daily_ohlcv.tail(lookback_days + 1)
    pct_changes = recent["close"].pct_change().abs()
    circuit_hits = int((pct_changes > config.CIRCUIT_MOVE_PCT).sum())

    if circuit_hits > config.MAX_CIRCUIT_HITS_60D:
        return False, f"{circuit_hits} circuit hits in last {lookback_days} days (max {config.MAX_CIRCUIT_HITS_60D})"
    return True, ""


def filter_fundamentals(
    symbol: str,
    promoter_pledge_pct: Optional[float],
    debt_equity_ratio: Optional[float],
    roe_pct: Optional[float] = None,
) -> tuple[bool, str]:
    """Filter 7: Exclude high-pledge, high-debt, or low-ROE stocks."""
    data_missing = promoter_pledge_pct is None and debt_equity_ratio is None and roe_pct is None
    if data_missing:
        # Screener.in blocked from Oracle Cloud VPS — pledge, D/E, and ROE unavailable.
        # Unlike market cap, these metrics genuinely vary within Nifty 200.
        # Logging at WARNING so missing fundamental risk controls are always visible.
        logger.warning(
            "%s: pledge/D-E/ROE data unavailable (Screener.in blocked) — "
            "fundamentals filter NOT enforced for this stock", symbol
        )
        return True, ""
    if promoter_pledge_pct is not None and promoter_pledge_pct > config.MAX_PROMOTER_PLEDGE_PCT:
        return False, f"Promoter pledge {promoter_pledge_pct:.1f}% > {config.MAX_PROMOTER_PLEDGE_PCT:.0f}%"
    if debt_equity_ratio is not None and debt_equity_ratio > config.MAX_DEBT_EQUITY_RATIO:
        return False, f"D/E ratio {debt_equity_ratio:.2f} > {config.MAX_DEBT_EQUITY_RATIO:.0f}"
    if (
        config.ENFORCE_MIN_ROE
        and roe_pct is not None
        and roe_pct < config.MIN_ROE_PCT
    ):
        return False, f"ROE {roe_pct:.1f}% < {config.MIN_ROE_PCT:.0f}% (Prasenjit Paul quality threshold)"
    return True, ""


def filter_ipo_age(
    symbol: str,
    listing_date: Optional[date],
    check_date: Optional[date] = None,
) -> tuple[bool, str]:
    """Filter 8: Exclude stocks listed less than 252 trading days ago."""
    if listing_date is None:
        return True, ""  # can't determine listing date → don't exclude

    ref_date = check_date or date.today()
    calendar_days = (ref_date - listing_date).days
    # Approximate: MIN_LISTING_DAYS trading days ≈ MIN_LISTING_CALENDAR_DAYS calendar days
    if calendar_days < config.MIN_LISTING_CALENDAR_DAYS:
        return False, (f"Listed only {calendar_days} days ago "
                       f"(need {config.MIN_LISTING_CALENDAR_DAYS}+ calendar days)")
    return True, ""


def filter_data_quality(
    symbol: str,
    daily_ohlcv: pd.DataFrame,
    check_date: Optional[date] = None,
) -> tuple[bool, str]:
    """Filter 9: Exclude stocks with data quality issues."""
    ref_date = check_date or date.today()
    start_90 = ref_date - timedelta(days=config.DATA_QUALITY_WINDOW_DAYS)

    missing = count_missing_trading_days(daily_ohlcv, start_90, ref_date)
    if missing > config.MAX_MISSING_DAYS_90D:
        return False, (f"{missing} missing trading days in last {config.DATA_QUALITY_WINDOW_DAYS} days "
                       f"(max {config.MAX_MISSING_DAYS_90D})")

    # OHLC sanity
    if (daily_ohlcv["high"] < daily_ohlcv["low"]).any():
        return False, "OHLC validation failure: high < low"
    if ((daily_ohlcv["close"] > daily_ohlcv["high"]) | (daily_ohlcv["close"] < daily_ohlcv["low"])).any():
        return False, "OHLC validation failure: close outside high-low range"

    return True, ""


# ── Orchestrator ──────────────────────────────────────────────────────────────

def apply_all_filters(
    symbol: str,
    daily_ohlcv: pd.DataFrame,
    close_price: float,
    market_cap_cr: Optional[float],
    promoter_pledge_pct: Optional[float],
    debt_equity_ratio: Optional[float],
    listing_date: Optional[date],
    instrument_category: str = "",
    surveillance_symbols: Optional[set[str]] = None,
    check_date: Optional[date] = None,
    roe_pct: Optional[float] = None,
) -> FilterResult:
    """Run all 9 filters on a single stock. Returns on first failure.

    Args:
        symbol:              NSE trading symbol.
        daily_ohlcv:         DataFrame with daily OHLCV.
        close_price:         Latest close price.
        market_cap_cr:       Market cap in Crores.
        promoter_pledge_pct: Promoter pledge percentage.
        debt_equity_ratio:   Standalone D/E ratio.
        listing_date:        NSE listing date.
        instrument_category: NSE category ('EQ', 'BE', 'BZ', 'Z', etc.)
        surveillance_symbols: Pre-loaded set of surveillance symbols.
        check_date:          Date to evaluate filters for (default: today).
        roe_pct:             Return on Equity % (Prasenjit Paul quality gate).

    Returns:
        FilterResult with passed=True or the failing filter name.
    """
    surv = surveillance_symbols or get_all_surveillance_symbols(use_cached=True)
    details: dict = {}

    filters = [
        ("price",           lambda: filter_price(symbol, close_price)),
        ("market_cap",      lambda: filter_market_cap(symbol, market_cap_cr)),
        ("liquidity",       lambda: filter_liquidity(symbol, daily_ohlcv)),
        ("surveillance",    lambda: filter_surveillance(symbol, surv, instrument_category)),
        ("corporate_event", lambda: filter_corporate_events(symbol, check_date)),
        ("circuit_breaker", lambda: filter_circuit_breaker(symbol, daily_ohlcv)),
        ("fundamentals",    lambda: filter_fundamentals(symbol, promoter_pledge_pct, debt_equity_ratio, roe_pct)),
        ("ipo_age",         lambda: filter_ipo_age(symbol, listing_date, check_date)),
        ("data_quality",    lambda: filter_data_quality(symbol, daily_ohlcv, check_date)),
    ]

    for filter_name, filter_fn in filters:
        passed, reason = filter_fn()
        details[filter_name] = "PASS" if passed else f"FAIL: {reason}"
        if not passed:
            logger.debug("Filter %s FAILED for %s: %s", filter_name, symbol, reason)
            return FilterResult(
                symbol=symbol,
                passed=False,
                failed_at=filter_name,
                fail_reason=reason,
                filter_details=details,
            )

    return FilterResult(symbol=symbol, passed=True, filter_details=details)


def run_batch_filters(
    universe_data: dict,
    surveillance_symbols: Optional[set[str]] = None,
    check_date: Optional[date] = None,
) -> tuple[list[str], dict[str, FilterResult]]:
    """Apply all filters to the entire universe.

    Args:
        universe_data:       Dict with per-symbol data (ohlcv, fundamentals, etc.)
        surveillance_symbols: Pre-loaded surveillance set.
        check_date:          Evaluation date.

    Returns:
        (passing_symbols, all_results_dict)
    """
    surv = surveillance_symbols or get_all_surveillance_symbols(use_cached=True)
    results: dict[str, FilterResult] = {}
    passing: list[str] = []

    for symbol, data in universe_data.items():
        result = apply_all_filters(
            symbol=symbol,
            daily_ohlcv=data.get("ohlcv", pd.DataFrame()),
            close_price=data.get("close", 0.0),
            market_cap_cr=data.get("market_cap_cr"),
            promoter_pledge_pct=data.get("promoter_pledge_pct"),
            debt_equity_ratio=data.get("debt_equity_ratio"),
            listing_date=data.get("listing_date"),
            instrument_category=data.get("category", "EQ"),
            surveillance_symbols=surv,
            check_date=check_date,
            roe_pct=data.get("roe_pct"),
        )
        results[symbol] = result
        if result.passed:
            passing.append(symbol)

    fail_counts: dict[str, int] = {}
    for r in results.values():
        if not r.passed:
            fail_counts[r.failed_at] = fail_counts.get(r.failed_at, 0) + 1

    logger.info(
        "Filters: %d/%d passed | failures: %s",
        len(passing),
        len(universe_data),
        fail_counts,
    )
    return passing, results


if __name__ == "__main__":
    from utils.logger import setup_logging
    setup_logging()
    parser = argparse.ArgumentParser(description="Run stock filters on universe")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()
    check = date.fromisoformat(args.date) if args.date else None
    print(f"Stock filter module ready. Date: {check or 'today'}, Paper: {args.paper}")
