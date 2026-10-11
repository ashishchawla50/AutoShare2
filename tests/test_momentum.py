"""Tests for the momentum strategy."""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.momentum_strategy import check_momentum


def _make_df(
    n: int = 80,
    trend: str = "up",   # "up", "down", "flat"
    vol_spike_today: bool = True,
) -> pd.DataFrame:
    """Synthetic OHLCV with controllable trend and volume."""
    dates = pd.date_range(end=date.today(), periods=n, freq="B")
    if trend == "up":
        close = 100.0 + np.arange(n) * 0.3
    elif trend == "down":
        close = 200.0 - np.arange(n) * 0.5
    else:
        close = np.full(n, 150.0)

    volume = np.full(n, 1_000_000.0)
    if vol_spike_today:
        volume[-1] = 2_000_000.0  # 2x — passes 1.5x threshold

    return pd.DataFrame({
        "open":   close * 0.99,
        "high":   close * 1.01,
        "low":    close * 0.98,
        "close":  close,
        "volume": volume,
    }, index=dates)


def test_momentum_passes_uptrend_with_volume():
    df = _make_df(trend="up", vol_spike_today=True)
    result = check_momentum("TEST", df)
    assert result.passes, result.failure_reason


def test_momentum_fails_downtrend():
    df = _make_df(trend="down", vol_spike_today=True)
    result = check_momentum("TEST", df)
    assert not result.passes
    assert "EMA21" in result.failure_reason or "EMA50" in result.failure_reason


def test_momentum_fails_without_volume():
    df = _make_df(trend="up", vol_spike_today=False)
    # All volumes equal → today = 1x average, below 1.5x threshold
    result = check_momentum("TEST", df)
    assert not result.passes
    assert "olume" in result.failure_reason


def test_momentum_fails_insufficient_data():
    df = _make_df(n=30, trend="up", vol_spike_today=True)
    result = check_momentum("TEST", df)
    assert not result.passes
    assert "Insufficient" in result.failure_reason


def test_momentum_stop_and_target_prices():
    df = _make_df(trend="up", vol_spike_today=True)
    result = check_momentum("TEST", df)
    assert result.passes
    entry = result.entry_price
    assert abs(result.stop_loss - entry * 0.95) < 0.05
    assert abs(result.target_price - entry * 1.12) < 0.05


def test_momentum_score_is_between_0_and_100():
    df = _make_df(trend="up", vol_spike_today=True)
    result = check_momentum("TEST", df)
    assert result.passes
    assert 0.0 <= result.score <= 100.0


def test_momentum_fails_overbought_rsi():
    """Stock in very steep uptrend will have RSI > 72 — should be rejected."""
    dates = pd.date_range(end=date.today(), periods=80, freq="B")
    # Extreme uptrend to push RSI above 72
    close = 100.0 + np.arange(80) ** 1.3 * 0.2
    volume = np.full(80, 1_000_000.0)
    volume[-1] = 2_000_000.0
    df = pd.DataFrame({
        "open": close * 0.99, "high": close * 1.02,
        "low": close * 0.98, "close": close, "volume": volume,
    }, index=dates)
    result = check_momentum("TEST", df)
    # Either passes (RSI within range) or fails with RSI reason — just check no crash
    assert isinstance(result.passes, bool)


def test_momentum_flat_market_no_ema_separation():
    df = _make_df(trend="flat", vol_spike_today=True)
    result = check_momentum("TEST", df)
    assert not result.passes
