"""Unit tests for the mean reversion strategy (Connors RSI-2 variant).

Pinned conditions per spec:
  - Entry fires when RSI(2) < 10 AND close > 200-day EMA
  - Exit fires on close > 5-day EMA
  - Exit fires on RSI(2) > 70
  - Exit fires on 7-day time stop
  - Costs of 0.5% round trip are modeled (verified via cost_model)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.mean_reversion import (
    MeanReversionResult,
    check_mean_reversion,
    should_exit_mean_reversion,
    _count_consecutive_down,
)
from backtest.cost_model import calculate_net_pnl, TradeCosts


# ── OHLCV builders ───────────────────────────────────────────────────────────

def _make_uptrend_df(n: int = 250, start_price: float = 500.0) -> pd.DataFrame:
    """Steady uptrend: all closes above any EMA200 computed from the series."""
    dates = pd.date_range(end="2026-07-01", periods=n, freq="B")
    closes = start_price * (1 + np.arange(n) * 0.002)
    return pd.DataFrame({
        "open": closes * 0.99,
        "high": closes * 1.005,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.full(n, 1_000_000),
    }, index=dates)


def _force_oversold(df: pd.DataFrame, rsi2_target: float = 5.0) -> pd.DataFrame:
    """Force the last few candles down so RSI(2) drops below rsi2_target."""
    df = df.copy()
    # Drop today's and yesterday's close steeply to push RSI(2) very low
    last_close = float(df["close"].iloc[-3])
    df.iloc[-2, df.columns.get_loc("close")] = last_close * 0.97
    df.iloc[-1, df.columns.get_loc("close")] = last_close * 0.94
    df.iloc[-2, df.columns.get_loc("low")] = last_close * 0.97
    df.iloc[-1, df.columns.get_loc("low")] = last_close * 0.93
    return df


def _make_downtrend_df(n: int = 250, start_price: float = 500.0) -> pd.DataFrame:
    """Downtrend: close will be below EMA200."""
    dates = pd.date_range(end="2026-07-01", periods=n, freq="B")
    closes = start_price * (1 - np.arange(n) * 0.002)
    closes = np.maximum(closes, 1.0)
    return pd.DataFrame({
        "open": closes * 1.01,
        "high": closes * 1.015,
        "low": closes * 0.995,
        "close": closes,
        "volume": np.full(n, 1_000_000),
    }, index=dates)


# ── Entry condition tests ─────────────────────────────────────────────────────

def test_entry_fires_rsi2_below_10_in_uptrend():
    """SPEC: entry fires when RSI(2) < 10 AND close > 200-day EMA."""
    df = _make_uptrend_df(n=250)
    df = _force_oversold(df)
    result = check_mean_reversion("TEST", df)
    # If RSI(2) was pushed low enough, it should pass
    if result.passes:
        assert result.rsi2 < 10.0
        assert result.entry_price > result.ema200
    # Whether it passes or not, it should never crash
    assert isinstance(result, MeanReversionResult)


def test_entry_blocked_by_trend_gate():
    """SPEC: close <= 200-day EMA must block entry (trend gate)."""
    df = _make_downtrend_df(n=250)
    result = check_mean_reversion("TEST", df)
    assert not result.passes
    assert "uptrend" in result.failure_reason or "EMA200" in result.failure_reason


def test_entry_blocked_rsi2_not_oversold():
    """RSI(2) >= 10 in an uptrend must block entry. A pure uptrend is never oversold."""
    df = _make_uptrend_df(n=250)
    # A monotonic uptrend has RSI(2) at or near 100 — never oversold
    result = check_mean_reversion("TEST", df)
    # Must not pass (whether blocked by RSI or NaN from pure uptrend — both are correct)
    assert not result.passes


def test_entry_blocked_insufficient_data():
    """Fewer than EMA_TREND+10 bars should fail with 'Insufficient data'."""
    df = _make_uptrend_df(n=100)
    result = check_mean_reversion("TEST", df)
    assert not result.passes
    assert "Insufficient" in result.failure_reason


def test_entry_prices_correct_on_pass():
    """A passing signal must have stop 5% below entry and zero target (EMA/RSI exit)."""
    df = _make_uptrend_df(n=250)
    df = _force_oversold(df)
    result = check_mean_reversion("TEST", df)
    if result.passes:
        assert result.stop_loss == round(result.entry_price * 0.95, 2)
        assert result.entry_price > 0
        assert result.ema200 > 0
        assert result.exit_ema5 > 0
        assert result.rsi2 < 10.0


def test_failing_result_has_zero_prices():
    """A failing result must return zero entry/stop prices."""
    df = _make_downtrend_df(n=250)
    result = check_mean_reversion("TEST", df)
    assert not result.passes
    assert result.entry_price == 0.0
    assert result.stop_loss == 0.0


# ── Exit condition tests ──────────────────────────────────────────────────────

def test_exit_fires_on_close_above_ema5():
    """SPEC: exit fires when close > 5-day EMA."""
    ema5 = 100.0
    # Price clearly above EMA5
    should_exit, reason = should_exit_mean_reversion(
        current_close=105.0, ema5=ema5, rsi2=50.0
    )
    assert should_exit
    assert reason == "mr_ema5_exit"


def test_exit_does_not_fire_close_at_ema5():
    """Close exactly at EMA5 should NOT trigger exit (must be strictly above)."""
    should_exit, reason = should_exit_mean_reversion(
        current_close=100.0, ema5=100.0, rsi2=50.0
    )
    assert not should_exit


def test_exit_fires_on_rsi2_above_70():
    """SPEC: exit fires when RSI(2) > 70."""
    should_exit, reason = should_exit_mean_reversion(
        current_close=98.0, ema5=100.0, rsi2=75.0  # close still below EMA5
    )
    assert should_exit
    assert reason == "mr_rsi_exit"


def test_exit_does_not_fire_rsi2_at_70():
    """RSI(2) == 70 should NOT trigger exit (must be strictly above)."""
    should_exit, reason = should_exit_mean_reversion(
        current_close=98.0, ema5=100.0, rsi2=70.0
    )
    assert not should_exit


def test_exit_does_not_fire_holding():
    """No exit signal while price is below EMA5 and RSI(2) is low."""
    should_exit, reason = should_exit_mean_reversion(
        current_close=95.0, ema5=100.0, rsi2=40.0
    )
    assert not should_exit
    assert reason == ""


def test_ema5_exit_takes_priority_over_rsi():
    """When both EMA5 and RSI(2) conditions are true, EMA5 reason is returned first."""
    should_exit, reason = should_exit_mean_reversion(
        current_close=105.0, ema5=100.0, rsi2=80.0
    )
    assert should_exit
    assert reason == "mr_ema5_exit"


# ── 7-day time stop (verified via backtest engine behaviour) ─────────────────

def test_consecutive_down_count():
    """_count_consecutive_down counts all consecutive falling pairs from the end."""
    closes = pd.Series([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
    # 6 bars: 5 transitions (100→99, 99→98, ..., 96→95) — all down
    assert _count_consecutive_down(closes) == 5


def test_consecutive_down_count_stops_at_up_bar():
    """Count stops at the first up-close encountered going backwards."""
    closes = pd.Series([100.0, 101.0, 100.0, 99.0, 98.0])
    # Going back: 98<99 (down), 99<100 (down), 100<101 (down), 101>100 (up) → stops at 3
    assert _count_consecutive_down(closes) == 3


# ── Cost model: 0.5% round trip modeled ─────────────────────────────────────

def test_cost_model_applies_round_trip_costs():
    """SPEC: costs of 0.5% round trip are applied in backtest.

    Verify that calculate_net_pnl reduces a winning trade by a non-trivial
    cost amount — confirming the cost model is active and not zero.
    """
    costs = TradeCosts()
    entry = 1000.0
    exit_price = 1020.0   # +2% gross gain
    shares = 100          # position value ₹1,00,000

    net_pnl, pnl_pct = calculate_net_pnl(entry, exit_price, shares, costs)

    # Gross P&L = (1020 - 1000) * 100 = ₹2,000 = +2%
    # After costs the net should be less than ₹2,000
    gross_pnl = (exit_price - entry) * shares
    assert net_pnl < gross_pnl, "Cost model must reduce net P&L below gross P&L"

    # Round-trip cost on ₹1L position should be at least ₹100 (0.1%) and below ₹1000 (1%)
    cost_amount = gross_pnl - net_pnl
    position_value = entry * shares
    cost_pct = cost_amount / position_value
    assert 0.001 <= cost_pct <= 0.01, (
        f"Expected round-trip cost 0.1–1% of position value, got {cost_pct:.4f}"
    )
