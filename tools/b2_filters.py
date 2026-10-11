"""B2 entry filters, pre-registered in docs/TEST_PLAN_2026_10.md Part 2 after the B1 study.

Thresholds are the B1 DEV quintile boundaries: the bottom quintile is dropped.
"""

from __future__ import annotations

import numpy as np

from backtest.fast.features import FeatureBook

RS_MIN = 41.0             # B1: RS percentile Q1 = [5.5 .. 40.9] → PF 0.58
PIVOT_MIN = 0.008         # B1: close vs pivot Q1 = [0.03% .. 0.81%] → PF 0.52


def rs_at_least(min_pct: float = RS_MIN):
    """Drop a breakout whose RS percentile is known and below min_pct. A stock with no RS
    score (< 252 rows) passes: B1 had no evidence about those."""
    book: dict = {}

    def f(engine, slot, p_row, ti, pend) -> bool:
        if "fb" not in book:
            book["fb"] = FeatureBook(engine)
        pct = book["fb"].rs_percentile(ti, slot, p_row)
        return bool(np.isnan(pct) or pct >= min_pct)
    return f


def clear_of_pivot(min_gap: float = PIVOT_MIN):
    """Drop a breakout that closed less than min_gap above its pivot."""
    def f(engine, slot, p_row, ti, pend) -> bool:
        return float(engine._md.close[p_row]) >= pend[0] * (1 + min_gap)
    return f


def both():
    a, b = rs_at_least(), clear_of_pivot()
    return lambda *args: b(*args) and a(*args)


FILTERS = {
    "B2a RS >= 41": rs_at_least,
    "B2b close >= pivot +0.8%": clear_of_pivot,
    "B2c both": both,
}
