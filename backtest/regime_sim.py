"""Historical regime series for backtesting.

Replicates RegimeDetector._classify() point-in-time so the backtest applies
the same regime gate as the live bot: BEAR blocks all entries, CAUTIOUS
halves position sizes, upgrades need 2-day confirmation while downgrades
are immediate.

Two documented differences from live:
- India VIX history is not stored in market_data.db, so the VIX terms are
  omitted: EXTREME_VOLATILITY and HIGH_VOLATILITY can never trigger, and the
  BULL vix_calm condition passes by default. Net effect: the simulated gate
  is slightly MORE permissive than live (it never blocks on volatility
  spikes), so backtest results remain an upper bound in that dimension.
- Breadth is computed over the symbols loaded in the backtest cache rather
  than the full point-in-time Nifty 200 (survivorship is handled upstream
  by PointInTimeUniverse).

Look-ahead safety: the regime assigned to trading day T is classified from
data through day T-1's close — matching the live bot, which detects the
regime at 8:35 AM before the market opens.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from config.config import config

logger = logging.getLogger("backtest.regime")

# Multipliers mirror RegimeDetector._position_multiplier (VIX-less)
REGIME_MULTIPLIER = {
    "BEAR": 0.0,
    "CAUTIOUS": 0.5,
    "BULL": 1.0,
}

# Higher rank = better regime; upgrades need 2-day confirmation
_RANK = {"BEAR": 0, "CAUTIOUS": 1, "BULL": 2}


def build_regime_series(
    ohlcv_by_symbol: dict[str, pd.DataFrame],
    nifty_df: pd.DataFrame,
    trading_days: list[date],
) -> dict[date, tuple[str, float]]:
    """Build a per-day regime mapping for the backtest period.

    Args:
        ohlcv_by_symbol: Symbol → OHLCV DataFrame (date-indexed), used for breadth.
        nifty_df:        Nifty 50 OHLCV with enough warmup history for EMA200.
        trading_days:    Backtest trading days (ascending).

    Returns:
        Dict mapping trading day → (regime_name, position_size_multiplier).
    """
    nifty_close = nifty_df["close"].astype(float)
    nifty_ema200 = nifty_close.ewm(span=200, adjust=False, min_periods=200).mean()

    # Nifty trending up: current 20d high > 20d high of 4 weeks ago
    high_20d = nifty_close.rolling(20).max()
    trending_up = high_20d > high_20d.shift(20)

    # Breadth: fraction of universe stocks above their own 200-day EMA,
    # aligned on the Nifty's date index
    above_flags = {}
    for symbol, df in ohlcv_by_symbol.items():
        close = df["close"].astype(float)
        if len(close) < 200:
            continue
        ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
        above_flags[symbol] = (close > ema200).reindex(nifty_close.index)
    if above_flags:
        breadth = pd.DataFrame(above_flags).mean(axis=1, skipna=True).fillna(0.5)
    else:
        breadth = pd.Series(0.5, index=nifty_close.index)  # neutral when no data

    # Raw daily classification (VIX-less _classify)
    price_gap_pct = (nifty_close - nifty_ema200) / nifty_ema200
    raw = pd.Series("CAUTIOUS", index=nifty_close.index)
    is_bull = (
        (nifty_close > nifty_ema200)
        & (breadth >= config.BULL_BREADTH_THRESHOLD)
        & trending_up.fillna(False)
    )
    raw[is_bull] = "BULL"
    is_bear = (breadth < config.BEAR_BREADTH_THRESHOLD) | (price_gap_pct < -0.03)
    raw[is_bear] = "BEAR"
    raw[nifty_ema200.isna()] = "CAUTIOUS"  # insufficient EMA warmup → neutral

    # Assign day T the classification from day T-1's close (no look-ahead),
    # then apply the live hysteresis: downgrades immediate, upgrades need the
    # same signal on 2 consecutive trading days.
    raw_shifted = raw.shift(1)
    raw_by_date = {ts.date(): r for ts, r in raw_shifted.items() if isinstance(r, str)}

    result: dict[date, tuple[str, float]] = {}
    current = "CAUTIOUS"
    pending: str | None = None
    for day in trading_days:
        signal = raw_by_date.get(day, current)
        if _RANK[signal] > _RANK[current]:
            if pending == signal:
                current = signal   # 2nd day with the same upgrade signal → confirm
                pending = None
            else:
                pending = signal   # hold previous regime, start confirmation
        else:
            current = signal       # downgrade or unchanged — immediate
            pending = None
        result[day] = (current, REGIME_MULTIPLIER[current])

    counts = pd.Series([r for r, _ in result.values()]).value_counts()
    logger.info("Regime series built: %s", counts.to_dict())
    return result
