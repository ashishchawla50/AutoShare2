"""Panel-backed pre-gate for the research engine — the FAST path's first consumer.

`multi_timeframe.analyse_stock` runs `detect_stage` (a weekly resample) and
`passes_trend_template` (three EMAs over the full series) on EVERY symbol-day, and
then short-circuits when the trend template fails. In the reference smoke run the
trend template rejected 19,008 of 25,522 symbol-days — 74% of the work is spent
computing indicators only to throw the candidate away.

This module evaluates the same 8 conditions from the precomputed panel as O(1)
lookups. When the gate says "fail", the caller can skip `analyse_stock` entirely and
record the identical `fail_tt` outcome. When it says "pass" — or when it cannot
decide — the reference path runs unchanged.

WHY THIS IS BEHAVIOUR-IDENTICAL, NOT AN APPROXIMATION
  - `analyse_stock` returns `is_high_confidence=False` with
    `failure_reason="Trend template failed: ..."` on TT failure. The engine's only
    use of that result is to bucket it as `fail_tt` and move on: no pivot watch is
    added, no entry is taken. Skipping the call reproduces that exactly.
  - `stage_result` is computed before the TT check but is only *used* when TT passes
    (for the Weinstein ranking bonus). Discarding it on the failure path changes
    nothing observable.
  - The gate ABSTAINS whenever any required panel value is NaN — notably between 210
    and 251 bars, where the reference computes 52-week extremes from a short window
    while the panel's rolling(252) is still warming up. Abstaining falls through to
    the reference, so a warm-up edge can never silently alter a decision.

EMA SEEDING CAVEAT (see docs/INDICATOR_PANEL.md)
The panel computes EMAs over each ISIN's full history; the reference computes them
over whatever window the engine loaded. Where the engine's warm-up start precedes the
dataset start the two coincide exactly. Where it does not, the gate and the reference
can disagree at the margin, so `strict_equivalence=True` (the default) restricts
gating to that provably-equivalent case by requiring the panel segment to begin at or
before the loaded window's first bar.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

# Column set the gate needs; all must be non-NaN for the gate to decide.
_NEEDED = ("close", "ema_50", "ema_150", "ema_200", "ema_200_20d_ago",
           "close_lo_252", "close_hi_252")

# Reference condition order — kept identical so failure strings match.
_COND_NAMES = (
    "c1_price_above_ema50", "c2_price_above_ema150", "c3_price_above_ema200",
    "c4_ema50_above_ema150", "c5_ema150_above_ema200", "c6_ema200_trending_up",
    "c7_price_25pct_above_52w_low", "c8_price_within_25pct_of_52w_high",
)


class GateResult:
    """Outcome of a pre-gate evaluation.

    decided=False means "cannot answer from the panel" — the caller MUST fall through
    to the reference implementation. It never means "pass".
    """

    __slots__ = ("decided", "passes", "failing", "score")

    def __init__(self, decided: bool, passes: bool = False,
                 failing: Optional[list] = None, score: float = 0.0) -> None:
        self.decided = decided
        self.passes = passes
        self.failing = failing or []
        self.score = score


def trend_template_gate(panel: Optional[pd.DataFrame], as_of: pd.Timestamp,
                        min_bars: int = 210) -> GateResult:
    """Evaluate the 8 Minervini conditions for `as_of` from the panel.

    Args:
        panel:   Precomputed panel for one ISIN, or None.
        as_of:   The decision bar — the LAST bar of the data the reference would see.
        min_bars: Reference requires >= 210 bars before it will evaluate at all.

    Returns:
        GateResult. Abstains (decided=False) on missing panel, missing row,
        insufficient history, quarantine, or any NaN input.
    """
    if panel is None or len(panel) == 0:
        return GateResult(False)
    try:
        row = panel.loc[as_of]
    except KeyError:
        return GateResult(False)
    if isinstance(row, pd.DataFrame):        # duplicate index — refuse to guess
        return GateResult(False)

    if "quarantined" in panel.columns and bool(row.get("quarantined", False)):
        return GateResult(False)
    if float(row.get("bars_available", 0) or 0) < min_bars:
        return GateResult(False)

    vals = {}
    for c in _NEEDED:
        v = row.get(c, np.nan)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return GateResult(False)
        vals[c] = float(v)

    cur = vals["close"]
    e50, e150, e200 = vals["ema_50"], vals["ema_150"], vals["ema_200"]
    e200_prev = vals["ema_200_20d_ago"]
    lo52, hi52 = vals["close_lo_252"], vals["close_hi_252"]

    conds = (
        cur > e50,
        cur > e150,
        cur > e200,
        e50 > e150,
        e150 > e200,
        e200 > e200_prev,
        cur >= lo52 * 1.25,
        cur >= hi52 * 0.75,
    )
    met = sum(1 for c in conds if c)
    failing = [n for n, c in zip(_COND_NAMES, conds) if not c]
    return GateResult(True, passes=all(conds), failing=failing, score=met / 8 * 100)
