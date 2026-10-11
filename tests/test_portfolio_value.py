"""Regression tests for bot-capital semantics in PortfolioManager.

2026-07-31 incident: live sizing used the whole Zerodha account value
(personal holdings + cash ≈ ₹5L) instead of the bot's ₹50k trading
capital, producing a ~10x oversized order that the cash gate blocked —
losing the first live signal. get_portfolio_value must always mean
trading equity: starting capital + bot P&L for the CURRENT mode only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from config.config import config


@pytest.fixture
def pm(tmp_path, monkeypatch):
    """PortfolioManager backed by a throwaway DB seeded with mixed trades."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path))
    db = sqlite3.connect(tmp_path / "portfolio_state.db")
    db.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, exit_date TEXT,"
        " realised_pnl REAL, is_paper INTEGER)"
    )
    db.execute(
        "CREATE TABLE open_positions (id INTEGER PRIMARY KEY, symbol TEXT,"
        " shares INTEGER, entry_price REAL, current_price REAL,"
        " partial_sold_shares INTEGER DEFAULT 0, is_paper INTEGER)"
    )
    db.execute("INSERT INTO trades VALUES (1, 'PAPERWIN', '2026-07-01', 394.37, 1)")
    db.execute("INSERT INTO trades VALUES (2, 'LIVEWIN', '2026-07-30', 100.0, 0)")
    db.commit()
    db.close()

    from broker.portfolio_manager import PortfolioManager
    return PortfolioManager(kite=None, starting_capital=50_000.0)


def test_realized_pnl_split_by_mode(pm, monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING_MODE", True)
    assert pm._get_total_realized_pnl() == pytest.approx(394.37)
    monkeypatch.setattr(config, "PAPER_TRADING_MODE", False)
    assert pm._get_total_realized_pnl() == pytest.approx(100.0)


def test_portfolio_value_is_trading_equity_not_account_value(pm, monkeypatch):
    """Live portfolio value = bot capital + live P&L; personal holdings in
    the Kite account must never inflate it."""
    monkeypatch.setattr(config, "PAPER_TRADING_MODE", False)

    class ExplodingKite:
        def get_holdings(self):
            raise AssertionError("sizing path must not read account holdings")

        def get_margins(self):
            raise AssertionError("sizing path must not read account margins")

    pm._kite = ExplodingKite()
    assert pm.get_portfolio_value() == pytest.approx(50_100.0)


def test_unrealized_pnl_counts_only_current_mode_positions(pm, monkeypatch):
    monkeypatch.setattr(config, "PAPER_TRADING_MODE", False)
    db = sqlite3.connect(Path(config.DB_PATH) / "portfolio_state.db")
    db.execute(
        "INSERT INTO open_positions VALUES (1, 'LIVEPOS', 10, 100.0, 110.0, 0, 0)"
    )
    db.execute(
        "INSERT INTO open_positions VALUES (2, 'PAPERPOS', 10, 100.0, 200.0, 0, 1)"
    )
    db.commit()
    db.close()
    # live: 50k + 100 realized + 10*(110-100) unrealized; paper position ignored
    assert pm.get_portfolio_value() == pytest.approx(50_200.0)
