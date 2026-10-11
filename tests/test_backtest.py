"""Smoke tests for the backtest engine and performance analyzer."""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.performance_analyzer import BacktestMetrics, analyse, _calc_max_drawdown, _calc_sharpe, _bootstrap_expectancy_prob
from backtest.backtest_engine import BacktestEngine
from backtest.cost_model import calculate_buy_cost, calculate_sell_cost
from portfolio.pnl_calculator import TradeCosts, calculate_net_pnl


# ── Performance Analyzer Tests ────────────────────────────────────────────────

def make_equity_curve(n: int = 252, start: float = 500_000, cagr: float = 0.20) -> pd.Series:
    """Synthetic equity curve with smooth uptrend."""
    daily_ret = (1 + cagr) ** (1 / 252) - 1
    values = start * (1 + daily_ret) ** np.arange(n)
    dates = pd.date_range(end=date.today(), periods=n, freq="B")
    return pd.Series(values, index=dates)


def make_trades(n: int = 20, win_rate: float = 0.6) -> list[dict]:
    """Synthetic trade list."""
    from datetime import timedelta
    trades = []
    base = date(2023, 1, 1)
    for i in range(n):
        is_win = i < int(n * win_rate)
        entry = base + timedelta(days=i * 15)
        exit_ = base + timedelta(days=i * 15 + 10)
        entry_price = 1000.0
        exit_price = 1150.0 if is_win else 930.0
        net_pnl, pnl_pct = calculate_net_pnl(entry_price, exit_price, 100)
        trades.append({
            "symbol": f"STOCK{i:02d}",
            "entry_date": entry,
            "exit_date": exit_,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "shares": 100,
            "realised_pnl": net_pnl,
            "realised_pnl_pct": pnl_pct * 100,
            "exit_reason": "trailing_stop",
        })
    return trades


def test_performance_analysis_runs():
    equity = make_equity_curve()
    trades = make_trades()
    metrics = analyse(
        trades=trades,
        equity_curve=equity,
        starting_capital=500_000,
        start_date=date(2023, 1, 1),
        end_date=date(2024, 1, 1),
    )
    assert isinstance(metrics, BacktestMetrics)
    assert metrics.total_trades == 20
    assert 0 < metrics.win_rate_pct < 100


def test_cagr_calculation():
    equity = make_equity_curve(n=252, start=500_000, cagr=0.20)
    trades = make_trades(n=10)
    metrics = analyse(trades, equity, 500_000, date(2023, 1, 1), date(2024, 1, 1))
    # CAGR should roughly reflect the equity curve's 20% growth
    assert 15.0 < metrics.cagr_pct < 25.0  # rough range accounting for trade P&L


def test_max_drawdown_calculation():
    """Equity that drops 10% then recovers should show ~10% max drawdown."""
    n = 100
    values = [100_000.0] * n
    # Add a 10% drawdown in the middle
    for i in range(40, 60):
        values[i] = 100_000 * 0.90
    equity = pd.Series(values, index=pd.date_range(end=date.today(), periods=n, freq="B"))
    dd, duration = _calc_max_drawdown(equity)
    assert -0.12 < dd < -0.08  # roughly 10% drawdown


def test_sharpe_positive_trend():
    equity = make_equity_curve(cagr=0.20)
    sharpe = _calc_sharpe(equity)
    assert sharpe > 0  # positive trend should give positive Sharpe


def test_empty_trades_returns_zero_metrics():
    equity = make_equity_curve()
    metrics = analyse([], equity, 500_000, date(2023, 1, 1), date(2024, 1, 1))
    assert metrics.total_trades == 0


def test_profit_factor_calculation():
    trades = make_trades(n=20, win_rate=0.6)
    equity = make_equity_curve()
    metrics = analyse(trades, equity, 500_000, date(2023, 1, 1), date(2024, 1, 1))
    # With 60% win rate and ~15% wins vs 7% losses, PF should be > 1
    assert metrics.profit_factor > 0


def test_metrics_dataclass_fields():
    trades = make_trades(n=10)
    equity = make_equity_curve()
    metrics = analyse(trades, equity, 500_000, date(2023, 1, 1), date(2024, 1, 1))
    # All required fields must be present
    assert hasattr(metrics, "sharpe_ratio")
    assert hasattr(metrics, "sortino_ratio")
    assert hasattr(metrics, "max_drawdown_pct")
    assert hasattr(metrics, "win_rate_pct")
    assert hasattr(metrics, "cagr_pct")
    assert hasattr(metrics, "exposure_time_pct")
    assert hasattr(metrics, "exit_reasons")
    assert hasattr(metrics, "expectancy_prob")
    assert hasattr(metrics, "strategy_breakdown")


def test_exit_reason_distribution():
    """exit_reasons must count occurrences of each exit reason."""
    trades = make_trades(n=10)
    equity = make_equity_curve()
    metrics = analyse(trades, equity, 500_000, date(2023, 1, 1), date(2024, 1, 1))
    # All synthetic trades use "trailing_stop" as exit reason
    assert "trailing_stop" in metrics.exit_reasons
    assert metrics.exit_reasons["trailing_stop"] == 10


def test_bootstrap_expectancy_positive_edge():
    """Bootstrap P(E>0) should be near 1.0 for a clearly profitable trade set."""
    pnl = np.array([500.0, 800.0, 300.0, 600.0, 400.0, 700.0, 200.0, 900.0])
    prob = _bootstrap_expectancy_prob(pnl)
    assert prob > 0.95, f"Expected P(E>0) > 0.95 for all-positive trades, got {prob:.3f}"


def test_bootstrap_expectancy_negative_edge():
    """Bootstrap P(E>0) should be near 0.0 for a clearly losing trade set."""
    pnl = np.array([-500.0, -800.0, -300.0, -600.0, -400.0, -700.0, -200.0, -900.0])
    prob = _bootstrap_expectancy_prob(pnl)
    assert prob < 0.05, f"Expected P(E>0) < 0.05 for all-negative trades, got {prob:.3f}"


def test_bootstrap_expectancy_nan_for_tiny_sample():
    """Fewer than 5 trades should return NaN (not enough data to bootstrap)."""
    import math
    pnl = np.array([100.0, 200.0, -50.0])
    prob = _bootstrap_expectancy_prob(pnl)
    assert math.isnan(prob)


# ── Ledger invariant tests ────────────────────────────────────────────────────

def _make_engine_with_position(entry_price: float, shares: int, strategy: str = "breakout") -> BacktestEngine:
    """Helper: create a BacktestEngine and manually place one open position."""
    engine = BacktestEngine(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        starting_capital=500_000.0,
        strategy=strategy,
    )
    pos_value = entry_price * shares
    buy_cost = calculate_buy_cost(pos_value)
    total_outflow = pos_value + buy_cost
    engine.capital -= total_outflow
    engine._open_positions["TEST"] = {
        "symbol": "TEST",
        "entry_date": date(2025, 1, 2),
        "entry_price": entry_price,
        "shares": shares,
        "stop_loss": entry_price * 0.95,
        "initial_stop_loss": entry_price * 0.95,
        "vcp_low": None,
        "partial_sold": 0,
        "strategy": strategy,
        "target_price": entry_price * 1.10,
    }
    return engine


def _check_ledger(engine: BacktestEngine, tolerance: float = 1.0) -> None:
    total_pnl = sum(t["realised_pnl"] for t in engine._closed_trades)
    capital_change = engine.capital - engine.starting_capital
    error = abs(total_pnl - capital_change)
    assert error < tolerance, (
        f"Ledger mismatch: sum_pnl={total_pnl:.2f} capital_change={capital_change:.2f} err={error:.2f}"
    )


def test_ledger_invariant_winning_trade():
    """Single winning trade: sum(realised_pnl) == capital - starting_capital."""
    engine = _make_engine_with_position(entry_price=1000.0, shares=100)
    engine._close_position("TEST", exit_price=1100.0, exit_date=date(2025, 1, 10), reason="profit_target")
    _check_ledger(engine)


def test_ledger_invariant_losing_trade():
    """Single losing trade: ledger invariant holds even for losses."""
    engine = _make_engine_with_position(entry_price=1000.0, shares=100)
    engine._close_position("TEST", exit_price=950.0, exit_date=date(2025, 1, 10), reason="stop_loss")
    _check_ledger(engine)


def test_ledger_invariant_two_trades():
    """Two trades (one win, one loss): sum of both realised P&Ls matches capital change."""
    engine = BacktestEngine(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        starting_capital=500_000.0,
        strategy="breakout",
    )

    for symbol, ep, shares_count, strat in [
        ("AAA", 1000.0, 100, "breakout"),
        ("BBB", 500.0, 200, "momentum"),
    ]:
        pv = ep * shares_count
        bc = calculate_buy_cost(pv)
        engine.capital -= (pv + bc)
        engine._open_positions[symbol] = {
            "symbol": symbol, "entry_date": date(2025, 1, 2),
            "entry_price": ep, "shares": shares_count,
            "stop_loss": ep * 0.95, "initial_stop_loss": ep * 0.95,
            "vcp_low": None, "partial_sold": 0,
            "strategy": strat, "target_price": ep * 1.10,
        }

    engine._close_position("AAA", exit_price=1100.0, exit_date=date(2025, 1, 10), reason="profit_target")
    engine._close_position("BBB", exit_price=475.0, exit_date=date(2025, 1, 12), reason="stop_loss")

    _check_ledger(engine)


def test_strategy_tag_in_trade_record():
    """Closed trade dict must carry the 'strategy' tag from the position."""
    engine = _make_engine_with_position(entry_price=1000.0, shares=50, strategy="mean_reversion")
    engine._close_position("TEST", exit_price=1050.0, exit_date=date(2025, 1, 10), reason="mr_ema5_exit")
    assert engine._closed_trades[0]["strategy"] == "mean_reversion"


# ── Momentum time-stop pinning test ──────────────────────────────────────────

def test_momentum_time_stop_fires_at_12_days():
    """Momentum positions held >= MOMENTUM_TIME_STOP_DAYS must be closed via time_stop."""
    from config.config import config
    from config.holidays import get_trading_days

    engine = BacktestEngine(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 6, 30),
        starting_capital=500_000.0,
        strategy="momentum",
    )

    # Place a momentum position with an entry old enough to exceed the time stop
    # MOMENTUM_TIME_STOP_DAYS = 12 trading days
    entry = date(2025, 1, 2)
    ep = 1000.0
    shares = 100
    pv = ep * shares
    bc = calculate_buy_cost(pv)
    engine.capital -= (pv + bc)
    engine._open_positions["MTEST"] = {
        "symbol": "MTEST",
        "entry_date": entry,
        "entry_price": ep,
        "shares": shares,
        "stop_loss": ep * 0.95,
        "initial_stop_loss": ep * 0.95,
        "vcp_low": None,
        "partial_sold": 0,
        "strategy": "momentum",
        "target_price": ep * 1.12,
    }

    # Inject a minimal OHLCV cache entry so _process_exits can read a price
    trading_days = get_trading_days(date(2025, 1, 1), date(2025, 6, 30))
    # Build a flat price series
    idx = pd.date_range(start="2025-01-01", periods=150, freq="B")
    df = pd.DataFrame({
        "open": [ep] * 150, "high": [ep * 1.01] * 150,
        "low": [ep * 0.99] * 150, "close": [ep] * 150,
        "volume": [1_000_000] * 150,
    }, index=idx)
    engine._ohlcv_cache["MTEST"] = df

    # Simulate 14 trading days after entry (definitely past 12-day time stop)
    entry_idx = next(i for i, d in enumerate(trading_days) if d >= entry)
    sim_date = trading_days[entry_idx + 14]

    engine._process_exits(sim_date)

    # Position must be closed
    assert "MTEST" not in engine._open_positions, "Momentum position should have been time-stopped"
    assert len(engine._closed_trades) == 1
    assert engine._closed_trades[0]["exit_reason"] == "time_stop"
    assert engine._closed_trades[0]["strategy"] == "momentum"
