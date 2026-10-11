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
from numba import njit

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
    src = Path(__file__).read_bytes()
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


# ── numba kernels ────────────────────────────────────────────────────────────

@njit(cache=True)
def _pairwise_sum(a, s, n):
    """numpy's pairwise_sum for n <= 128 — what pandas' Series.mean() reduces with."""
    if n < 8:
        r = 0.0
        for i in range(n):
            r += a[s + i]
        return r
    r0 = a[s]; r1 = a[s + 1]; r2 = a[s + 2]; r3 = a[s + 3]
    r4 = a[s + 4]; r5 = a[s + 5]; r6 = a[s + 6]; r7 = a[s + 7]
    i = 8
    lim = n - (n % 8)
    while i < lim:
        r0 += a[s + i]; r1 += a[s + i + 1]; r2 += a[s + i + 2]; r3 += a[s + i + 3]
        r4 += a[s + i + 4]; r5 += a[s + i + 5]; r6 += a[s + i + 6]; r7 += a[s + i + 7]
        i += 8
    res = ((r0 + r1) + (r2 + r3)) + ((r4 + r5) + (r6 + r7))
    while i < n:
        res += a[s + i]
        i += 1
    return res


@njit(cache=True)
def _vcp_rows(cand, first, high, low, close, volume, tr, prefix_low_min, L,
              verdict, pivot, vcp_low):
    """detect_vcp + tightness gate for each candidate row g (prefix [first[g] .. g])."""
    hi_idx = np.empty(L, np.int64)
    lo_idx = np.empty(L, np.int64)
    c_hi = np.empty(L, np.int64)
    c_lo = np.empty(L, np.int64)
    c_rng = np.empty(L)
    c_pk = np.empty(L)
    for t in range(cand.shape[0]):
        g = cand[t]
        s = first[g]
        m = g - s + 1
        if m < L + 60:
            verdict[g] = 2          # "Insufficient data"
            continue
        w0 = g - L + 1              # consolidation window [w0, g]
        prior_low = prefix_low_min[w0 - 1]
        if prior_low <= 0.0:
            verdict[g] = 2
            continue
        up = (close[w0] - prior_low) / prior_low
        if up < 0.30:
            verdict[g] = 2
            continue

        # ATR compression: mean(TR last 10) / mean(TR last 50)
        atr10 = _pairwise_sum(tr, g - 9, 10) / 10
        atr50 = _pairwise_sum(tr, g - 49, 50) / 50
        compressed = False
        if atr50 > 0.0:
            compressed = (atr10 / atr50) < 0.7

        # Swing highs/lows inside the window (left=2 ties allowed, right=2 strict)
        nh = 0
        nl = 0
        for j in range(2, L - 2):
            i = w0 + j
            h = high[i]
            if h >= max(high[i - 2], high[i - 1]) and h > max(high[i + 1], high[i + 2]):
                hi_idx[nh] = j
                nh += 1
            lw = low[i]
            if lw <= min(low[i - 2], low[i - 1]) and lw < min(low[i + 1], low[i + 2]):
                lo_idx[nl] = j
                nl += 1

        # Pair each swing high with the first swing low after it
        nc = 0
        for a in range(nh):
            hj = hi_idx[a]
            lj = -1
            for b in range(nl):
                if lo_idx[b] > hj:
                    lj = lo_idx[b]
                    break
            if lj < 0:
                continue
            sh = high[w0 + hj]
            sl = low[w0 + lj]
            if sh <= 0.0:
                continue
            rng = (sh - sl) / sh
            pk = volume[w0 + hj]
            for k in range(hj + 1, lj + 1):
                if volume[w0 + k] > pk:
                    pk = volume[w0 + k]
            if rng < 0.01:
                continue
            c_hi[nc] = hj
            c_lo[nc] = lj
            c_rng[nc] = rng
            c_pk[nc] = pk
            nc += 1

        # Deduplicate overlapping contractions (keep in order, non-overlapping)
        if nc > 1:
            kept = 1
            for a in range(1, nc):
                if c_hi[a] > c_lo[kept - 1]:
                    c_hi[kept] = c_hi[a]
                    c_lo[kept] = c_lo[a]
                    c_rng[kept] = c_rng[a]
                    c_pk[kept] = c_pk[a]
                    kept += 1
            nc = kept
        if nc < 2:
            verdict[g] = 2
            continue
        off = 0
        if nc > 4:
            off = nc - 4
            nc = 4

        viol = 0
        for a in range(1, nc):
            if c_rng[off + a] >= c_rng[off + a - 1]:
                viol += 1
        if viol > 1 or (viol == 1 and not compressed):
            verdict[g] = 2
            continue

        vol_contracting = True
        for a in range(1, nc):
            if not (c_pk[off + a] < c_pk[off + a - 1]):
                vol_contracting = False
                break

        final = c_rng[off + nc - 1]
        score = 0.0
        if final < 0.05:
            score += 60.0
        elif final < 0.10:
            score += 45.0
        elif final < 0.15:
            score += 25.0
        score += min(15.0, (nc - 1) * 5.0)
        red = final / c_rng[off]
        if red < 0.3:
            score += 10.0
        elif red < 0.5:
            score += 5.0
        if vol_contracting:
            score += 15.0
        score = min(100.0, score)

        piv = high[w0]
        clow = low[w0]
        for i in range(w0 + 1, g + 1):
            if high[i] > piv:
                piv = high[i]
            if low[i] < clow:
                clow = low[i]
        if clow < prior_low * 0.97:
            verdict[g] = 2
            continue
        if score < 50.0:
            verdict[g] = 3
            continue
        verdict[g] = 4
        pivot[g] = piv
        vcp_low[g] = clow


# ── driver ───────────────────────────────────────────────────────────────────

def compute_signals(md: MarketData) -> Signals:
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
