"""Tests for position sizer, stop loss manager, and P&L calculator."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from risk.position_sizer import calculate_position_size, calculate_stop_loss_price
from exit.stop_loss_manager import (
    TRAILING_RULES,
    calculate_hard_stop,
    calculate_trailing_stop,
    should_exit_on_stop,
    validate_stop_not_widened,
)
from portfolio.pnl_calculator import (
    TradeCosts,
    calculate_buy_cost,
    calculate_net_pnl,
    calculate_sell_cost,
    estimate_break_even_price,
)

import pandas as pd
import numpy as np
from datetime import date


def make_simple_ohlcv(n: int = 20, price: float = 500.0) -> pd.DataFrame:
    import pandas as pd
    dates = pd.date_range(end=date.today(), periods=n, freq="B")
    return pd.DataFrame({
        "open": [price * 0.99] * n,
        "high": [price * 1.01] * n,
        "low": [price * 0.98] * n,
        "close": [price] * n,
        "volume": [1_000_000] * n,
    }, index=dates)


# ── Position Sizer Tests ──────────────────────────────────────────────────────

def test_position_size_basic():
    result = calculate_position_size(
        portfolio_value=500_000.0,
        entry_price=500.0,
        stop_loss_price=465.0,   # 7% stop
    )
    assert result.is_valid
    assert result.shares > 0
    assert result.actual_risk_pct <= 0.015 + 0.001  # within 1.5% ± rounding


def test_position_size_capped_by_position_limit():
    """Entry at ₹100 with 1% risk per trade on large portfolio."""
    result = calculate_position_size(
        portfolio_value=1_000_000.0,
        entry_price=100.0,
        stop_loss_price=93.0,
    )
    assert result.is_valid
    # Position value should not exceed the configured cap (tracks the
    # sizing-study value rather than a hardcoded percentage)
    from config.config import config
    assert result.position_value <= 1_000_000.0 * config.MAX_POSITION_SIZE_PCT + 1  # +1 for rounding


def test_position_size_invalid_stop():
    result = calculate_position_size(
        portfolio_value=500_000.0,
        entry_price=500.0,
        stop_loss_price=510.0,  # stop ABOVE entry — invalid
    )
    assert not result.is_valid


def test_position_size_regime_multiplier():
    full = calculate_position_size(500_000.0, 500.0, 465.0, regime_multiplier=1.0)
    half = calculate_position_size(500_000.0, 500.0, 465.0, regime_multiplier=0.5)
    assert full.shares > 0
    assert half.shares > 0
    assert half.shares <= full.shares


def test_calculate_stop_loss_price():
    stop = calculate_stop_loss_price(entry_price=1000.0, stop_pct=0.07)
    assert 920.0 < stop < 940.0  # ~7% below ₹1000
    # Should be rounded to nearest 0.05
    assert stop % 0.05 < 0.01 or (0.05 - stop % 0.05) < 0.01


# ── Stop Loss Manager Tests ───────────────────────────────────────────────────

def test_hard_stop_calculation():
    stop = calculate_hard_stop(entry_price=1000.0)
    assert stop == pytest.approx(930.0, abs=1.0)


def test_trailing_stop_breakeven_at_10pct():
    df = make_simple_ohlcv(n=15, price=1100.0)
    new_stop, reason = calculate_trailing_stop(
        entry_price=1000.0,
        current_price=1100.0,   # 10% profit
        current_stop=930.0,
        daily_ohlcv=df,
    )
    assert new_stop >= 1000.0  # at breakeven or above
    assert reason != "ratchet_hold"


def test_trailing_stop_never_decreases():
    df = make_simple_ohlcv(n=15, price=1000.0)
    current_stop = 1050.0  # already trailed up
    new_stop, reason = calculate_trailing_stop(
        entry_price=1000.0,
        current_price=1000.0,   # pulled back
        current_stop=current_stop,
        daily_ohlcv=df,
    )
    assert new_stop >= current_stop  # ratchet: never go lower


def test_should_exit_on_stop():
    assert should_exit_on_stop(current_price=460.0, stop_price=465.0)
    assert not should_exit_on_stop(current_price=470.0, stop_price=465.0)


def test_validate_stop_not_widened_blocks(caplog):
    result = validate_stop_not_widened("TEST", current_stop=500.0, proposed_stop=490.0)
    assert not result


def test_validate_stop_not_widened_allows():
    result = validate_stop_not_widened("TEST", current_stop=500.0, proposed_stop=510.0)
    assert result


# ── P&L Calculator Tests ──────────────────────────────────────────────────────

def test_buy_cost_positive():
    cost = calculate_buy_cost(100_000.0)
    assert cost > 0
    # Approximate: should be around 0.3–0.5% of trade value
    assert 100_000 * 0.002 <= cost <= 100_000 * 0.01


def test_sell_cost_positive():
    cost = calculate_sell_cost(100_000.0)
    assert cost > 0


def test_net_pnl_profitable_trade():
    net_pnl, pnl_pct = calculate_net_pnl(
        entry_price=1000.0,
        exit_price=1150.0,
        shares=100,
    )
    assert net_pnl > 0
    assert pnl_pct > 0
    # Gross = 15,000, costs ~= 700, so net should be ~14,300
    assert 13_000 < net_pnl < 15_000


def test_net_pnl_losing_trade():
    net_pnl, pnl_pct = calculate_net_pnl(
        entry_price=1000.0,
        exit_price=930.0,
        shares=100,
    )
    assert net_pnl < 0
    assert pnl_pct < 0


def test_break_even_price_above_entry():
    be = estimate_break_even_price(entry_price=1000.0, shares=100)
    assert be > 1000.0  # must sell above entry to break even after costs


def test_round_trip_costs_reasonable():
    """Total round-trip cost should be roughly 0.3–0.6% of trade value."""
    entry = 1000.0
    shares = 100
    trade_value = entry * shares
    buy_cost = calculate_buy_cost(trade_value)
    sell_cost = calculate_sell_cost(trade_value)
    total_pct = (buy_cost + sell_cost) / trade_value
    assert 0.002 <= total_pct <= 0.01  # 0.2%–1%
