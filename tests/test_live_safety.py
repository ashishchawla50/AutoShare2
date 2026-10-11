"""Regression tests for the 2026-10-11 Phase 1 audit live-safety fixes.

C1: check_exits raised UnboundLocalError on the first live trailing-stop ratchet
    when ENABLE_PARTIAL_PROFIT is False, and the exception aborted every later
    position's exit checks.
M2: update_gtt_stop_loss deleted the old GTT before placing the new one.
H1: live available cash was the whole Zerodha account balance.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import config.config as cfg_module
import main
from broker.order_manager import OrderManager
from broker.portfolio_manager import PortfolioManager


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setattr(cfg_module.config, "PAPER_TRADING_MODE", False)


def _rising_df() -> pd.DataFrame:
    """80 rising closes: the 10-day EMA x 0.97 floor sits well above a -7% stop."""
    idx = pd.bdate_range(end=date.today(), periods=80)
    close = np.linspace(90, 110, 80)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6},
        index=idx,
    )


def _position(symbol: str, gtt: str = "111") -> dict:
    return {
        "symbol": symbol, "entry_price": 100.0, "stop_loss": 93.0, "shares": 10,
        "partial_sold_shares": 0, "gtt_order_id": gtt, "strategy": "vcp", "vcp_low": 80.0,
        "entry_date": (date.today() - timedelta(days=3)).isoformat(),
    }


def _bot(positions: list[dict], ohlcv: dict) -> tuple[main.TradingBot, MagicMock, MagicMock]:
    bot = object.__new__(main.TradingBot)
    bot._process_manual_exits = lambda: None
    pm = MagicMock()
    pm.get_open_positions.return_value = positions
    kite = MagicMock()
    kite.place_gtt.return_value = "222"
    bot._portfolio_manager = pm
    bot._universe_ohlcv = ohlcv
    bot._regime_state = None
    bot._kite = kite
    bot._days_in_bear = 0
    return bot, pm, kite


# ── C1: trailing-stop ratchet ────────────────────────────────────────────────


@pytest.mark.parametrize("partial_on", [False, True])
@pytest.mark.parametrize("paper", [False, True])
def test_stop_ratchet_updates_db_and_gtt(monkeypatch, partial_on, paper):
    monkeypatch.setattr(cfg_module.config, "PAPER_TRADING_MODE", paper)
    monkeypatch.setattr(cfg_module.config, "ENABLE_PARTIAL_PROFIT", partial_on)
    bot, pm, kite = _bot([_position("XYZ")], {"XYZ": _rising_df()})

    bot.check_exits()

    pm.update_stop_loss.assert_called_once()
    symbol, new_stop, new_gtt = pm.update_stop_loss.call_args.args
    assert symbol == "XYZ" and new_stop > 93.0
    pm.update_current_prices.assert_called_once()
    if paper:
        kite.place_gtt.assert_not_called()
        assert new_gtt is None
    else:
        assert kite.place_gtt.call_args.kwargs["quantity"] == 10
        assert kite.place_gtt.call_args.kwargs["trigger_price"] == pytest.approx(new_stop)
        kite.delete_gtt.assert_called_once_with("111")
        assert new_gtt == "222"


def test_one_failing_position_does_not_block_others(live_mode, monkeypatch):
    bad, good = _position("BAD"), _position("GOOD")
    bot, pm, _ = _bot([bad, good], {"BAD": _rising_df(), "GOOD": _rising_df()})
    real_breakdown = main.check_technical_breakdown

    def breakdown(symbol, *args, **kwargs):
        if symbol == "BAD":
            raise RuntimeError("boom")
        return real_breakdown(symbol, *args, **kwargs)

    monkeypatch.setattr(main, "check_technical_breakdown", breakdown)
    alerts = MagicMock()
    monkeypatch.setattr(main, "send_system_error", alerts)

    bot.check_exits()

    updated = [c.args[0] for c in pm.update_stop_loss.call_args_list]
    assert updated == ["GOOD"]
    pm.update_current_prices.assert_called_once()
    alerts.assert_called_once()
    assert "BAD" in alerts.call_args.args[1]


# ── M2: GTT replacement order ────────────────────────────────────────────────


def test_gtt_update_places_new_before_deleting_old(live_mode):
    kite = MagicMock()
    calls = []
    kite.place_gtt.side_effect = lambda **kw: calls.append("place") or "222"
    kite.delete_gtt.side_effect = lambda gid: calls.append("delete")

    assert OrderManager(kite).update_gtt_stop_loss("111", "XYZ", 98.0, 10) == "222"
    assert calls == ["place", "delete"]


def test_gtt_update_failure_keeps_old_gtt(live_mode):
    kite = MagicMock()
    kite.place_gtt.return_value = None

    assert OrderManager(kite).update_gtt_stop_loss("111", "XYZ", 98.0, 10) is None
    kite.delete_gtt.assert_not_called()


# ── H1: live cash is capped at the bot's own cash ────────────────────────────


def _live_pm(monkeypatch, broker_cash: float, invested: float, realised: float = 0.0,
             unrealised: float = 0.0) -> PortfolioManager:
    kite = MagicMock()
    kite.get_margins.return_value = {"equity": {"available": {"live_balance": broker_cash}}}
    pm = PortfolioManager(kite, starting_capital=50_000.0)
    monkeypatch.setattr(pm, "_get_invested_value", lambda: invested)
    monkeypatch.setattr(pm, "_get_total_realized_pnl", lambda: realised)
    monkeypatch.setattr(pm, "_get_total_unrealized_pnl", lambda: unrealised)
    return pm


def test_owner_cash_does_not_extend_bot_capital(live_mode, monkeypatch):
    # ₹5L in the account, four ₹10k positions open: the bot's ₹50k minus ₹40k
    # invested minus the ₹10k reserve leaves nothing for a fifth position
    pm = _live_pm(monkeypatch, broker_cash=500_000.0, invested=40_000.0)
    assert pm.get_available_cash() == pytest.approx(0.0)


def test_bot_cash_includes_realised_pnl_and_reserve(live_mode, monkeypatch):
    pm = _live_pm(monkeypatch, broker_cash=500_000.0, invested=10_000.0, realised=5_000.0)
    # bot cash 50k + 5k - 10k = 45k; reserve 20% of 55k equity = 11k
    assert pm.get_available_cash() == pytest.approx(34_000.0)


def test_broker_cash_still_binds_when_lower(live_mode, monkeypatch):
    pm = _live_pm(monkeypatch, broker_cash=3_000.0, invested=0.0)
    assert pm.get_available_cash() == pytest.approx(3_000.0)
