"""Backtest performance analysis and reporting.

Computes all required metrics:
Total Return, CAGR, Win Rate, Profit Factor, Sharpe, Sortino,
Max Drawdown, Avg Win/Loss, Monthly & Annual returns, Rolling Sharpe.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.perf")


@dataclass
class BacktestMetrics:
    start_date: date
    end_date: date
    starting_capital: float
    ending_capital: float

    # Returns
    total_return_pct: float
    cagr_pct: float

    # Trade stats
    total_trades: int
    win_count: int
    loss_count: int
    win_rate_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: float
    avg_holding_days: float

    # Risk metrics
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    calmar_ratio: float

    # Extremes
    best_trade_pct: float
    worst_trade_pct: float
    max_consecutive_wins: int
    max_consecutive_losses: int

    # Exposure
    exposure_time_pct: float   # fraction of days with open positions

    # Time series (for charts)
    monthly_returns: dict = field(default_factory=dict)    # "YYYY-MM" → pct
    annual_returns: dict = field(default_factory=dict)     # "YYYY" → pct

    # Diagnostics (Step 4 additions)
    exit_reasons: dict = field(default_factory=dict)       # reason → count
    expectancy_prob: float = 0.0    # P(mean trade P&L > 0) from 10k bootstrap resamples
    strategy_breakdown: dict = field(default_factory=dict) # strategy → {trades, win_rate, pnl, ...}


def analyse(
    trades: list[dict],
    equity_curve: pd.Series,
    starting_capital: float,
    start_date: date,
    end_date: date,
) -> BacktestMetrics:
    """Compute all performance metrics from a list of completed trades.

    Args:
        trades:           List of dicts with keys: entry_date, exit_date,
                          entry_price, exit_price, shares, realised_pnl, realised_pnl_pct.
        equity_curve:     Daily portfolio value Series with DatetimeIndex.
        starting_capital: Initial capital.
        start_date:       Backtest start date.
        end_date:         Backtest end date.

    Returns:
        BacktestMetrics dataclass with all computed values.
    """
    if not trades:
        logger.warning("No trades to analyse")
        return _empty_metrics(starting_capital, start_date, end_date)

    trades_df = pd.DataFrame(trades)
    ending_capital = float(equity_curve.iloc[-1]) if not equity_curve.empty else starting_capital

    total_return = (ending_capital - starting_capital) / starting_capital
    years = max((end_date - start_date).days / 365.25, 0.01)
    cagr = (ending_capital / starting_capital) ** (1 / years) - 1

    wins = trades_df[trades_df["realised_pnl"] > 0]
    losses = trades_df[trades_df["realised_pnl"] <= 0]
    total_trades = len(trades_df)
    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    avg_win = float(wins["realised_pnl_pct"].mean()) if not wins.empty else 0.0
    avg_loss = float(losses["realised_pnl_pct"].mean()) if not losses.empty else 0.0

    gross_wins = float(wins["realised_pnl"].sum()) if not wins.empty else 0.0
    gross_losses = abs(float(losses["realised_pnl"].sum())) if not losses.empty else 0.0
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    avg_holding = float(trades_df.apply(
        lambda r: (
            pd.Timestamp(r["exit_date"]) - pd.Timestamp(r["entry_date"])
        ).days, axis=1
    ).mean()) if total_trades > 0 else 0.0

    # Risk metrics from equity curve
    sharpe = _calc_sharpe(equity_curve)
    sortino = _calc_sortino(equity_curve)
    max_dd, max_dd_days = _calc_max_drawdown(equity_curve)
    calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0

    best_trade = float(trades_df["realised_pnl_pct"].max()) if total_trades > 0 else 0.0
    worst_trade = float(trades_df["realised_pnl_pct"].min()) if total_trades > 0 else 0.0

    max_cons_wins = _max_consecutive(trades_df["realised_pnl"] > 0)
    max_cons_losses = _max_consecutive(trades_df["realised_pnl"] <= 0)

    # Exposure time (days with open positions / total days)
    exposure = _calc_exposure(trades_df, start_date, end_date)

    # Monthly and annual returns from equity curve
    monthly_returns = _calc_monthly_returns(equity_curve, starting_capital)
    annual_returns = _calc_annual_returns(equity_curve, starting_capital)

    # Diagnostic fields
    exit_reasons = (
        trades_df["exit_reason"].value_counts().to_dict()
        if "exit_reason" in trades_df.columns else {}
    )
    expectancy_prob = _bootstrap_expectancy_prob(trades_df["realised_pnl"].values)
    strategy_breakdown = _strategy_breakdown(trades_df)

    return BacktestMetrics(
        start_date=start_date,
        end_date=end_date,
        starting_capital=starting_capital,
        ending_capital=ending_capital,
        total_return_pct=total_return * 100,
        cagr_pct=cagr * 100,
        total_trades=total_trades,
        win_count=len(wins),
        loss_count=len(losses),
        win_rate_pct=win_rate * 100,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        profit_factor=profit_factor,
        avg_holding_days=avg_holding,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown_pct=max_dd * 100,
        max_drawdown_duration_days=max_dd_days,
        calmar_ratio=calmar,
        best_trade_pct=best_trade,
        worst_trade_pct=worst_trade,
        max_consecutive_wins=max_cons_wins,
        max_consecutive_losses=max_cons_losses,
        exposure_time_pct=exposure * 100,
        monthly_returns=monthly_returns,
        annual_returns=annual_returns,
        exit_reasons=exit_reasons,
        expectancy_prob=expectancy_prob,
        strategy_breakdown=strategy_breakdown,
    )


def print_report(metrics: BacktestMetrics) -> None:
    """Print a formatted backtest summary report."""
    print("\n" + "=" * 60)
    print(f"BACKTEST RESULTS: {metrics.start_date} → {metrics.end_date}")
    print("=" * 60)
    print(f"Starting Capital : ₹{metrics.starting_capital:>12,.0f}")
    print(f"Ending Capital   : ₹{metrics.ending_capital:>12,.0f}")
    print(f"Total Return     : {metrics.total_return_pct:>+8.2f}%")
    print(f"CAGR             : {metrics.cagr_pct:>+8.2f}%")
    print()
    print(f"Total Trades     : {metrics.total_trades:>8}")
    print(f"Win Rate         : {metrics.win_rate_pct:>8.1f}%  ({metrics.win_count}W / {metrics.loss_count}L)")
    print(f"Avg Win          : {metrics.avg_win_pct:>+8.2f}%")
    print(f"Avg Loss         : {metrics.avg_loss_pct:>+8.2f}%")
    print(f"Profit Factor    : {metrics.profit_factor:>8.2f}")
    print(f"Avg Holding Days : {metrics.avg_holding_days:>8.1f}")
    print()
    print(f"Sharpe Ratio     : {metrics.sharpe_ratio:>8.2f}")
    print(f"Sortino Ratio    : {metrics.sortino_ratio:>8.2f}")
    print(f"Max Drawdown     : {metrics.max_drawdown_pct:>+8.2f}%")
    print(f"Max DD Duration  : {metrics.max_drawdown_duration_days:>8} days")
    print(f"Calmar Ratio     : {metrics.calmar_ratio:>8.2f}")
    print()
    print(f"Best Trade       : {metrics.best_trade_pct:>+8.2f}%")
    print(f"Worst Trade      : {metrics.worst_trade_pct:>+8.2f}%")
    print(f"Max Consec. Wins : {metrics.max_consecutive_wins:>8}")
    print(f"Max Consec. Loss : {metrics.max_consecutive_losses:>8}")
    print(f"Exposure Time    : {metrics.exposure_time_pct:>8.1f}%")

    # Exit reason distribution
    if metrics.exit_reasons:
        print("\nExit Reasons:")
        for reason, count in sorted(metrics.exit_reasons.items(), key=lambda x: -x[1]):
            pct = count / metrics.total_trades * 100
            print(f"  {reason:<25} {count:>4}  ({pct:.0f}%)")

    # Bootstrap confidence
    if not math.isnan(metrics.expectancy_prob):
        flag = "  ✓ EDGE CONFIRMED" if metrics.expectancy_prob >= 0.80 else "  ✗ EDGE NOT CONFIRMED"
        print(f"\nBootstrap P(E>0)  : {metrics.expectancy_prob:>7.1%}  (10,000 resamples){flag}")

    # Per-strategy breakdown (shown when --strategy all)
    if metrics.strategy_breakdown:
        print("\nPer-Strategy Breakdown:")
        hdr = f"  {'Strategy':<20} {'Trades':>6}  {'WR':>5}  {'PF':>5}  {'PnL (₹)':>10}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for strat, s in sorted(metrics.strategy_breakdown.items()):
            pf_str = f"{s['profit_factor']:.2f}" if not math.isinf(s["profit_factor"]) else "  inf"
            print(f"  {strat:<20} {s['trades']:>6}  {s['win_rate']:>4.0f}%  {pf_str:>5}  {s['total_pnl']:>10,.0f}")

    if metrics.annual_returns:
        print("\nAnnual Returns:")
        for year, ret in sorted(metrics.annual_returns.items()):
            bar = "+" * int(abs(ret) / 5) if ret >= 0 else "-" * int(abs(ret) / 5)
            print(f"  {year}: {ret:>+7.2f}%  {bar}")
    print("=" * 60)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _bootstrap_expectancy_prob(pnl_values: np.ndarray, n_samples: int = 10_000) -> float:
    """P(mean trade P&L > 0) by bootstrap resampling.

    Resamples the observed trade list 10,000 times and measures what fraction
    of bootstrap samples have a positive mean P&L. ≥80% is the acceptance threshold.
    """
    if len(pnl_values) < 5:
        return float("nan")
    rng = np.random.default_rng(42)
    sample_means = rng.choice(pnl_values, size=(n_samples, len(pnl_values)), replace=True).mean(axis=1)
    return float((sample_means > 0).mean())


def _strategy_breakdown(trades_df: pd.DataFrame) -> dict:
    """Per-strategy P&L summary when trades carry a 'strategy' tag."""
    if "strategy" not in trades_df.columns:
        return {}
    result: dict = {}
    for strat, group in trades_df.groupby("strategy"):
        wins = group[group["realised_pnl"] > 0]
        losses = group[group["realised_pnl"] <= 0]
        gross_w = float(wins["realised_pnl"].sum()) if not wins.empty else 0.0
        gross_l = abs(float(losses["realised_pnl"].sum())) if not losses.empty else 0.0
        result[strat] = {
            "trades": len(group),
            "win_rate": len(wins) / len(group) * 100 if len(group) > 0 else 0.0,
            "total_pnl": float(group["realised_pnl"].sum()),
            "avg_win": float(wins["realised_pnl_pct"].mean()) if not wins.empty else 0.0,
            "avg_loss": float(losses["realised_pnl_pct"].mean()) if not losses.empty else 0.0,
            "profit_factor": gross_w / gross_l if gross_l > 0 else float("inf"),
        }
    return result


def _calc_sharpe(equity_curve: pd.Series, risk_free: float = 0.06) -> float:
    """Annualised Sharpe ratio (assuming 252 trading days, 6% risk-free rate)."""
    if len(equity_curve) < 2:
        return 0.0
    returns = equity_curve.pct_change().dropna()
    excess = returns - (risk_free / 252)
    std = float(returns.std())
    if std == 0:
        return 0.0
    return float(excess.mean() / std) * math.sqrt(252)


def _calc_sortino(equity_curve: pd.Series, risk_free: float = 0.06) -> float:
    """Annualised Sortino ratio (downside deviation only)."""
    if len(equity_curve) < 2:
        return 0.0
    returns = equity_curve.pct_change().dropna()
    excess = returns - (risk_free / 252)
    downside = returns[returns < 0]
    downside_std = float(downside.std()) if len(downside) > 1 else 0.0
    if downside_std == 0:
        return 0.0
    return float(excess.mean() / downside_std) * math.sqrt(252)


def _calc_max_drawdown(equity_curve: pd.Series) -> tuple[float, int]:
    """Return (max_drawdown_fraction, max_drawdown_duration_days)."""
    if equity_curve.empty:
        return 0.0, 0
    rolling_peak = equity_curve.cummax()
    drawdown = (equity_curve - rolling_peak) / rolling_peak
    max_dd = float(drawdown.min())

    # Duration: longest consecutive stretch below previous peak
    below_peak = drawdown < 0
    max_duration = 0
    current_duration = 0
    for v in below_peak:
        if v:
            current_duration += 1
            max_duration = max(max_duration, current_duration)
        else:
            current_duration = 0

    return max_dd, max_duration


def _max_consecutive(bool_series: pd.Series) -> int:
    """Return the maximum run of True values in a boolean series."""
    max_run = current = 0
    for v in bool_series:
        if v:
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return max_run


def _calc_exposure(trades_df: pd.DataFrame, start: date, end: date) -> float:
    """Fraction of calendar days with at least one open position."""
    total_days = max((end - start).days, 1)
    if trades_df.empty:
        return 0.0
    exposure_days = int(trades_df.apply(
        lambda r: (pd.Timestamp(r["exit_date"]) - pd.Timestamp(r["entry_date"])).days,
        axis=1,
    ).sum())
    return min(1.0, exposure_days / total_days)


def _calc_monthly_returns(equity_curve: pd.Series, starting_capital: float) -> dict:
    if equity_curve.empty:
        return {}
    monthly = equity_curve.resample("ME").last()
    prev = monthly.shift(1).fillna(starting_capital)
    returns = (monthly - prev) / prev
    return {str(idx.strftime("%Y-%m")): round(float(v) * 100, 2) for idx, v in returns.items()}


def _calc_annual_returns(equity_curve: pd.Series, starting_capital: float) -> dict:
    if equity_curve.empty:
        return {}
    annual = equity_curve.resample("YE").last()
    prev = annual.shift(1).fillna(starting_capital)
    returns = (annual - prev) / prev
    return {str(idx.year): round(float(v) * 100, 2) for idx, v in returns.items()}


def _empty_metrics(capital: float, start: date, end: date) -> BacktestMetrics:
    return BacktestMetrics(
        start_date=start, end_date=end,
        starting_capital=capital, ending_capital=capital,
        total_return_pct=0, cagr_pct=0, total_trades=0,
        win_count=0, loss_count=0, win_rate_pct=0,
        avg_win_pct=0, avg_loss_pct=0, profit_factor=0,
        avg_holding_days=0, sharpe_ratio=0, sortino_ratio=0,
        max_drawdown_pct=0, max_drawdown_duration_days=0,
        calmar_ratio=0, best_trade_pct=0, worst_trade_pct=0,
        max_consecutive_wins=0, max_consecutive_losses=0,
        exposure_time_pct=0,
    )
