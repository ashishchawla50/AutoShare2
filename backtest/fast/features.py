"""Entry-day features of a vcp_v2 breakout, for filters (test plan A2/B2) and the B1 study.

All features read only data up to the breakout candle (p_row) plus the next day's open
(the entry fill), so they are known when the order is placed.

RS percentile mirrors scanner/relative_strength.py: weighted compound return over
bars 21-84 (40%), 84-147, 147-210, 210-252 (20% each), a segment counting 0 when the
stock has too little history, then percentile rank (pandas rank(pct=True) * 99,
average ties) among the day's universe members with >= 252 rows. The benchmark term
is identical for every stock on a day, so it cannot change a rank and is left out.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from backtest.data_loader import load_nifty_ohlcv
from backtest.fast.data import MarketData
from config.config import config

_SEGMENTS = ((21, 84, 0.4), (84, 147, 0.2), (147, 210, 0.2), (210, 252, 0.2))


def rs_raw(md: MarketData) -> np.ndarray:
    """Weighted RS return for every row (NaN where the prefix has < 252 rows)."""
    N = len(md.dates)
    first = np.repeat(md.offsets[:-1], np.diff(md.offsets))
    m = np.arange(N) - first + 1                       # prefix length (rows)
    c = md.close
    out = np.zeros(N)
    for start, end, w in _SEGMENTS:
        ok = (m - 1) >= end                            # len(returns) >= end_bar
        a = np.where(ok, np.arange(N) - start, 0)
        b = np.where(ok, np.arange(N) - end, 0)
        out += w * np.where(ok, c[a] / c[b] - 1, 0.0)
    out[m < 252] = np.nan
    return out


class FeatureBook:
    """Computes features for an engine; build once per engine run (after _prepare)."""

    def __init__(self, engine) -> None:
        self.e = engine
        md = engine._md
        self.rs = rs_raw(md)
        nifty = load_nifty_ohlcv(engine.start_date - timedelta(days=730), engine.end_date)
        self.n_days = nifty.index.values.astype("datetime64[D]").astype(np.int64)
        nc = nifty["close"].astype(float)
        self.n_close = nc.to_numpy()
        self.n_ema50 = nc.ewm(span=50, adjust=False).mean().to_numpy()

    def _nifty_idx(self, day: int) -> int:
        return int(np.searchsorted(self.n_days, day, side="right")) - 1

    def rs_percentile(self, ti: int, slot: int, g: int) -> float:
        e = self.e
        U = e._day_slots[ti]
        n = e._n[ti, U].astype(np.int64)
        rows = e._lo[U] + n - 2
        vals = self.rs[np.where(n >= 2, rows, 0)]
        vals = vals[(n >= 2) & ~np.isnan(vals)]
        v = self.rs[g]
        if np.isnan(v) or len(vals) == 0:
            return np.nan                              # live: < 252 rows → no RS score
        less = np.sum(vals < v)
        eq = np.sum(vals == v)
        return (less + (eq + 1) / 2) / len(vals) * 99

    def features(self, slot: int, p_row: int, ti: int, pend: tuple) -> dict:
        e, md, sig = self.e, self.e._md, self.e._sig
        g = p_row
        a = int(e._a[slot])
        c, o_next = float(md.close[g]), float(md.open[g + 1])
        k = self._nifty_idx(int(md.dates[g]))
        lo_tr = max(a + 1, g - 13)
        tr = np.maximum.reduce([md.high[lo_tr:g + 1] - md.low[lo_tr:g + 1],
                                np.abs(md.high[lo_tr:g + 1] - md.close[lo_tr - 1:g]),
                                np.abs(md.low[lo_tr:g + 1] - md.close[lo_tr - 1:g])])
        hi52 = float(md.high[max(a, g - 251):g + 1].max())
        k63 = self._nifty_idx(int(md.dates[max(a, g - 63)]))
        watch_start = pend[2] - timedelta(days=config.VCP2_PIVOT_WATCH_DAYS)
        return {
            "rs_pct": self.rs_percentile(ti, slot, g),
            "vol_mult": float(md.volume[g]) / float(sig.volsma50[g]),
            "close_vs_pivot": c / pend[0] - 1,
            "price": o_next,
            "nifty_vs_ema50": self.n_close[k] / self.n_ema50[k] - 1,
            "breadth": float(e._breadth_s.iloc[k]) if k < len(e._breadth_s) else np.nan,
            "regime": e._regime_name,
            "gap": o_next / c - 1,
            "atr_pct": float(tr.mean()) / c,
            "from_52w_high": c / hi52 - 1,
            "close_vs_ema50": c / float(sig.ema50[g]) - 1,
            "watch_days": (e._sim_date - watch_start).days,
            "rel_63": (c / float(md.close[max(a, g - 63)]) - 1) - (self.n_close[k] / self.n_close[k63] - 1),
        }


def rs_filter(min_pct: float = 70.0):
    """Entry filter for test A2: drop breakouts below the RS percentile (live's removed rule,
    passes_rs_filter). A stock with no RS score (< 252 rows) fails, as it is absent from live's
    rs_scores and defaults to 0 there."""
    book: dict = {}

    def f(engine, slot, p_row, ti, pend) -> bool:
        if "fb" not in book:
            book["fb"] = FeatureBook(engine)
        pct = book["fb"].rs_percentile(ti, slot, p_row)
        return not np.isnan(pct) and pct >= min_pct
    return f
