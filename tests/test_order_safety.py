"""Tests for the live-order safety hardening (2026-08-06).

Covers: broker fill-price retrieval, sell-failure handling in OrderManager,
GTT backup monitor quantity/mode/idempotency fixes, the reverse-direction
reconcile query, and the auth-server operator secret.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

import config.config as cfg_module
from broker.order_manager import OrderManager


@pytest.fixture
def live_mode(monkeypatch):
    """Flip config to live mode so the real broker code paths run (mocked Kite)."""
    monkeypatch.setattr(cfg_module.config, "PAPER_TRADING_MODE", False)


# ── OrderManager.get_fill_price ──────────────────────────────────────────────


class TestGetFillPrice:
    def test_paper_mode_returns_none(self):
        assert OrderManager(MagicMock()).get_fill_price("PAPER_X_1") is None

    def test_none_order_id_returns_none(self, live_mode):
        assert OrderManager(MagicMock()).get_fill_price(None) is None

    def test_returns_broker_average_price(self, live_mode):
        kite = MagicMock()
        kite.get_order_history.return_value = [
            {"status": "OPEN"},
            {"status": "COMPLETE", "average_price": 101.35},
        ]
        assert OrderManager(kite).get_fill_price("123") == pytest.approx(101.35)

    def test_zero_average_price_returns_none(self, live_mode):
        kite = MagicMock()
        kite.get_order_history.return_value = [
            {"status": "COMPLETE", "average_price": 0}
        ]
        assert OrderManager(kite).get_fill_price("123") is None

    def test_incomplete_order_returns_none(self, live_mode):
        kite = MagicMock()
        kite.get_order_history.return_value = [{"status": "REJECTED"}]
        assert OrderManager(kite).get_fill_price("123") is None


# ── OrderManager.sell_cnc failure paths ──────────────────────────────────────


class TestSellCncFailures:
    def test_rejected_sell_returns_none(self, live_mode):
        kite = MagicMock()
        kite.place_order.return_value = None
        assert OrderManager(kite).sell_cnc("ABC", 10, order_type="MARKET") is None

    def test_market_upgrade_rejection_returns_none(self, live_mode, monkeypatch):
        kite = MagicMock()
        kite.place_order.side_effect = ["OID1", None]  # limit ok, market rejected
        om = OrderManager(kite)
        monkeypatch.setattr(om, "_wait_for_fill", lambda oid, timeout=30: "TIMEOUT")
        assert om.sell_cnc("ABC", 10, order_type="LIMIT", limit_price=100.0) is None
        kite.cancel_order.assert_called_once_with("OID1")

    def test_market_upgrade_unfilled_returns_none(self, live_mode, monkeypatch):
        kite = MagicMock()
        kite.place_order.side_effect = ["OID1", "OID2"]
        om = OrderManager(kite)
        monkeypatch.setattr(om, "_wait_for_fill", lambda oid, timeout=30: "TIMEOUT")
        assert om.sell_cnc("ABC", 10, order_type="LIMIT", limit_price=100.0) is None

    def test_market_upgrade_fill_confirmed(self, live_mode, monkeypatch):
        kite = MagicMock()
        kite.place_order.side_effect = ["OID1", "OID2"]
        om = OrderManager(kite)
        statuses = iter(["TIMEOUT", "COMPLETE"])
        monkeypatch.setattr(om, "_wait_for_fill", lambda oid, timeout=30: next(statuses))
        assert om.sell_cnc("ABC", 10, order_type="LIMIT", limit_price=100.0) == "OID2"


# ── GTT backup monitor ───────────────────────────────────────────────────────


@pytest.fixture
def gtt_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg_module.config, "DB_PATH", str(tmp_path))
    db = sqlite3.connect(tmp_path / "portfolio_state.db")
    db.execute(
        "CREATE TABLE open_positions (symbol TEXT, shares INTEGER, "
        "partial_sold_shares INTEGER, stop_loss REAL, is_paper INTEGER)"
    )
    # Paper position with a partial booking, and a live position
    db.execute("INSERT INTO open_positions VALUES ('AAA', 100, 40, 95.0, 1)")
    db.execute("INSERT INTO open_positions VALUES ('BBB', 50, 0, 200.0, 0)")
    db.commit()
    db.close()
    from broker.gtt_monitor import GTTBackupMonitor

    monitor = GTTBackupMonitor(MagicMock())
    monkeypatch.setattr("broker.gtt_monitor.is_market_open", lambda: True)
    return monitor


class TestGTTBackupMonitor:
    def test_remaining_shares_and_mode_filter(self, gtt_monitor):
        # Paper mode (conftest): only the paper row is visible, at 100-40 shares
        positions = gtt_monitor._load_open_positions()
        assert positions == [
            {"symbol": "AAA", "quantity": 60, "stop_loss_price": 95.0}
        ]

    def test_live_mode_sees_only_live_rows(self, gtt_monitor, live_mode):
        positions = gtt_monitor._load_open_positions()
        assert positions == [
            {"symbol": "BBB", "quantity": 50, "stop_loss_price": 200.0}
        ]

    def test_failed_sell_retries_then_gives_up(self, gtt_monitor, monkeypatch):
        monkeypatch.setattr(gtt_monitor, "_fetch_ltps", lambda syms: {"AAA": 90.0})
        calls = []
        monkeypatch.setattr(
            gtt_monitor, "_fire_market_sell", lambda s, q: calls.append((s, q)) or False
        )
        gtt_monitor.run_once()
        assert "AAA" not in gtt_monitor._triggered  # failure is not "handled"
        gtt_monitor.run_once()
        gtt_monitor.run_once()
        # Gave up after MAX_SELL_ATTEMPTS — marked handled to stop the spam
        assert "AAA" in gtt_monitor._triggered
        assert len(calls) == gtt_monitor.MAX_SELL_ATTEMPTS
        # And it always sold the remaining quantity, not the original shares
        assert all(q == 60 for _, q in calls)

    def test_successful_sell_marked_handled(self, gtt_monitor, monkeypatch):
        monkeypatch.setattr(gtt_monitor, "_fetch_ltps", lambda syms: {"AAA": 90.0})
        monkeypatch.setattr(gtt_monitor, "_fire_market_sell", lambda s, q: True)
        gtt_monitor.run_once()
        assert "AAA" in gtt_monitor._triggered


# ── Reverse-direction reconcile query ────────────────────────────────────────


class TestRecentlyClosedTrades:
    def test_returns_only_recent_trades_in_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg_module.config, "DB_PATH", str(tmp_path))
        db = sqlite3.connect(tmp_path / "portfolio_state.db")
        db.execute(
            "CREATE TABLE trades (symbol TEXT, shares INTEGER, exit_date TEXT, "
            "exit_reason TEXT, is_paper INTEGER)"
        )
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=30)).isoformat()
        db.execute(f"INSERT INTO trades VALUES ('NEW', 10, '{today}', 'stop_loss', 1)")
        db.execute(f"INSERT INTO trades VALUES ('OLD', 10, '{old}', 'stop_loss', 1)")
        db.execute(f"INSERT INTO trades VALUES ('LIVE', 10, '{today}', 'stop_loss', 0)")
        db.commit()
        db.close()

        from broker.portfolio_manager import PortfolioManager

        pm = PortfolioManager()
        symbols = {t["symbol"] for t in pm.get_recently_closed_trades(days=7)}
        assert symbols == {"NEW"}  # old trade and live-mode trade excluded


# ── Auth server /logout protection ───────────────────────────────────────────


class TestLogoutEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        import broker.auth_server as srv

        monkeypatch.setattr(srv, "_auth", MagicMock())
        return srv.app.test_client()

    def test_disabled_when_no_secret(self, client, monkeypatch):
        monkeypatch.setattr(cfg_module.config, "AUTH_OPERATOR_SECRET", "")
        assert client.post("/logout").status_code == 403

    def test_wrong_secret_rejected(self, client, monkeypatch):
        monkeypatch.setattr(cfg_module.config, "AUTH_OPERATOR_SECRET", "s3cret")
        resp = client.post("/logout", headers={"X-Operator-Secret": "wrong"})
        assert resp.status_code == 403

    def test_correct_secret_accepted(self, client, monkeypatch):
        monkeypatch.setattr(cfg_module.config, "AUTH_OPERATOR_SECRET", "s3cret")
        resp = client.post("/logout", headers={"X-Operator-Secret": "s3cret"})
        assert resp.status_code == 200
