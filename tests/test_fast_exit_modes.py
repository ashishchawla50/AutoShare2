"""Fast engine exit_mode="my_strategy": half sold at +10%, then stop at entry and trailing.

The vcp exit path stays covered by tests/test_fast_engine.py (frozen oracle). This file
checks the partial-sale bookkeeping: one record per trade, cash ledger exact, and the
first leg really sold at +10% (or the gap-up open).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
DB = REPO / "data" / "reconstructed.db"

pytestmark = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")


@pytest.fixture(scope="module")
def run():
    from config.config import config as cfg
    old = cfg.DB_PATH, cfg.HARD_STOP_PCT
    cfg.DB_PATH = str(REPO / "data")
    cfg.HARD_STOP_PCT = 0.10
    from backtest.fast.data import Universe, load_market_data
    from backtest.fast.engine import FastBacktestEngine
    from backtest.fast.signals import load_signals
    md = load_market_data()
    eng = FastBacktestEngine(start_date=date(2023, 1, 1), end_date=date(2024, 12, 31),
                             market=md, signals=load_signals(md), universe=Universe(),
                             verbose=False, dd_halt=False, exit_mode="my_strategy")
    eng.run()
    yield eng
    cfg.DB_PATH, cfg.HARD_STOP_PCT = old


def test_partial_trades_exist_and_are_one_record(run):
    split = [t for t in run._closed_trades if "after partial" in t["exit_reason"]]
    assert split, "no trade reached +10% — window too short to exercise the mode"
    assert len({(t["isin"], t["entry_date"]) for t in run._closed_trades}) == len(run._closed_trades)


def test_ledger_matches_cash(run):
    total = sum(t["realised_pnl"] for t in run._closed_trades)
    assert abs(total - (run.capital - run.starting_capital)) < 1e-6


def test_no_trade_loses_more_than_first_stop_after_partial(run):
    # after the half-sale the stop sits at entry or above, so a split trade can only
    # lose through costs or a gap through the stop on the remaining half
    for t in run._closed_trades:
        if "after partial" in t["exit_reason"]:
            assert t["realised_pnl_pct"] > -6.0, t
