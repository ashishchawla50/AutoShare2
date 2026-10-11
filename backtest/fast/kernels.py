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
              verdict, pivot, vcp_low, prm, fr_th, fr_pts, red_th, red_pts):
    """detect_vcp + tightness gate for each candidate row g (prefix [first[g] .. g]).

    Every rule constant comes from config via signals.vcp_params(): prm holds the scalars
    (order fixed by signals.VCP_PARAM_NAMES); fr_th/fr_pts and red_th/red_pts are the
    tightness tables VCP_TIGHTNESS_FINAL_RANGE_POINTS and VCP_TIGHTNESS_REDUCTION_POINTS.
    """
    pre_min = int(prm[0]); min_up = prm[1]; sw = int(prm[2]); min_swing = prm[3]
    min_c = int(prm[4]); max_c = int(prm[5]); max_viol = int(prm[6])
    af = int(prm[7]); aslow = int(prm[8]); atr_comp = prm[9]; low_mult = prm[10]
    per_c = prm[11]; cap_c = prm[12]; vol_bonus = prm[13]; score_max = prm[14]
    min_tight = prm[15]
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
        if m < L + pre_min:
            verdict[g] = 2          # "Insufficient data"
            continue
        w0 = g - L + 1              # consolidation window [w0, g]
        prior_low = prefix_low_min[w0 - 1]
        if prior_low <= 0.0:
            verdict[g] = 2
            continue
        up = (close[w0] - prior_low) / prior_low
        if up < min_up:
            verdict[g] = 2
            continue

        # ATR compression: mean(TR last af) / mean(TR last aslow)
        atr10 = _pairwise_sum(tr, g - af + 1, af) / af
        atr50 = _pairwise_sum(tr, g - aslow + 1, aslow) / aslow
        compressed = False
        if atr50 > 0.0:
            compressed = (atr10 / atr50) < atr_comp

        # Swing highs/lows inside the window (left=sw ties allowed, right=sw strict)
        nh = 0
        nl = 0
        for j in range(sw, L - sw):
            i = w0 + j
            h = high[i]
            lw = low[i]
            is_hi = True
            is_lo = True
            for k in range(1, sw + 1):
                if not (h >= high[i - k]):
                    is_hi = False
                if not (h > high[i + k]):
                    is_hi = False
                if not (lw <= low[i - k]):
                    is_lo = False
                if not (lw < low[i + k]):
                    is_lo = False
            if is_hi:
                hi_idx[nh] = j
                nh += 1
            if is_lo:
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
            if rng < min_swing:
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
        if nc < min_c:
            verdict[g] = 2
            continue
        off = 0
        if nc > max_c:
            off = nc - max_c
            nc = max_c

        viol = 0
        for a in range(1, nc):
            if c_rng[off + a] >= c_rng[off + a - 1]:
                viol += 1
        if viol > max_viol or (viol >= 1 and not compressed):
            verdict[g] = 2
            continue

        vol_contracting = True
        for a in range(1, nc):
            if not (c_pk[off + a] < c_pk[off + a - 1]):
                vol_contracting = False
                break

        final = c_rng[off + nc - 1]
        score = 0.0
        for q in range(fr_th.shape[0]):
            if final < fr_th[q]:
                score += fr_pts[q]
                break
        score += min(cap_c, (nc - 1) * per_c)
        red = final / c_rng[off]
        for q in range(red_th.shape[0]):
            if red < red_th[q]:
                score += red_pts[q]
                break
        if vol_contracting:
            score += vol_bonus
        score = min(score_max, score)

        piv = high[w0]
        clow = low[w0]
        for i in range(w0 + 1, g + 1):
            if high[i] > piv:
                piv = high[i]
            if low[i] < clow:
                clow = low[i]
        if clow < prior_low * low_mult:
            verdict[g] = 2
            continue
        if score < min_tight:
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
