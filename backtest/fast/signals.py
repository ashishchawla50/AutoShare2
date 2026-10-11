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

# Scalar VCP rule constants handed to kernels._vcp_rows, in this order. Every one is
# read from config, the same values strategy/vcp_detector.py uses.
VCP_PARAM_NAMES = (
    "VCP_PRE_BASE_MIN_DAYS", "VCP_MIN_PRIOR_UPTREND_PCT", "VCP_SWING_WINDOW",
    "VCP_MIN_SWING_RANGE_PCT", "VCP_MIN_CONTRACTIONS", "VCP_MAX_CONTRACTIONS",
    "VCP_MAX_RANGE_VIOLATIONS", "VCP_ATR_FAST_DAYS", "VCP_ATR_SLOW_DAYS",
    "VCP_ATR_COMPRESSED_BELOW", "VCP_MIN_LOW_VS_BASE_LOW",
    "VCP_TIGHTNESS_POINTS_PER_CONTRACTION", "VCP_TIGHTNESS_CONTRACTION_CAP",
    "VCP_TIGHTNESS_VOLUME_BONUS", "VCP_TIGHTNESS_MAX", "VCP_MIN_TIGHTNESS_SCORE",
)
# Every config value the cached arrays depend on — part of the cache key, so changing
# any of them rebuilds the cache instead of silently reusing stale verdicts.
SIGNAL_CONFIG_KEYS = VCP_PARAM_NAMES + (
    "VCP_LOOKBACK_DAYS", "VCP_TIGHTNESS_FINAL_RANGE_POINTS", "VCP_TIGHTNESS_REDUCTION_POINTS",
    "TT_MIN_HISTORY_DAYS", "TT_EMA_FAST", "TT_EMA_MID", "TT_EMA_SLOW",
    "TT_EMA_SLOW_RISING_LOOKBACK", "TT_52W_DAYS", "TT_MIN_MULT_OF_52W_LOW",
    "TT_MIN_MULT_OF_52W_HIGH", "STOP_EMA_FLOOR_DAYS", "BREAKDOWN_EMA_DAYS",
    "PIVOT_BREAKOUT_VOLUME_AVG_DAYS", "BREAKDOWN_VOLUME_AVG_DAYS",
)


def vcp_params() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Config → kernel arguments: (scalars, final-range thresholds/points, reduction thresholds/points)."""
    prm = np.array([float(getattr(config, k)) for k in VCP_PARAM_NAMES])
    fr = np.array(config.VCP_TIGHTNESS_FINAL_RANGE_POINTS, dtype=np.float64)
    red = np.array(config.VCP_TIGHTNESS_REDUCTION_POINTS, dtype=np.float64)
    return prm, fr[:, 0].copy(), fr[:, 1].copy(), red[:, 0].copy(), red[:, 1].copy()

_ARRAYS = ("verdict", "pivot", "vcp_low", "ema10", "ema50", "volsma50", "volsma_bd")


@dataclass
class Signals:
    verdict: np.ndarray    # int8
    pivot: np.ndarray
    vcp_low: np.ndarray
    ema10: np.ndarray      # EMA(STOP_EMA_FLOOR_DAYS)      — trailing-stop floor
    ema50: np.ndarray      # EMA(BREAKDOWN_EMA_DAYS)       — breakdown exit
    volsma50: np.ndarray   # SMA(PIVOT_BREAKOUT_VOLUME_AVG_DAYS) — breakout volume
    volsma_bd: np.ndarray  # SMA(BREAKDOWN_VOLUME_AVG_DAYS) — breakdown volume


def _signal_key(md: MarketData) -> str:
    here = Path(__file__).parent
    src = (here / "signals.py").read_bytes() + (here / "kernels.py").read_bytes()
    cfg = "|".join(f"{k}={getattr(config, k)!r}" for k in SIGNAL_CONFIG_KEYS)
    parts = f"{md.key}|{hashlib.sha256(src).hexdigest()[:12]}|{cfg}"
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
    tt_fast = np.full(N, np.nan); ema150 = np.full(N, np.nan); ema200 = np.full(N, np.nan)
    volsma50 = np.full(N, np.nan); volsma_bd = np.full(N, np.nan)
    lo252 = np.full(N, np.nan); hi252 = np.full(N, np.nan)
    prefix_low_min = np.full(N, np.nan)
    first = np.empty(N, np.int64)
    tr = np.empty(N)

    for k in range(S):
        a, b = int(md.offsets[k]), int(md.offsets[k + 1])
        first[a:b] = a
        c = pd.Series(md.close[a:b])
        ema10[a:b] = c.ewm(span=config.STOP_EMA_FLOOR_DAYS, adjust=False).mean().to_numpy()
        ema50[a:b] = c.ewm(span=config.BREAKDOWN_EMA_DAYS, adjust=False).mean().to_numpy()
        tt_fast[a:b] = c.ewm(span=config.TT_EMA_FAST, adjust=False).mean().to_numpy()
        ema150[a:b] = c.ewm(span=config.TT_EMA_MID, adjust=False).mean().to_numpy()
        ema200[a:b] = c.ewm(span=config.TT_EMA_SLOW, adjust=False).mean().to_numpy()
        vol = pd.Series(md.volume[a:b])
        volsma50[a:b] = vol.rolling(window=config.PIVOT_BREAKOUT_VOLUME_AVG_DAYS).mean().to_numpy()
        volsma_bd[a:b] = vol.rolling(window=config.BREAKDOWN_VOLUME_AVG_DAYS).mean().to_numpy()
        lo252[a:b] = c.rolling(config.TT_52W_DAYS, min_periods=1).min().to_numpy()
        hi252[a:b] = c.rolling(config.TT_52W_DAYS, min_periods=1).max().to_numpy()
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
    ok = m >= config.TT_MIN_HISTORY_DAYS
    back = config.TT_EMA_SLOW_RISING_LOOKBACK - 1    # iloc[-N] is N-1 rows before the last
    e200_20 = np.full(N, np.nan)
    e200_20[back:] = ema200[:-back]
    c = md.close
    with np.errstate(invalid="ignore"):
        tt = (ok
              & (c > tt_fast) & (c > ema150) & (c > ema200)
              & (tt_fast > ema150) & (ema150 > ema200)
              & (ema200 > e200_20)
              & (c >= lo252 * config.TT_MIN_MULT_OF_52W_LOW)
              & (c >= hi252 * config.TT_MIN_MULT_OF_52W_HIGH))

    verdict = np.full(N, FAIL_TT, dtype=np.int8)
    pivot = np.zeros(N)
    vcp_low = np.zeros(N)
    cand = np.nonzero(tt)[0].astype(np.int64)
    prm, fr_th, fr_pts, red_th, red_pts = vcp_params()
    _vcp_rows(cand, first, md.high, md.low, md.close, md.volume, tr, prefix_low_min, L,
              verdict, pivot, vcp_low, prm, fr_th, fr_pts, red_th, red_pts)
    logger.info("Signals: %d rows, %d pass trend template, %d high-confidence",
                N, len(cand), int((verdict == HIGH_CONF).sum()))
    return Signals(verdict=verdict, pivot=pivot, vcp_low=vcp_low,
                   ema10=ema10, ema50=ema50, volsma50=volsma50, volsma_bd=volsma_bd)
