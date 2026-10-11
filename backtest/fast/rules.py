"""Per-row entry verdicts for the rule-based strategies, computed once for all history.

Like signals.py does for vcp_v2: `check_mean_reversion`, `check_momentum` and
`check_range_breakout` each look only at one stock's own prefix, so their outcome
is evaluated here for every row of every security and cached. The engine then only
reads arrays.

Verdict codes (int8), in the order the reference checks its gates:
    mean_reversion  1 pass · 2 close <= EMA200 · 3 RSI(2) not oversold · 0 NaN indicator
    momentum        1 pass · 2 EMA21/EMA50 · 3 volume · 4 too far from 20d high · 5 RSI
    breakout        1 pass · 2 no new 20d high · 3 volume · 4 below EMA50 · 5 RSI

Min-bars checks are left to the engine, which knows the reference's window length.
Indicators seed from each security's first row (same warm-up note as signals.py).
tests/test_fast_rules.py checks these arrays against the check_* functions.
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
from utils.indicator_utils import ema, rsi

logger = logging.getLogger("backtest.fast.rules")

PASS = 1

_ARRAYS = ("mr", "mom", "bo", "stage2", "ema5", "ema21", "ema50", "ema200",
           "rsi2", "rsi14", "vol_ratio", "pct_from_high")

# Every config value the verdicts depend on — changing one rebuilds the cache.
_CFG = ("MR_EMA_TREND", "MR_EMA_EXIT", "MR_RSI_PERIOD", "MR_RSI_ENTRY",
        "MOMENTUM_EMA_FAST", "MOMENTUM_EMA_SLOW", "MOMENTUM_VOLUME_MULTIPLIER",
        "MOMENTUM_MAX_PCT_FROM_HIGH", "MOMENTUM_RSI_MIN", "MOMENTUM_RSI_MAX",
        "MOMENTUM3_EMA200_RISE_DAYS",
        "BREAKOUT_LOOKBACK_DAYS", "BREAKOUT_VOLUME_MIN", "BREAKOUT_EMA_PERIOD",
        "BREAKOUT_RSI_MAX", "BREAKOUT_RSI_MIN")


@dataclass
class RuleSignals:
    mr: np.ndarray             # int8 verdicts (codes above)
    mom: np.ndarray
    bo: np.ndarray
    stage2: np.ndarray         # bool — momentum_v3 trend-structure gate
    ema5: np.ndarray           # exit indicators, read on the position's own row
    ema21: np.ndarray
    ema50: np.ndarray
    ema200: np.ndarray
    rsi2: np.ndarray
    rsi14: np.ndarray
    vol_ratio: np.ndarray      # today's volume / prior-20-day average (0 if no average)
    pct_from_high: np.ndarray  # momentum: distance below the 20-day closing high


def _key(md: MarketData) -> str:
    here = Path(__file__).parent
    src = (here / "rules.py").read_bytes() + (here / "kernels.py").read_bytes()
    cfg = "|".join(f"{k}={getattr(config, k)}" for k in _CFG)
    parts = f"{md.key}|{hashlib.sha256(src).hexdigest()[:12]}|{cfg}"
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def load_rule_signals(md: MarketData, rebuild: bool = False) -> RuleSignals:
    d = cache_dir() / f"rules_{_key(md)}"
    if d.exists() and not rebuild:
        return RuleSignals(**{f: np.load(d / f"{f}.npy") for f in _ARRAYS})
    rs = compute_rule_signals(md)
    d.mkdir(parents=True, exist_ok=True)
    for f in _ARRAYS:
        np.save(d / f"{f}.npy", getattr(rs, f))
    return rs


def _per_security(md: MarketData, fn) -> np.ndarray:
    out = np.full(len(md.dates), np.nan)
    for k in range(len(md.isins)):
        a, b = int(md.offsets[k]), int(md.offsets[k + 1])
        out[a:b] = fn(pd.Series(md.close[a:b])).to_numpy()
    return out


def compute_rule_signals(md: MarketData) -> RuleSignals:
    from backtest.fast.kernels import _prior_mean

    N = len(md.dates)
    first = np.repeat(md.offsets[:-1], np.diff(md.offsets)).astype(np.int64)
    lb = config.BREAKOUT_LOOKBACK_DAYS
    lag = config.MOMENTUM3_EMA200_RISE_DAYS - 1          # ema200.iloc[-21] on the prefix

    # Each indicator runs through the same pandas kernel the strategy uses, on the
    # full series — a prefix of a recursive EMA / Wilder RSI equals the same
    # computation on the truncated frame.
    e5 = _per_security(md, lambda c: ema(c, config.MR_EMA_EXIT))
    e21 = _per_security(md, lambda c: ema(c, config.MOMENTUM_EMA_FAST))
    e50 = _per_security(md, lambda c: ema(c, config.MOMENTUM_EMA_SLOW))
    e_bo = (e50 if config.BREAKOUT_EMA_PERIOD == config.MOMENTUM_EMA_SLOW
            else _per_security(md, lambda c: ema(c, config.BREAKOUT_EMA_PERIOD)))
    e200 = _per_security(md, lambda c: ema(c, config.MR_EMA_TREND))
    e200_lag = _per_security(md, lambda c: ema(c, config.MR_EMA_TREND).shift(lag))
    r2 = _per_security(md, lambda c: rsi(c, config.MR_RSI_PERIOD))
    r14 = _per_security(md, lambda c: rsi(c, 14))
    hi20 = _per_security(md, lambda c: c.rolling(20, min_periods=1).max())           # close.tail(20).max()
    prior_hi = _per_security(md, lambda c: c.shift(1).rolling(lb, min_periods=1).max())

    avg20 = np.empty(N)
    _prior_mean(md.volume, first, 20, avg20)          # volume.iloc[-21:-1].mean()
    c, vol = md.close, md.volume
    with np.errstate(invalid="ignore", divide="ignore"):
        vol_ratio = np.where(avg20 > 0, vol / avg20, 0.0)
        pfh = np.where(hi20 > 0, (hi20 - c) / hi20, 1.0)

        # Later assignments win, so the gate the reference checks first takes precedence.
        mr = np.full(N, PASS, np.int8)
        mr[r2 >= config.MR_RSI_ENTRY] = 3
        mr[c <= e200] = 2
        mr[np.isnan(e200) | np.isnan(r2) | np.isnan(e5)] = 0

        mom = np.full(N, PASS, np.int8)
        mom[~np.isnan(r14) & ((r14 < config.MOMENTUM_RSI_MIN) | (r14 > config.MOMENTUM_RSI_MAX))] = 5
        mom[pfh > config.MOMENTUM_MAX_PCT_FROM_HIGH] = 4
        mom[vol_ratio < config.MOMENTUM_VOLUME_MULTIPLIER] = 3
        mom[(c <= e21) | (e21 <= e50)] = 2

        bo = np.full(N, PASS, np.int8)
        bo[~np.isnan(r14) & ((r14 > config.BREAKOUT_RSI_MAX) | (r14 < config.BREAKOUT_RSI_MIN))] = 5
        bo[c <= e_bo] = 4
        bo[vol_ratio < config.BREAKOUT_VOLUME_MIN] = 3
        bo[~(c > prior_hi)] = 2

        stage2 = (c > e200) & (e50 > e200) & (e200 > e200_lag)

    logger.info("Rule signals: %d rows | MR %d · momentum %d · breakout %d passing",
                N, int((mr == PASS).sum()), int((mom == PASS).sum()), int((bo == PASS).sum()))
    return RuleSignals(mr=mr, mom=mom, bo=bo, stage2=stage2, ema5=e5, ema21=e21, ema50=e50,
                       ema200=e200, rsi2=r2, rsi14=r14, vol_ratio=vol_ratio, pct_from_high=pfh)


# ── same-day ranking scores (strategy "all" only), mirroring each check_* ────

def mr_score(rs: RuleSignals, md: MarketData, g: int, first: int) -> float:
    c = md.close
    down = 0
    for i in range(1, min(11, g - first + 1)):
        if float(c[g - i + 1]) < float(c[g - i]):
            down += 1
        else:
            break
    rsi_score = max(0.0, (config.MR_RSI_ENTRY - float(rs.rsi2[g])) / config.MR_RSI_ENTRY * 100)
    consec_score = min(100.0, down / 5 * 100)
    pct_above = (float(c[g]) / float(rs.ema200[g]) - 1) * 100
    trend_score = max(0.0, 50.0 - abs(pct_above - 10.0) * 2)
    return round(rsi_score * 0.5 + consec_score * 0.3 + trend_score * 0.2, 2)


def mom_score(rs: RuleSignals, g: int) -> float:
    e21, e50 = float(rs.ema21[g]), float(rs.ema50[g])
    ema_gap = min(100.0, ((e21 - e50) / e50) * 1000)
    vol_score = min(100.0, (float(rs.vol_ratio[g]) - 1.5) / 3.5 * 100)
    rs_score = (1.0 - float(rs.pct_from_high[g]) / config.MOMENTUM_MAX_PCT_FROM_HIGH) * 100
    rsi_score = max(0.0, 100.0 - abs(float(rs.rsi14[g]) - 60.0) * 4)
    return round(ema_gap * 0.30 + vol_score * 0.35 + rs_score * 0.25 + rsi_score * 0.10, 2)


def bo_score(rs: RuleSignals, md: MarketData, g: int) -> float:
    c = md.close
    lb = config.BREAKOUT_LOOKBACK_DAYS
    cur = float(c[g])
    days = lb
    for i in range(2, lb + 2):
        if float(c[g - i + 1]) >= cur:
            days = i - 2
            break
    days = max(1, days)
    vol_score = min(100.0, (float(rs.vol_ratio[g]) - 1.5) / 3.5 * 100)
    rsi_score = max(0.0, 100.0 - abs(float(rs.rsi14[g]) - 60.0) * 3)
    days_score = min(100.0, days / lb * 100)
    return round(vol_score * 0.5 + rsi_score * 0.3 + days_score * 0.2, 2)
