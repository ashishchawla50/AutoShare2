"""Technical indicator calculations used across strategy modules.

All functions take a pandas Series and return a Series unless noted.
These are pure numeric functions with no side effects or I/O.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average (adjust=False matches charting platforms)."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder smoothing)."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def volume_sma(volume: pd.Series, period: int = 50) -> pd.Series:
    """Simple moving average of volume."""
    return volume.rolling(window=period).mean()


def highest_high(high: pd.Series, period: int) -> pd.Series:
    """Rolling highest high over the given period."""
    return high.rolling(window=period).max()


def lowest_low(low: pd.Series, period: int) -> pd.Series:
    """Rolling lowest low over the given period."""
    return low.rolling(window=period).min()


def daily_to_weekly(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to weekly (week ending Friday).

    Args:
        ohlcv: DataFrame with DatetimeIndex and columns [open, high, low, close, volume].

    Returns:
        Weekly resampled DataFrame.
    """
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(set(ohlcv.columns)):
        raise ValueError(f"DataFrame must have columns: {required}")
    return ohlcv.resample("W-FRI").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()


def pct_change_from_low(series: pd.Series, lookback: int) -> pd.Series:
    """Percent change from the rolling lowest value over the lookback window."""
    rolling_low = series.rolling(window=lookback).min()
    return (series - rolling_low) / rolling_low


def swing_highs(high: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """Identify swing highs (local maxima) in a price series.

    Returns a boolean Series where True marks a confirmed swing high.
    A point is a swing high if it is higher than `left` bars before
    and `right` bars after it.
    """
    result = pd.Series(False, index=high.index)
    for i in range(left, len(high) - right):
        window_left = high.iloc[i - left : i]
        window_right = high.iloc[i + 1 : i + right + 1]
        # >= on the left window (ties allowed), strictly > on the right — an
        # exact == on the left would require the bar to tie a prior high to
        # the paisa, which float prices almost never do (found 2026-07-15:
        # this silently disabled swing detection and with it the VCP path)
        if high.iloc[i] >= window_left.max() and high.iloc[i] > window_right.max():
            result.iloc[i] = True
    return result


def swing_lows(low: pd.Series, left: int = 5, right: int = 5) -> pd.Series:
    """Identify swing lows (local minima) in a price series.

    Returns a boolean Series where True marks a confirmed swing low.
    """
    result = pd.Series(False, index=low.index)
    for i in range(left, len(low) - right):
        window_left = low.iloc[i - left : i]
        window_right = low.iloc[i + 1 : i + right + 1]
        # mirror of swing_highs: <= on the left window (ties allowed), strictly < right
        if low.iloc[i] <= window_left.min() and low.iloc[i] < window_right.min():
            result.iloc[i] = True
    return result


def normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase all column names for consistent access across data sources."""
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    return df


def validate_ohlcv(df: pd.DataFrame) -> list[str]:
    """Check OHLCV data for basic sanity.

    Returns:
        List of validation error messages; empty = data is clean.
    """
    errors: list[str] = []
    if df.empty:
        return ["DataFrame is empty"]

    if (df["high"] < df["low"]).any():
        count = (df["high"] < df["low"]).sum()
        errors.append(f"{count} rows have high < low")

    if ((df["close"] > df["high"]) | (df["close"] < df["low"])).any():
        count = ((df["close"] > df["high"]) | (df["close"] < df["low"])).sum()
        errors.append(f"{count} rows have close outside high-low range")

    if (df["volume"] < 0).any():
        errors.append("Negative volume values found")

    if (df["close"] <= 0).any():
        errors.append("Non-positive close prices found")

    # Only check the latest bar against the 50-day trailing median of prior bars.
    # Median is robust to legitimate high-volume events (F&O expiry, results day)
    # that would inflate a mean-based baseline and cause false positives.
    if len(df) >= 10:
        latest_vol = int(df["volume"].iloc[-1])
        prior_vol = df["volume"].iloc[:-1].tail(50)
        median_vol = prior_vol.median()
        if median_vol > 0 and latest_vol > median_vol * 25:
            ratio = int(latest_vol / median_vol)
            errors.append(
                f"Latest volume {latest_vol:,} is {ratio}x the 50-day median "
                f"({int(median_vol):,}) — possible data error"
            )

    return errors
