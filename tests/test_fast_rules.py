"""Parity: backtest/fast/rules.py verdict arrays vs the strategy check_* functions.

For a sample of rows (half drawn from passing rows so passes and scores are
exercised), the prefix DataFrame is rebuilt and check_mean_reversion /
check_momentum / check_range_breakout are called on it. Their pass/fail, the gate
that failed (classified exactly as backtest_engine's _record_*_fail helpers do),
and the ranking score must agree with the cached arrays.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).parent.parent
DB = REPO / "data" / "reconstructed.db"
N_SAMPLE = 600

pytestmark = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")


def _mr_code(r) -> int:
    if r.passes:
        return 1
    reason = r.failure_reason
    if "uptrend" in reason or "EMA200" in reason:
        return 2
    if "RSI" in reason or "oversold" in reason:
        return 3
    return 0


def _mom_code(r) -> int:
    if r.passes:
        return 1
    reason = r.failure_reason
    if "EMA" in reason:
        return 2
    if "olume" in reason or "vol" in reason.lower():
        return 3
    if "20d high" in reason or "below" in reason:
        return 4
    if "RSI" in reason:
        return 5
    return 0


def _bo_code(r) -> int:
    if r.passes:
        return 1
    reason = r.failure_reason
    if "high" in reason.lower():
        return 2
    if "olume" in reason or "vol" in reason.lower():
        return 3
    if "EMA" in reason:
        return 4
    if "RSI" in reason:
        return 5
    return 0


@pytest.fixture(scope="module")
def arrays():
    from config.config import config as cfg
    old = cfg.DB_PATH
    cfg.DB_PATH = str(REPO / "data")
    from backtest.fast.data import load_market_data
    from backtest.fast.rules import load_rule_signals
    md = load_market_data()
    rs = load_rule_signals(md)
    yield md, rs
    cfg.DB_PATH = old


def _sample(md, verdict: np.ndarray, min_hist: int, seed: int) -> list[int]:
    first = np.repeat(md.offsets[:-1], np.diff(md.offsets))
    ok = (np.arange(len(md.dates)) - first + 1) >= min_hist
    rng = np.random.default_rng(seed)
    passing = np.nonzero(ok & (verdict == 1))[0]
    anyrow = np.nonzero(ok)[0]
    k = N_SAMPLE // 2
    return sorted(set(rng.choice(passing, min(k, len(passing)), replace=False).tolist()
                      + rng.choice(anyrow, k, replace=False).tolist()))


def _prefix(md, g: int) -> pd.DataFrame:
    k = int(np.searchsorted(md.offsets, g, side="right") - 1)
    a = int(md.offsets[k])
    return pd.DataFrame({"open": md.open[a:g + 1], "high": md.high[a:g + 1],
                         "low": md.low[a:g + 1], "close": md.close[a:g + 1],
                         "volume": md.volume[a:g + 1]})


@pytest.mark.parametrize("family", ["mr", "mom", "bo"])
def test_verdicts_and_scores_match_check_functions(arrays, family):
    from backtest.fast.rules import bo_score, mom_score, mr_score
    from strategy.breakout_scanner import check_range_breakout
    from strategy.mean_reversion import check_mean_reversion
    from strategy.momentum_strategy import check_momentum

    md, rs = arrays
    check, code, min_hist, seed = {
        "mr": (check_mean_reversion, _mr_code, 210, 1),
        "mom": (check_momentum, _mom_code, 60, 2),
        "bo": (check_range_breakout, _bo_code, 60, 3),
    }[family]
    verdict = getattr(rs, family)
    bad = []
    for g in _sample(md, verdict, min_hist, seed):
        r = check("X", _prefix(md, g))
        if code(r) != int(verdict[g]):
            bad.append((g, int(verdict[g]), r.failure_reason))
            continue
        if r.passes:
            first = int(md.offsets[np.searchsorted(md.offsets, g, side="right") - 1])
            fast = (mr_score(rs, md, g, first) if family == "mr"
                    else mom_score(rs, g) if family == "mom" else bo_score(rs, md, g))
            if abs(fast - r.score) > 1e-9:
                bad.append((g, "score", fast, r.score))
    assert not bad, f"{len(bad)} mismatches, first: {bad[:5]}"


def test_stage2_matches_reference_expression(arrays):
    """momentum_v3's trend-structure gate, as backtest_engine evaluates it."""
    from utils.indicator_utils import ema

    md, rs = arrays
    for g in _sample(md, rs.mom, 220, 4)[:200]:
        closes = _prefix(md, g)["close"]
        e50, e200 = ema(closes, 50), ema(closes, 200)
        ref = (float(closes.iloc[-1]) > float(e200.iloc[-1])
               and float(e50.iloc[-1]) > float(e200.iloc[-1])
               and float(e200.iloc[-1]) > float(e200.iloc[-21]))
        assert bool(rs.stage2[g]) == ref, g
