"""Historical regime series for backtesting.

Replicates RegimeDetector._classify() point-in-time so the backtest applies
the same regime gate as the live bot: BEAR blocks all entries, CAUTIOUS
halves position sizes, upgrades need 2-day confirmation while downgrades
are immediate.

Classification and size multipliers are the live functions themselves
(regime_detector.classify_regime / regime_position_multiplier), so the rules cannot
drift apart. Two documented differences from live:
- VIX is optional. Pass the India VIX close series (backtest/data_loader.load_india_vix)
  to apply the VIX tiers: EXTREME_VOLATILITY (> 35) and HIGH_VOLATILITY (25-35), and
  the BULL vix_calm condition (< 20). Without it those terms are omitted and the gate
  is slightly MORE permissive than live. Day T uses VIX's T-1 close; live reads the
  current VIX at each check.
- Breadth is computed over the symbols loaded in the backtest cache rather
  than the live universe (survivorship is handled upstream by PointInTimeUniverse).
- Upgrades are confirmed on 2 consecutive trading days; live confirms on 2
  different calendar days.

Look-ahead safety: the regime assigned to trading day T is classified from
data through day T-1's close — matching the live bot, which detects the
regime at 8:35 AM before the market opens.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from config.config import config
from regime.regime_detector import Regime, classify_regime, regime_position_multiplier, regime_rank

logger = logging.getLogger("backtest.regime")

def build_regime_series(
    ohlcv_by_symbol: dict[str, pd.DataFrame],
    nifty_df: pd.DataFrame,
    trading_days: list[date],
    breadth: pd.Series | None = None,
    vix: pd.Series | None = None,
) -> dict[date, tuple[str, float]]:
    """Build a per-day regime mapping for the backtest period.

    Args:
        ohlcv_by_symbol: Symbol → OHLCV DataFrame (date-indexed), used for breadth.
        nifty_df:        Nifty 50 OHLCV with enough warmup history for EMA200.
        trading_days:    Backtest trading days (ascending).
        breadth:         Precomputed breadth on the Nifty index (fast engine);
                         when given, ohlcv_by_symbol is ignored.
        vix:             India VIX close, date-indexed; None = VIX-less gate.

    Returns:
        Dict mapping trading day → (regime_name, position_size_multiplier).
    """
    nifty_close = nifty_df["close"].astype(float)
    span = config.REGIME_EMA_DAYS
    nifty_ema200 = nifty_close.ewm(span=span, adjust=False, min_periods=span).mean()

    # Nifty trending up: current N-day high > the N-day high before it
    hd = config.REGIME_TREND_HIGH_DAYS
    high_20d = nifty_close.rolling(hd).max()
    trending_up = high_20d > high_20d.shift(hd)

    # Breadth: fraction of universe stocks above their own 200-day EMA,
    # aligned on the Nifty's date index
    if breadth is None:
        above_flags = {}
        for symbol, df in ohlcv_by_symbol.items():
            close = df["close"].astype(float)
            if len(close) < span:
                continue
            ema200 = close.ewm(span=span, adjust=False, min_periods=span).mean()
            above_flags[symbol] = (close > ema200).reindex(nifty_close.index)
        if above_flags:
            breadth = (pd.DataFrame(above_flags).mean(axis=1, skipna=True)
                       .fillna(config.REGIME_BREADTH_NEUTRAL))
        else:
            breadth = pd.Series(config.REGIME_BREADTH_NEUTRAL, index=nifty_close.index)

    # VIX close aligned to the Nifty dates (last known value carried forward)
    if vix is not None:
        vix_on = vix.astype(float).reindex(nifty_close.index, method="ffill")
    else:
        vix_on = pd.Series(float("nan"), index=nifty_close.index)

    # Raw daily classification with the live rules, from each date's close
    raw: dict[date, tuple[str, float | None]] = {}
    for ts, c, e, b, t, v in zip(nifty_close.index, nifty_close.to_numpy(),
                                 nifty_ema200.to_numpy(), breadth.reindex(nifty_close.index).to_numpy(),
                                 trending_up.to_numpy(), vix_on.to_numpy()):
        v = None if pd.isna(v) else float(v)
        if pd.isna(e):
            r = Regime.CAUTIOUS           # insufficient EMA warmup → neutral
        else:
            r = classify_regime(float(c), float(e), float(b), v, trending_up=bool(t))
        raw[ts.date()] = (r.value, v)

    # Assign day T the classification from day T-1's close (no look-ahead),
    # then apply the live hysteresis: downgrades immediate, upgrades need the
    # same signal on 2 consecutive trading days.
    dates = list(raw)
    prior = {dates[i + 1]: raw[dates[i]] for i in range(len(dates) - 1)}

    result: dict[date, tuple[str, float]] = {}
    current = Regime.CAUTIOUS.value
    current_vix: float | None = None
    pending: str | None = None
    for day in trading_days:
        signal, day_vix = prior.get(day, (current, current_vix))
        if regime_rank(Regime(signal)) > regime_rank(Regime(current)):
            if pending == signal:
                current = signal   # 2nd day with the same upgrade signal → confirm
                pending = None
            else:
                pending = signal   # hold previous regime, start confirmation
        else:
            current = signal       # downgrade or unchanged — immediate
            pending = None
        current_vix = day_vix
        result[day] = (current, regime_position_multiplier(Regime(current), day_vix))

    counts = pd.Series([r for r, _ in result.values()]).value_counts()
    logger.info("Regime series built: %s", counts.to_dict())
    return result
