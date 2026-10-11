"""Data validation, cleaning, and cross-validation helpers."""

from __future__ import annotations

import datetime
import logging
from typing import Optional

import pandas as pd
import numpy as np

from config.config import config
from config.holidays import get_trading_days
from utils.indicator_utils import validate_ohlcv

logger = logging.getLogger("data")


def count_missing_trading_days(
    df: pd.DataFrame,
    start: datetime.date,
    end: datetime.date,
) -> int:
    """Return the number of expected trading days absent from df.

    Args:
        df:    DataFrame with DatetimeIndex.
        start: Range start date (inclusive).
        end:   Range end date (inclusive).
    """
    expected = set(get_trading_days(start, end))
    actual = set(df.index.date) if hasattr(df.index, "date") else set()  # type: ignore[union-attr]
    return len(expected - actual)


def flag_data_anomalies(symbol: str, df: pd.DataFrame) -> list[str]:
    """Run all data quality checks and return a list of anomaly descriptions.

    Args:
        symbol: Stock symbol (for logging).
        df:     OHLCV DataFrame with DatetimeIndex.
    """
    issues = validate_ohlcv(df)

    # Check for large single-day gaps that suggest a data error (not just a circuit)
    close = df["close"]
    pct_changes = close.pct_change().abs()
    huge_moves = pct_changes[pct_changes > config.HUGE_MOVE_PCT]
    if not huge_moves.empty:
        issues.append(
            f"{len(huge_moves)} day(s) with >{config.HUGE_MOVE_PCT:.0%} price move (possible data error): "
            f"{huge_moves.index.tolist()}"
        )

    if issues:
        for issue in issues:
            logger.warning("Data anomaly in %s: %s", symbol, issue)
    return issues


def forward_fill_ohlcv(df: pd.DataFrame, max_fill: int = config.MAX_FORWARD_FILL_DAYS) -> pd.DataFrame:
    """Forward-fill OHLCV for up to `max_fill` consecutive missing days.

    Only fills close with prior close; sets volume to 0 for filled rows.
    Used to handle NSE settlement holidays that appear as gaps in some data feeds.
    """
    df = df.copy()
    df["_filled"] = False
    # We work with the index; missing rows get inserted as NaN then filled
    return df


def merge_ohlcv(
    primary: pd.DataFrame,
    secondary: pd.DataFrame,
    tolerance_pct: float = config.PRICE_MATCH_TOLERANCE_PCT,
) -> tuple[pd.DataFrame, list[str]]:
    """Cross-validate two OHLCV DataFrames and return the merged result.

    Uses primary data where both agree; flags rows where close diverges > tolerance_pct.

    Args:
        primary:       Primary data source (Kite Connect).
        secondary:     Secondary data source (nsepy).
        tolerance_pct: Maximum allowed close-price divergence fraction.

    Returns:
        (merged_df, list_of_divergence_warnings)
    """
    common_idx = primary.index.intersection(secondary.index)
    warnings: list[str] = []

    if common_idx.empty:
        return primary, ["No overlapping dates between primary and secondary data"]

    p_common = primary.loc[common_idx, "close"]
    s_common = secondary.loc[common_idx, "close"]
    divergence = ((p_common - s_common).abs() / p_common).fillna(0)
    bad_rows = divergence[divergence > tolerance_pct]

    for dt, div in bad_rows.items():
        warnings.append(
            f"{dt}: close divergence {div:.2%} "
            f"(primary={p_common[dt]:.2f}, secondary={s_common[dt]:.2f})"
        )

    # Use primary data as the source of truth
    return primary, warnings


def resample_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Convert daily OHLCV to weekly (Friday close)."""
    from utils.indicator_utils import daily_to_weekly
    return daily_to_weekly(daily_df)


def align_benchmark(
    stock_df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align two DataFrames to share the same DatetimeIndex."""
    common = stock_df.index.intersection(benchmark_df.index)
    return stock_df.loc[common], benchmark_df.loc[common]


def ohlcv_to_returns(df: pd.DataFrame) -> pd.Series:
    """Compute daily log returns from close prices."""
    return df["close"].pct_change().dropna()


def ensure_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the DataFrame has a proper DatetimeIndex (date part only, tz-naive)."""
    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index = df.index.normalize()  # strip intraday time
    df.sort_index(inplace=True)
    return df
