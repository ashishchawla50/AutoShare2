"""Live pre-scan stock filters, replayed on the reconstructed arrays.

Live (main.run_pre_scan) runs scanner/stock_filter.run_batch_filters before the
scan. A symbol that fails is dropped from that day's scan: no new pivot watch and
no breakout check (a watched pivot stays on the watch until it expires).

Only the filters that bhavcopy data can reproduce are replayed here, with live's
exact rules and data window (config.HISTORICAL_DAYS of candles up to yesterday,
checked on the morning of `today`):

  price          last close >= MIN_STOCK_PRICE, on the price that actually traded
                 (adjusted close / adj_factor; live's latest candle is unadjusted)
  liquidity      mean(volume) x mean(close) over the last LIQUIDITY_LOOKBACK_DAYS
                 candles >= MIN_DAILY_TRADED_VALUE_CR (fails on too little history).
                 The product is unchanged by split adjustment, so adjusted data is fine.
  circuit        more than MAX_CIRCUIT_HITS_60D closes moving > CIRCUIT_MOVE_PCT in the
                 last CIRCUIT_LOOKBACK_DAYS (passes on too little history)
  data_quality   more than MAX_MISSING_DAYS_90D trading days missing in the last
                 DATA_QUALITY_WINDOW_DAYS calendar days up to and including today, or
                 any high < low / close outside high-low in the loaded window.
                 Today itself is always missing (the pre-scan runs before the open),
                 exactly as live counts it. The other expected days are the days the
                 exchange actually traded (every date in the bhavcopy archive), not
                 config/holidays.py: that calendar has no 2021-2023 holidays and its 2026
                 list was partly wrong, which would count market holidays as gaps.

Not replayed: market cap, promoter pledge and D/E (no history), surveillance lists
(no history), and IPO age, which never fires live because Kite instruments carry no
listing date. Corporate events have their own --events-filter.

tests/test_fast_live_parity.py checks every verdict against the live functions.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from backtest.fast.data import MarketData
from config.config import config


class LiveFilters:
    """Per-candidate verdicts of live's bhavcopy-derivable pre-scan filters."""

    def __init__(self, md: MarketData, market_days: Optional[np.ndarray] = None) -> None:
        """market_days: sorted day numbers the exchange traded; default = every date in
        the dataset (the tests pass live's holiday calendar instead, to compare 1:1)."""
        self._md = md
        self._tdays = np.unique(md.dates) if market_days is None else np.asarray(market_days)
        bad = (md.high < md.low) | (md.close > md.high) | (md.close < md.low)
        self._bad_cum = np.concatenate(([0], np.cumsum(bad)))

    def check(self, first_row: int, last_row: int, today: int) -> str | None:
        """Name of the first failing filter (live order), or None if all pass.

        first_row: the security's first row; last_row: its latest candle before
        today (the scan candle); today: day number of the session being traded.
        """
        md = self._md
        d = md.dates
        # live loads config.HISTORICAL_DAYS calendar days of candles
        lo = first_row + int(np.searchsorted(d[first_row:last_row + 1],
                                             today - config.HISTORICAL_DAYS, side="left"))
        n = last_row - lo + 1

        if md.close[last_row] / md.adj[last_row] < config.MIN_STOCK_PRICE:
            return "price"

        k = config.LIQUIDITY_LOOKBACK_DAYS
        if n < k:
            return "liquidity"
        s = slice(last_row - k + 1, last_row + 1)
        if md.volume[s].mean() * md.close[s].mean() / 1e7 < config.MIN_DAILY_TRADED_VALUE_CR:
            return "liquidity"

        k = config.CIRCUIT_LOOKBACK_DAYS
        if n >= k + 1:
            c = md.close[last_row - k:last_row + 1]
            hits = int((np.abs(c[1:] / c[:-1] - 1) > config.CIRCUIT_MOVE_PCT).sum())
            if hits > config.MAX_CIRCUIT_HITS_60D:
                return "circuit_breaker"

        start = today - config.DATA_QUALITY_WINDOW_DAYS
        t = self._tdays
        expected = t[np.searchsorted(t, start, side="left"):np.searchsorted(t, today, side="left")]
        have = d[lo:last_row + 1]
        have = have[have >= start]
        missing = 1 + len(expected) - int(np.isin(expected, have).sum())   # 1 = today
        if missing > config.MAX_MISSING_DAYS_90D:
            return "data_quality"
        if self._bad_cum[last_row + 1] - self._bad_cum[lo]:
            return "data_quality"
        return None
