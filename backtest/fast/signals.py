"""Per-row vcp_v2 scan verdicts and exit indicators, computed once for all history.

`analyse_stock(symbol, prev_day_df)` in scan mode depends only on that security's
own history up to the scan row. It never looks at the portfolio. So instead of
calling it per symbol per simulated day (the reference engine's 3-hour cost), it is
evaluated here for EVERY row of EVERY security in one pass and cached:

    verdict[g]  = outcome of analyse_stock on rows [first .. g] of g's security
                  1 fail_tt · 2 fail_vcp · 3 fail_tightness · 4 high-confidence
    pivot[g], vcp_low[g]  = the pivot watch entry when verdict == 4

The arithmetic mirrors the reference bit for bit where a threshold depends on it:
EMAs and rolling means go through the same pandas kernels on the full series (a
prefix of an EMA or a fixed rolling window is identical to the same computation on
the truncated frame); the ATR means replicate numpy's pairwise summation.

Warm-up semantics: indicators are seeded from each security's FIRST row in the
dataset. The reference seeds from `start - 730 days`, which is the same thing for
any backtest starting on or before 2023-06-01 (dataset begins 2021-06-01). Later
starts get a longer, better-warmed EMA here than the reference would compute.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.fast.data import MarketData, cache_dir
from config.config import config

logger = logging.getLogger("backtest.fast.signals")

FAIL_TT, FAIL_VCP, FAIL_TIGHTNESS, HIGH_CONF = 1, 2, 3, 4

_ARRAYS = ("verdict", "pivot", "vcp_low", "ema10", "ema50", "volsma50")


@dataclass
class Signals:
    verdict: np.ndarray    # int8
    pivot: np.ndarray
    vcp_low: np.ndarray
    ema10: np.ndarray
    ema50: np.ndarray
    volsma50: np.ndarray


def _signal_key(md: MarketData) -> str:
    here = Path(__file__).parent
    src = (here / "signals.py").read_bytes() + (here / "kernels.py").read_bytes()
    parts = f"{md.key}|{hashlib.sha256(src).hexdigest()[:12]}|L={config.VCP_LOOKBACK_DAYS}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def load_signals(md: MarketData, rebuild: bool = False) -> Signals:
    d = cache_dir() / f"signals_{_signal_key(md)}"
    if d.exists() and not rebuild:
        return Signals(**{f: np.load(d / f"{f}.npy") for f in _ARRAYS})
    sig = compute_signals(md)
    d.mkdir(parents=True, exist_ok=True)
    for f in _ARRAYS:
        np.save(d / f"{f}.npy", getattr(sig, f))
    return sig


# ── driver ───────────────────────────────────────────────────────────────────

def compute_signals(md: MarketData) -> Signals:
    from backtest.fast.kernels import _vcp_rows

    N = len(md.dates)
    S = len(md.isins)
    L = int(config.VCP_LOOKBACK_DAYS)
    ema10 = np.full(N, np.nan); ema50 = np.full(N, np.nan)
    ema150 = np.full(N, np.nan); ema200 = np.full(N, np.nan)
    volsma50 = np.full(N, np.nan)
    lo252 = np.full(N, np.nan); hi252 = np.full(N, np.nan)
    prefix_low_min = np.full(N, np.nan)
    first = np.empty(N, np.int64)
    tr = np.empty(N)

    for k in range(S):
        a, b = int(md.offsets[k]), int(md.offsets[k + 1])
        first[a:b] = a
        c = pd.Series(md.close[a:b])
        ema10[a:b] = c.ewm(span=10, adjust=False).mean().to_numpy()
        ema50[a:b] = c.ewm(span=50, adjust=False).mean().to_numpy()
        ema150[a:b] = c.ewm(span=150, adjust=False).mean().to_numpy()
        ema200[a:b] = c.ewm(span=200, adjust=False).mean().to_numpy()
        volsma50[a:b] = pd.Series(md.volume[a:b]).rolling(window=50).mean().to_numpy()
        lo252[a:b] = c.rolling(252, min_periods=1).min().to_numpy()
        hi252[a:b] = c.rolling(252, min_periods=1).max().to_numpy()
        prefix_low_min[a:b] = np.minimum.accumulate(md.low[a:b])
        h, l, cl = md.high[a:b], md.low[a:b], md.close[a:b]
        t = h - l
        if b - a > 1:
            pc = cl[:-1]
            t[1:] = np.maximum(np.maximum(t[1:], np.abs(h[1:] - pc)), np.abs(l[1:] - pc))
        tr[a:b] = t

    # Trend template — all 8 conditions, vectorised
    idx = np.arange(N)
    m = idx - first + 1
    ok = m >= 210
    e200_20 = np.full(N, np.nan)
    e200_20[19:] = ema200[:-19]
    c = md.close
    with np.errstate(invalid="ignore"):
        tt = (ok
              & (c > ema50) & (c > ema150) & (c > ema200)
              & (ema50 > ema150) & (ema150 > ema200)
              & (ema200 > e200_20)
              & (c >= lo252 * 1.25) & (c >= hi252 * 0.75))

    verdict = np.full(N, FAIL_TT, dtype=np.int8)
    pivot = np.zeros(N)
    vcp_low = np.zeros(N)
    cand = np.nonzero(tt)[0].astype(np.int64)
    _vcp_rows(cand, first, md.high, md.low, md.close, md.volume, tr, prefix_low_min, L,
              verdict, pivot, vcp_low)
    logger.info("Signals: %d rows, %d pass trend template, %d high-confidence",
                N, len(cand), int((verdict == HIGH_CONF).sum()))
    return Signals(verdict=verdict, pivot=pivot, vcp_low=vcp_low,
                   ema10=ema10, ema50=ema50, volsma50=volsma50)
