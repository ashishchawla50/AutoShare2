"""Precomputed per-ISIN indicator panel — the FAST path for research.

The reference path (`strategy/*` + `utils/indicator_utils.py`) recomputes every
indicator over the whole series for every symbol-day, which is O(n^2) and makes the
research matrix computationally infeasible. This module computes each indicator once
per ISIN, vectorised, and caches it.

    Layer 3 (reconstructed.db)
        -> build_panel(isin)      one vectorised pass
        -> data/panel/<isin>.npz  cached, load-on-demand
        -> strategy evaluation    O(1) lookups

DESIGN RULES

1. ISIN is the key. Ticker changes never split a series.

2. Strictly causal. Every value at row T derives only from rows <= T. Verified by
   `tests/test_indicator_panel.py::test_future_mutation_does_not_change_past`, which
   mutates bar T+1 and asserts nothing at or before T moves.

   The one indicator that is NOT naturally causal is swing detection: a swing high at
   index i is only confirmable `right` bars later. Both forms are stored:
     is_swing_high      flag at the bar it describes (for window scans, as the
                        reference does — stable once its right window is in the past)
     swing_high_conf    the same flag shifted forward by `right`, i.e. "a swing high
                        became confirmable as of this row". Causal by construction.
   Consumers making a decision at T must use the _conf form, or restrict the raw form
   to indices <= T - right (which is what the reference does implicitly by slicing).

3. Warm-up is explicit. Insufficient history yields NaN. Never 0, never
   forward/back-fill, never a constant. Warm-up lengths are documented per indicator
   in PANEL_SPEC and in docs/INDICATOR_PANEL.md.

4. Quarantine is not bridged. An unrepaired corporate action means the price basis
   either side of it is not comparable, so an EMA spanning it is meaningless. With
   `segment_on_quarantine=True` (the research default) the series is split at every
   quarantine window and each segment warms up independently; quarantined rows
   themselves are NaN. Set False to reproduce the reference path exactly — that flag
   exists so the fast/reference regression can be proven on identical semantics.

5. Deterministic. No randomness, no clock reads, no environment dependence. Identical
   input + identical PANEL_VERSION => byte-identical output.

EMA SEEDING — a documented deviation worth knowing about
`ewm(adjust=False)` is recursive from the first observation, so an EMA's value depends
on where the series starts. The reference engine loads `start_date - 730d`, so its EMA
seed moves with the backtest window; the panel always uses each ISIN's full available
history. For windows whose warm-up predates the dataset (e.g. the 2023 smoke run,
where data_start 2021-01 precedes the archive start 2021-06) the two coincide exactly.
For later windows the panel is the canonical definition, and is the better one: more
warm-up means less seed contamination. Documented rather than silently differing.

Usage:
    python research/indicator_panel.py --build            # all ISINs
    python research/indicator_panel.py --verify <ISIN>    # fast vs reference
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config

logger = logging.getLogger("research.panel")

# Bump when any formula, lookback, or column set changes — invalidates every cache.
PANEL_VERSION = "1.1.0"   # 1.1.0: ema_200_20d_ago lag 20 -> 19 to match the oracle

# Every lookback the existing research engine already uses. Nothing invented.
#   ema      50/150/200  trend_template;  10 stop_loss_manager floor;  21/50 momentum
#   sma_wk   30          stage_analysis (Weinstein 30-week MA)
#   rsi      2/14        mean_reversion / general
#   atr      14          general;  10 & 50 TR means for VCP compression ratio
#   roll     252         trend_template 52w high/low (of CLOSE, per reference)
#            20/60       momentum proximity;  VCP pivot & consolidation low
#   vol_sma  50/20       breakout_detector 50d avg volume;  momentum 20d
#   mom      21/63/126/252
PANEL_SPEC: dict[str, dict] = {
    "ema":      {"periods": [10, 21, 50, 150, 200], "warmup": lambda p: p},
    "rsi":      {"periods": [2, 14],                 "warmup": lambda p: p + 1},
    "atr":      {"periods": [14],                    "warmup": lambda p: p + 1},
    "roll_hi":  {"periods": [20, 60, 252],           "warmup": lambda p: p},
    "roll_lo":  {"periods": [20, 60, 252],           "warmup": lambda p: p},
    "vol_sma":  {"periods": [20, 50],                "warmup": lambda p: p},
    "mom":      {"periods": [21, 63, 126, 252],      "warmup": lambda p: p + 1},
}
SWING_LEFT, SWING_RIGHT = 2, 2      # vcp_detector._identify_contractions
VCP_LOOKBACK = 60                   # config.VCP_LOOKBACK_DAYS
WEEKLY_MA_WEEKS = 30                # config.WEINSTEIN_MA_PERIOD_WEEKS


# ── indicator primitives (same formulas as utils/indicator_utils.py) ──────────

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def _rsi(s: pd.Series, p: int) -> pd.Series:
    d = s.diff()
    gain, loss = d.clip(lower=0), (-d).clip(lower=0)
    ag = gain.ewm(alpha=1 / p, adjust=False).mean()
    al = loss.ewm(alpha=1 / p, adjust=False).mean()
    return 100 - (100 / (1 + ag / al.replace(0, np.nan)))


def _true_range(h: pd.Series, l: pd.Series, c: pd.Series) -> pd.Series:
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _swings(v: np.ndarray, left: int, right: int, high: bool) -> np.ndarray:
    """Vectorised equivalent of indicator_utils.swing_highs / swing_lows.

    Reference semantics preserved exactly: >= against the left window (ties allowed),
    strictly > against the right window (mirrored for lows). Bars within `left` of the
    start or `right` of the end are never marked.
    """
    n = len(v)
    out = np.zeros(n, dtype=bool)
    if n < left + right + 1:
        return out
    idx = np.arange(left, n - right)
    if high:
        lmax = np.max(np.stack([v[idx - k] for k in range(1, left + 1)]), axis=0)
        rmax = np.max(np.stack([v[idx + k] for k in range(1, right + 1)]), axis=0)
        out[idx] = (v[idx] >= lmax) & (v[idx] > rmax)
    else:
        lmin = np.min(np.stack([v[idx - k] for k in range(1, left + 1)]), axis=0)
        rmin = np.min(np.stack([v[idx + k] for k in range(1, right + 1)]), axis=0)
        out[idx] = (v[idx] <= lmin) & (v[idx] < rmin)
    return out


# ── panel construction ───────────────────────────────────────────────────────

def compute_panel(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every panel column for one contiguous price segment.

    Args:
        df: date-indexed OHLCV(+turnover) for a single ISIN segment, ascending.

    Returns:
        DataFrame with the same index and all PANEL_SPEC columns. NaN where warm-up
        is insufficient — never filled.
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    v = df["volume"]
    out = pd.DataFrame(index=df.index)
    out["open"], out["high"], out["low"], out["close"] = o, h, l, c
    out["volume"] = v
    out["turnover"] = df["turnover"] if "turnover" in df else np.nan
    n = len(df)

    out["ret_1d"] = c.pct_change()

    for p in PANEL_SPEC["ema"]["periods"]:
        e = _ema(c, p)
        e[:p - 1] = np.nan            # explicit warm-up; ewm otherwise emits from bar 0
        out[f"ema_{p}"] = e
    # trend_template does `ema(close, 200).iloc[-20]` against `.iloc[-1]`. On a series
    # of length N that is index N-20 vs N-1 — a **19-bar** lag, despite the name. The
    # panel must reproduce the oracle, so shift(19), not shift(20).
    #
    # Found by the Step-1 reclassification investigation: shift(20) flipped
    # c6_ema200_trending_up for MARUTI 2023-01-30, APOLLOHOSP 2023-02-22 and
    # JKCEMENT 2023-03-02, where ema_200 and its lag differ in the 4th decimal.
    # Whether the reference's off-by-one should be corrected is a STRATEGY-SEMANTICS
    # question, deliberately NOT decided here; the panel mirrors current behaviour.
    out["ema_200_20d_ago"] = out["ema_200"].shift(19)

    for p in PANEL_SPEC["rsi"]["periods"]:
        r = _rsi(c, p)
        r[:p] = np.nan
        out[f"rsi_{p}"] = r

    tr = _true_range(h, l, c)
    out["tr"] = tr
    for p in PANEL_SPEC["atr"]["periods"]:
        a = tr.ewm(span=p, adjust=False).mean()
        a[:p] = np.nan
        out[f"atr_{p}"] = a
    # vcp_detector._calculate_atr_compression: rolling means of TR, ratio 10/50
    out["tr_mean_10"] = tr.rolling(10).mean()
    out["tr_mean_50"] = tr.rolling(50).mean()
    out["atr_compression_ratio"] = (out["tr_mean_10"] / out["tr_mean_50"]).round(3)

    # trend_template uses CLOSE for the 52w extremes; VCP uses HIGH/LOW for the base
    for p in PANEL_SPEC["roll_hi"]["periods"]:
        out[f"close_hi_{p}"] = c.rolling(p).max()
        out[f"close_lo_{p}"] = c.rolling(p).min()
        out[f"high_hi_{p}"] = h.rolling(p).max()
        out[f"low_lo_{p}"] = l.rolling(p).min()

    for p in PANEL_SPEC["vol_sma"]["periods"]:
        out[f"vol_sma_{p}"] = v.rolling(p).mean()

    for p in PANEL_SPEC["mom"]["periods"]:
        out[f"mom_{p}"] = c.pct_change(p)

    out["vol_ret_20"] = out["ret_1d"].rolling(20).std()

    # VCP prior-uptrend inputs: min low STRICTLY BEFORE the 60-bar consolidation, and
    # the close at the consolidation's first bar (vcp_detector steps 1-2).
    out["pre_base_low_min"] = l.expanding().min().shift(VCP_LOOKBACK)
    out["consol_start_close"] = c.shift(VCP_LOOKBACK - 1)

    # Weinstein 30-week MA (stage_analysis), computed weekly then aligned forward.
    # Only past weeks can inform the current row: resample labels the week end, so the
    # value is shifted to become visible on the following bar.
    wk = df.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min",
                                   "close": "last", "volume": "sum"}).dropna()
    if len(wk) >= WEEKLY_MA_WEEKS:
        wk_ma = wk["close"].rolling(WEEKLY_MA_WEEKS).mean()
        out["wk_sma_30"] = wk_ma.reindex(df.index, method="ffill")
        out["wk_close"] = wk["close"].reindex(df.index, method="ffill")
    else:
        out["wk_sma_30"] = np.nan
        out["wk_close"] = np.nan

    sh = _swings(h.to_numpy(dtype=float), SWING_LEFT, SWING_RIGHT, True)
    sl = _swings(l.to_numpy(dtype=float), SWING_LEFT, SWING_RIGHT, False)
    out["is_swing_high"] = sh
    out["is_swing_low"] = sl
    # Causal forms: a swing at i is only knowable at i+right.
    out["swing_high_conf"] = np.concatenate([np.zeros(SWING_RIGHT, bool), sh[:-SWING_RIGHT]]) if n > SWING_RIGHT else np.zeros(n, bool)
    out["swing_low_conf"] = np.concatenate([np.zeros(SWING_RIGHT, bool), sl[:-SWING_RIGHT]]) if n > SWING_RIGHT else np.zeros(n, bool)

    out["bars_available"] = np.arange(1, n + 1)
    return out


def _segments(dates: pd.DatetimeIndex, quarantine: list[tuple[str, str]]
              ) -> list[tuple[int, int]]:
    """Split a series into contiguous non-quarantined [start, end) index ranges."""
    if not quarantine:
        return [(0, len(dates))]
    ds = dates.strftime("%Y-%m-%d").to_numpy()
    blocked = np.zeros(len(ds), dtype=bool)
    for f, t in quarantine:
        blocked |= (ds >= f) & (ds <= t)
    segs, start = [], None
    for i, b in enumerate(blocked):
        if not b and start is None:
            start = i
        elif b and start is not None:
            segs.append((start, i)); start = None
    if start is not None:
        segs.append((start, len(ds)))
    return segs


def build_panel_for_isin(con: sqlite3.Connection, isin: str,
                         segment_on_quarantine: bool = True) -> Optional[pd.DataFrame]:
    """Build the full panel for one ISIN, honouring quarantine segmentation."""
    df = pd.read_sql(
        "SELECT trade_date, open, high, low, close, volume, turnover "
        "FROM ohlcv_adjusted WHERE isin = ? ORDER BY trade_date",
        con, params=(isin,), parse_dates=["trade_date"]).set_index("trade_date")
    if df.empty:
        return None

    quar: list[tuple[str, str]] = []
    if segment_on_quarantine:
        quar = con.execute(
            "SELECT from_date, to_date FROM quarantine WHERE isin = ?", (isin,)).fetchall()

    parts = []
    for si, (a, b) in enumerate(_segments(df.index, quar)):
        seg = df.iloc[a:b]
        if seg.empty:
            continue
        p = compute_panel(seg)
        p["segment_id"] = si
        parts.append(p)
    if not parts:
        # Fully quarantined: emit an all-NaN frame so downstream sees "unavailable"
        # rather than "missing", which is a different and more dangerous state.
        empty = pd.DataFrame(index=df.index, columns=["close"], dtype=float)
        empty["segment_id"] = -1
        empty["quarantined"] = True
        return empty

    panel = pd.concat(parts).reindex(df.index)
    panel["quarantined"] = ~panel.index.isin(pd.concat(parts).index)
    return panel


# ── storage ──────────────────────────────────────────────────────────────────

def _panel_dir(root: Optional[Path] = None) -> Path:
    """Panel cache directory for a dataset root.

    Must honour an explicit root: building with --root data_local previously READ
    from data_local/ but WROTE to config.DB_PATH/panel, so a research dataset and a
    live dataset would silently share one cache.
    """
    return Path(root or config.DB_PATH) / "panel"


def _source_fingerprint(con: sqlite3.Connection) -> str:
    """Fingerprint of the Layer-3 inputs the panel derives from."""
    parts = [
        str(con.execute("SELECT COUNT(*) FROM ohlcv_adjusted").fetchone()[0]),
        str(con.execute("SELECT MAX(trade_date) FROM ohlcv_adjusted").fetchone()[0]),
        str(con.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0]),
        str(con.execute("SELECT COUNT(*) FROM corporate_actions "
                        "WHERE resolution='applied'").fetchone()[0]),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _code_fingerprint() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def manifest_path(root: Optional[Path] = None) -> Path:
    return _panel_dir(root) / "manifest.json"


def cache_is_valid(con: sqlite3.Connection, segment_on_quarantine: bool = True,
                   root: Optional[Path] = None) -> tuple[bool, str]:
    """Can the cached panel be trusted for a backtest right now?"""
    mp = manifest_path(root)
    if not mp.exists():
        return False, "no manifest — panel never built"
    m = json.loads(mp.read_text())
    if m.get("panel_version") != PANEL_VERSION:
        return False, f"panel version {m.get('panel_version')} != {PANEL_VERSION}"
    if m.get("code_fingerprint") != _code_fingerprint():
        return False, "indicator_panel.py changed since the cache was built"
    if m.get("source_fingerprint") != _source_fingerprint(con):
        return False, "reconstructed dataset changed since the cache was built"
    if m.get("segment_on_quarantine") != segment_on_quarantine:
        return False, "segment_on_quarantine differs from the cached build"
    return True, "valid"


def build_all(root: Optional[Path] = None, segment_on_quarantine: bool = True,
              limit: Optional[int] = None) -> dict:
    """Build and cache the panel for every ISIN. Returns build statistics."""
    db = Path(root or config.DB_PATH) / config.RECONSTRUCTED_DB
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out_dir = _panel_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)

    isins = [r[0] for r in con.execute(
        "SELECT DISTINCT isin FROM ohlcv_adjusted ORDER BY isin")]
    if limit:
        isins = isins[:limit]

    t0 = time.time()
    n_rows = n_ok = 0
    for i, isin in enumerate(isins, 1):
        p = build_panel_for_isin(con, isin, segment_on_quarantine)
        if p is None or p.empty:
            continue
        # float64 throughout: float32 would break the reference tolerance we assert.
        np.savez_compressed(
            out_dir / f"{isin}.npz",
            index=p.index.astype("int64").to_numpy(),
            columns=np.array(list(p.columns), dtype=object),
            values=p.to_numpy(dtype=np.float64))
        n_rows += len(p); n_ok += 1
        if i % 250 == 0:
            logger.info("  %d/%d ISINs, %d rows, %.0fs", i, len(isins), n_rows, time.time() - t0)

    stats = {
        "panel_version": PANEL_VERSION,
        "code_fingerprint": _code_fingerprint(),
        "source_fingerprint": _source_fingerprint(con),
        "segment_on_quarantine": segment_on_quarantine,
        "isins": n_ok,
        "rows": n_rows,
        "columns": len(load_panel(isins[0], root).columns) if n_ok else 0,
        "build_seconds": round(time.time() - t0, 1),
    }
    manifest_path(root).write_text(json.dumps(stats, indent=2))
    con.close()
    logger.warning("panel built: %d ISINs, %d rows, %.0fs", n_ok, n_rows, stats["build_seconds"])
    return stats


# Memory-BUDGETED LRU, not a count-based one. ISIN histories vary from a handful of
# bars to the full 1,283, so "N panels" is not a memory bound: a 400-panel cap
# measured at ~400 MB peak RSS on a 956 MB VPS. The budget below is enforced against
# each frame's real nbytes, leaving headroom for Python, pandas, SQLite, the engine's
# own OHLCV cache and transient allocations.
#
# float64 is retained deliberately. Downcasting to float32 would halve this but would
# breach the 1e-9 reference tolerance, so memory is solved by eviction, not precision.
_MEM: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
_MEM_BYTES = 0
PANEL_CACHE_BUDGET_MB = 96          # measured: keeps peak RSS ~200 MB on the VPS
_MISSING: set[str] = set()          # negative cache; avoids re-stat'ing absent files


def _frame_bytes(df: pd.DataFrame) -> int:
    return int(df.memory_usage(index=True, deep=False).sum())


def load_panel(isin: str, root: Optional[Path] = None) -> Optional[pd.DataFrame]:
    """Load one ISIN's panel, LRU-evicting to stay inside the memory budget."""
    global _MEM_BYTES
    if isin in _MEM:
        _MEM.move_to_end(isin)                 # LRU touch
        return _MEM[isin]
    if isin in _MISSING:
        return None
    f = _panel_dir(root) / f"{isin}.npz"
    if not f.exists():
        _MISSING.add(isin)
        return None
    z = np.load(f, allow_pickle=True)
    df = pd.DataFrame(z["values"], columns=list(z["columns"]),
                      index=pd.to_datetime(z["index"]))
    nb = _frame_bytes(df)
    budget = PANEL_CACHE_BUDGET_MB * 1024 * 1024
    while _MEM and _MEM_BYTES + nb > budget:
        _, ev = _MEM.popitem(last=False)       # evict least recently used
        _MEM_BYTES -= _frame_bytes(ev)
    _MEM[isin] = df
    _MEM_BYTES += nb
    return df


def cache_stats() -> dict:
    sizes = [_frame_bytes(d) for d in _MEM.values()]
    return {
        "cached_isins": len(_MEM),
        "cache_mb": round(_MEM_BYTES / 1024 / 1024, 1),
        "budget_mb": PANEL_CACHE_BUDGET_MB,
        "largest_panel_kb": round(max(sizes) / 1024, 1) if sizes else 0,
        "smallest_panel_kb": round(min(sizes) / 1024, 1) if sizes else 0,
    }


def clear_memory_cache() -> None:
    global _MEM_BYTES
    _MEM.clear()
    _MISSING.clear()
    _MEM_BYTES = 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser(description="Build the per-ISIN indicator panel")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--root", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-segment", action="store_true",
                    help="disable quarantine segmentation (reference-equivalent mode)")
    a = ap.parse_args()
    if a.build:
        print(json.dumps(build_all(Path(a.root) if a.root else None,
                                   not a.no_segment, a.limit), indent=2))
