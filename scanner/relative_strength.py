"""O'Neil Relative Strength calculation.

RS Score = weighted multi-period performance vs Nifty 200 benchmark.
Only the top 30% (>70th percentile) are considered; prefer >85th percentile.

Lookback skips the most recent 21 trading days (~1 month) to avoid the
well-documented short-term reversal effect (Jegadeesh & Titman 1993).
Periods are measured from 21 days ago, not from today.

Weights:
  Months 1–4  (days 21–84):   40%
  Months 4–7  (days 84–147):  20%
  Months 7–10 (days 147–210): 20%
  Months 10–12 (days 210–252): 20%
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from config.config import config

logger = logging.getLogger("scanner.rs")


def _period_perf(returns: pd.Series, start_bar: int, end_bar: int) -> float:
    """Compound return for bars [start_bar, end_bar] counting back from most recent.

    Args:
        returns:    Daily return series (pct_change format).
        start_bar:  Bars from end to start the segment (0 = most recent).
        end_bar:    Bars from end to stop the segment.

    Returns:
        Compound return as a decimal (0.10 = 10%), or 0 if insufficient data.
    """
    n = len(returns)
    if n < end_bar:
        return 0.0

    if start_bar == 0:
        segment = returns.iloc[-end_bar:]
    else:
        segment = returns.iloc[-end_bar:-start_bar]

    if segment.empty:
        return 0.0

    return float((1 + segment).prod() - 1)


def calculate_raw_rs(
    stock_returns: pd.Series,
    benchmark_returns: pd.Series,
) -> float:
    """Calculate raw (unranked) RS score for one stock vs benchmark.

    Args:
        stock_returns:     Daily return series for the stock.
        benchmark_returns: Daily return series for Nifty 200.

    Returns:
        Raw RS score (excess return vs benchmark, weighted by period).
        Higher = better relative performance.
    """
    # Skip most recent 21 days (1 month) to avoid short-term reversal bias
    s1 = _period_perf(stock_returns, 21, 84)
    s2 = _period_perf(stock_returns, 84, 147)
    s3 = _period_perf(stock_returns, 147, 210)
    s4 = _period_perf(stock_returns, 210, 252)

    b1 = _period_perf(benchmark_returns, 21, 84)
    b2 = _period_perf(benchmark_returns, 84, 147)
    b3 = _period_perf(benchmark_returns, 147, 210)
    b4 = _period_perf(benchmark_returns, 210, 252)

    weighted_stock = 0.4 * s1 + 0.2 * s2 + 0.2 * s3 + 0.2 * s4
    weighted_bench = 0.4 * b1 + 0.2 * b2 + 0.2 * b3 + 0.2 * b4

    return weighted_stock - weighted_bench


def calculate_rs_universe(
    universe_ohlcv: dict[str, pd.DataFrame],
    benchmark_ohlcv: pd.DataFrame,
) -> dict[str, float]:
    """Calculate RS scores for all symbols and return percentile ranks (0–99).

    Args:
        universe_ohlcv:  Dict mapping symbol → daily OHLCV DataFrame.
        benchmark_ohlcv: Nifty 200 daily OHLCV DataFrame.

    Returns:
        Dict mapping symbol → RS percentile rank (0–99).
    """
    bench_returns = benchmark_ohlcv["close"].pct_change().dropna()
    raw_scores: dict[str, float] = {}

    for symbol, df in universe_ohlcv.items():
        if len(df) < 252:
            continue
        stock_returns = df["close"].pct_change().dropna()
        raw_scores[symbol] = calculate_raw_rs(stock_returns, bench_returns)

    if not raw_scores:
        return {}

    # Percentile rank across the universe
    scores_series = pd.Series(raw_scores)
    percentile_ranks = scores_series.rank(pct=True) * 99

    return percentile_ranks.to_dict()


def passes_rs_filter(
    rs_percentile: float,
    min_percentile: float = None,
) -> bool:
    """Return True if the RS score meets the minimum threshold.

    Args:
        rs_percentile: RS percentile rank (0–99).
        min_percentile: Minimum required percentile (default from config).
    """
    min_pct = min_percentile or config.MIN_RS_PERCENTILE
    return rs_percentile >= min_pct
