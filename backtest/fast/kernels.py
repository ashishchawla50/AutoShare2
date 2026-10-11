"""numba kernels for signals.py — imported only when a signal cache is (re)built,
so warm runs skip numba's ~0.7 s import."""

from __future__ import annotations

import numpy as np
from numba import njit

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


@njit(cache=True)
def _prior_mean(x, first, w, out):
    """out[g] = mean(x[g-w .. g-1]) within g's security, i.e. x.iloc[-w-1:-1].mean()
    on the prefix ending at g. NaN until the prefix holds w+1 rows."""
    for g in range(x.shape[0]):
        if g - first[g] >= w:
            out[g] = _pairwise_sum(x, g - w, w) / w
        else:
            out[g] = np.nan
