"""The manual blocklist must keep the owner's personal holdings out of the bot.

data/manual_blocklist.txt lists symbols the owner holds by hand (JIOFIN,
SILVERBEES, SILVERIETF, HDFCGOLD from 2026-07-17; seven more added 2026-10-12). The bot must never buy
them. It can't sell them either, because every sell path only acts on the
bot's own ledger, and the buy gate is what keeps them out of that ledger.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

import config.config as cfg_module
import main
from scanner.opportunity_ranker import TradeSetup

PERSONAL = ["JIOFIN", "SILVERBEES", "SILVERIETF", "HDFCGOLD"]


def _write_blocklist(lines: list[str]) -> None:
    Path(cfg_module.config.MANUAL_BLOCKLIST_PATH).write_text("\n".join(lines) + "\n")


def _setup(symbol: str) -> TradeSetup:
    return TradeSetup(
        symbol=symbol, rs_percentile=90.0, vcp_tightness_score=80.0, trend_template_score=8.0,
        volume_multiplier=2.0, pivot_price=100.0, stop_loss_price=90.0, entry_price=100.0,
        sector="Unknown", strategy="vcp",
    )


def _df() -> pd.DataFrame:
    idx = pd.bdate_range(end=date.today(), periods=30)
    close = np.full(30, 100.0)
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": 1e6}, index=idx)


@pytest.fixture
def entry_bot(monkeypatch):
    """A live-mode bot inside the entry window where every gate passes, so
    any setup that survives the blocklist reaches buy_cnc."""
    monkeypatch.setattr(cfg_module.config, "PAPER_TRADING_MODE", False)
    monkeypatch.setattr(main, "is_entry_window", lambda: True)
    monkeypatch.setattr(main, "is_emergency_halt_active", lambda: False)
    import strategy.breakout_detector as bd
    monkeypatch.setattr(bd, "evaluate_gap", lambda *a, **k: (bd.GapDecision.PROCEED, "ok"))
    import data_fetcher.fundamental_fetcher as ff
    monkeypatch.setattr(ff, "get_fundamentals", lambda s: {"sector": "Unknown"})
    order_mgr = MagicMock()
    order_mgr.buy_cnc.return_value = "OID"
    order_mgr.get_fill_price.return_value = 100.0
    order_mgr.place_gtt_stop_loss.return_value = "GTT"
    monkeypatch.setattr(main, "OrderManager", lambda kite: order_mgr)
    monkeypatch.setattr("alerts.telegram_alert.send_trade_entry", lambda **k: None)

    bot = object.__new__(main.TradingBot)
    bot._regime_state = SimpleNamespace(allow_new_trades=True, position_size_multiplier=1.0,
                                        regime=SimpleNamespace(value="BULL"))
    bot._drawdown_monitor = MagicMock(**{"check_all_limits.return_value": (True, "")})
    pm = MagicMock()
    pm.get_portfolio_value.return_value = 50_000.0
    pm.get_available_cash.return_value = 40_000.0
    bot._portfolio_manager = pm
    bot._risk_manager = MagicMock(**{"approve_entry.return_value": SimpleNamespace(approved=True, reason="")})
    kite = MagicMock()
    kite.get_ltp.side_effect = lambda keys: {k: 100.0 for k in keys}
    bot._kite = kite
    bot._trades_today = 0
    return bot, order_mgr


def test_blocklisted_symbols_never_bought(entry_bot):
    bot, order_mgr = entry_bot
    _write_blocklist(["# personal holdings", *PERSONAL])
    symbols = PERSONAL + ["GOODCO"]
    bot._universe_ohlcv = {s: _df() for s in symbols}
    bot._watchlist = [_setup(s) for s in symbols]

    bot.check_entries()

    bought = [c.kwargs["symbol"] for c in order_mgr.buy_cnc.call_args_list]
    assert bought == ["GOODCO"]          # control: the harness really does buy
    assert bot._watchlist == []          # blocked setups are dropped, not retried


def test_blocklist_is_case_insensitive_and_ignores_comments(entry_bot):
    bot, order_mgr = entry_bot
    _write_blocklist(["  jiofin   # held since 2023", "", "#HDFCGOLD"])
    bot._universe_ohlcv = {s: _df() for s in ("JIOFIN", "HDFCGOLD")}
    bot._watchlist = [_setup("JIOFIN"), _setup("HDFCGOLD")]

    bot.check_entries()

    # JIOFIN is blocked despite case and whitespace; a commented-out line blocks nothing
    assert [c.kwargs["symbol"] for c in order_mgr.buy_cnc.call_args_list] == ["HDFCGOLD"]


def test_blocklist_reread_on_every_entry_check(entry_bot):
    """No restart needed: adding a symbol takes effect on the next check."""
    bot, order_mgr = entry_bot
    _write_blocklist([])
    bot._universe_ohlcv = {"JIOFIN": _df()}
    _write_blocklist(["JIOFIN"])
    bot._watchlist = [_setup("JIOFIN")]

    bot.check_entries()

    order_mgr.buy_cnc.assert_not_called()


def test_manual_exit_never_sells_untracked_holdings(monkeypatch):
    """Writing a personal holding into manual_exit.txt must not sell it:
    the bot only exits positions in its own ledger."""
    monkeypatch.setattr(cfg_module.config, "PAPER_TRADING_MODE", False)
    monkeypatch.setattr("alerts.telegram_alert.send_text", lambda *a, **k: None)
    Path(cfg_module.config.MANUAL_EXIT_PATH).write_text("JIOFIN\n")
    bot = object.__new__(main.TradingBot)
    pm = MagicMock()
    pm.get_open_position.return_value = None    # not a bot position
    bot._portfolio_manager = pm
    bot._universe_ohlcv = {}
    bot._execute_exit = MagicMock()

    bot._process_manual_exits()

    bot._execute_exit.assert_not_called()
