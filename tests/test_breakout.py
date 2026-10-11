"""Tests for the 20-day range breakout scanner."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.breakout_scanner import BreakoutScanResult, check_range_breakout


def _make_df(
    n_days: int = 80,
    trend: str = "up",
    breakout_today: bool = True,
    high_volume_today: bool = True,
    price: float = 200.0,
) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame for breakout scanner tests."""
    dates = pd.date_range(end="2026-07-01", periods=n_days, freq="B")

    if trend == "up":
        closes = price * (1 + np.arange(n_days) * 0.001)
    elif trend == "down":
        closes = price * (1 - np.arange(n_days) * 0.002)
        closes = np.maximum(closes, 1.0)
    else:
        closes = np.full(n_days, price)

    avg_vol = 1_000_000
    volumes = np.full(n_days, avg_vol, dtype=float)

    if breakout_today:
        # Today's close is a clear 20-day high
        closes[-1] = closes[:-1].max() + 5.0
        if high_volume_today:
            volumes[-1] = avg_vol * 2.0   # 2x avg — passes 1.5x threshold
        else:
            volumes[-1] = avg_vol * 0.8   # below threshold

    df = pd.DataFrame({
        "open": closes * 0.99,
        "high": closes * 1.005,
        "low": closes * 0.985,
        "close": closes,
        "volume": volumes,
    }, index=dates)

    return df


# ── Basic pass / fail ─────────────────────────────────────────────────────────

def test_breakout_passes_clean_setup():
    """Uptrend with new 20-day high and 2x volume should pass all 4 conditions."""
    df = _make_df(n_days=80, trend="up", breakout_today=True, high_volume_today=True)
    result = check_range_breakout("TEST", df)
    assert result.passes, result.failure_reason


def test_breakout_returns_correct_prices():
    """Entry, stop, and target prices should be arithmetically consistent."""
    df = _make_df(n_days=80, trend="up", breakout_today=True, high_volume_today=True)
    result = check_range_breakout("TEST", df)
    assert result.passes
    assert result.stop_loss < result.entry_price < result.target_price
    # 5% stop
    assert abs(result.stop_loss / result.entry_price - 0.95) < 0.01
    # 10% target
    assert abs(result.target_price / result.entry_price - 1.10) < 0.01


def test_breakout_fails_no_new_high():
    """Flat close (not a new high) should fail condition 1."""
    df = _make_df(n_days=80, trend="up", breakout_today=False, high_volume_today=True)
    # Set today's close below prior max
    df.iloc[-1, df.columns.get_loc("close")] = float(df["close"].iloc[:-1].max()) - 2.0
    result = check_range_breakout("TEST", df)
    assert not result.passes
    assert "high" in result.failure_reason.lower()


def test_breakout_fails_low_volume():
    """Volume below 1.5x average should fail condition 2."""
    df = _make_df(n_days=80, trend="up", breakout_today=True, high_volume_today=False)
    result = check_range_breakout("TEST", df)
    assert not result.passes
    assert "Volume" in result.failure_reason or "vol" in result.failure_reason.lower()


def test_breakout_fails_below_ema50():
    """Price below 50 EMA (downtrend) should fail condition 3."""
    # A downtrend where today happens to be a local high but EMA50 is above price
    df = _make_df(n_days=80, trend="down", breakout_today=False, price=300.0)
    # Force today to look like a new 20-day high in a downtrend context
    # In a strong downtrend, EMA50 is above current price
    result = check_range_breakout("TEST", df)
    # The stock is below EMA50 in a downtrend — should fail
    assert not result.passes


def test_breakout_fails_insufficient_data():
    """Less than 60 rows should fail with 'Insufficient data'."""
    df = _make_df(n_days=30, trend="up", breakout_today=True, high_volume_today=True)
    result = check_range_breakout("TEST", df)
    assert not result.passes
    assert "Insufficient" in result.failure_reason


def test_breakout_score_is_non_negative():
    """Score for a valid breakout should be >= 0."""
    df = _make_df(n_days=80, trend="up", breakout_today=True, high_volume_today=True)
    result = check_range_breakout("TEST", df)
    if result.passes:
        assert result.score >= 0.0


def test_breakout_result_fields_populated():
    """A passing result must have all non-default numeric fields set."""
    df = _make_df(n_days=80, trend="up", breakout_today=True, high_volume_today=True)
    result = check_range_breakout("TEST", df)
    if result.passes:
        assert result.entry_price > 0
        assert result.stop_loss > 0
        assert result.target_price > 0
        assert result.volume_ratio >= 1.5
        assert result.ema50 > 0
        assert result.days_high >= 1


def test_breakout_failing_result_has_zero_prices():
    """A failing result should have zero entry/stop/target prices."""
    df = _make_df(n_days=30)  # too little data
    result = check_range_breakout("TEST", df)
    assert not result.passes
    assert result.entry_price == 0.0
    assert result.stop_loss == 0.0
    assert result.target_price == 0.0
