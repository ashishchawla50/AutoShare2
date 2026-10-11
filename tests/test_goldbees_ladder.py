"""GOLDBEES lot-ladder alert: messages, lot logic, Zerodha sync, scheduler wiring."""

from __future__ import annotations

import pytest

from alerts import goldbees_ladder as gl
from config.config import config


class FakeKite:
    """Read-only Kite stand-in. Any order call is recorded so tests can prove none happen."""

    def __init__(self, holdings=None, ltp=100.0, connected=True):
        self.holdings = holdings if holdings is not None else [{"tradingsymbol": "RELIANCE", "quantity": 5}]
        self.ltp = ltp
        self.is_connected = connected
        self.order_calls = []

    def get_ltp(self, instruments):
        return {instruments[0]: self.ltp} if self.ltp else {}

    def get_holdings(self):
        return self.holdings

    def place_order(self, *a, **kw):
        self.order_calls.append((a, kw))

    def place_gtt(self, *a, **kw):
        self.order_calls.append((a, kw))


def gb(qty, avg, t1=0):
    return [{"tradingsymbol": "RELIANCE", "quantity": 5},
            {"tradingsymbol": "GOLDBEES", "quantity": qty, "t1_quantity": t1, "average_price": avg}]


@pytest.fixture
def sent(monkeypatch):
    msgs: list[str] = []
    monkeypatch.setattr(gl, "send_text", msgs.append)
    return msgs


def lot(i, units, price, date="2026-03-23"):
    return {"id": i, "units": units, "price": price, "date": date}


# ── check(): the 10:15 decision ───────────────────────────────────────────────

def test_sell_message_matches_agreed_format():
    state = gl.new_state(0, 0.0, 120.0)
    state["lots"] = [lot(1, 87, 113.98)] + [lot(i, 80, 125.0 + i, "2026-06-10") for i in range(2, 7)]
    state["last_trade_price"] = 121.0
    _, msgs = gl.check(state, 123.46, "2026-10-12")
    assert msgs == [
        "🔺 SELL 87 units, the lot bought 23 Mar @ ₹113.98. Now ₹123.46 (+8.3%). Open lots left: 5."
    ]


def test_lot_is_never_flagged_below_its_own_target():
    state = gl.new_state(0, 0.0, 100.0)
    state["lots"] = [lot(1, 100, 100.0)]
    state["last_trade_price"] = 100.0
    _, msgs = gl.check(state, 107.99, "2026-10-12")
    assert msgs == []
    _, msgs = gl.check(state, 108.0, "2026-10-12")
    assert msgs[0].startswith("🔺 SELL 100 units")
    assert state["pending_sell_ids"] == [1]


def test_each_lot_uses_its_own_price_not_the_average():
    # Bought at 100 and 90 (average 95): at 97.20 only the 90-lot is due.
    state = gl.new_state(0, 0.0, 100.0)
    state["lots"] = [lot(1, 1, 100.0, "2026-10-01"), lot(2, 1, 90.0, "2026-10-02")]
    state["last_trade_price"] = 90.0
    _, msgs = gl.check(state, 97.2, "2026-10-12")
    assert len(msgs) == 1 and "@ ₹90.00" in msgs[0] and "Open lots left: 1" in msgs[0]


def test_buy_alert_at_drop_threshold():
    state = gl.new_state(0, 0.0, 100.0)
    state["lots"] = [lot(1, 100, 100.0)]
    state["last_trade_price"] = 100.0
    _, msgs = gl.check(dict(state, lots=list(state["lots"])), 98.6, "2026-10-12")
    assert msgs == []
    state, msgs = gl.check(state, 98.5, "2026-10-12")
    units = int(config.GOLDBEES_LADDER_BUY_AMOUNT // 98.5)
    assert msgs == [f"🔻 BUY {units} units (₹{units * 98.5:,.0f}) @ ₹98.50. "
                    f"Down 1.5% from ₹100.00. Open lots after buy: 2/{config.GOLDBEES_LADDER_MAX_LOTS}."]
    assert state["last_trade_price"] == 98.5


def test_first_buy_waits_for_a_dip_from_the_high():
    state = gl.new_state(0, 0.0, 100.0)
    state, msgs = gl.check(state, 104.0, "2026-10-12")          # rising: high moves up, no alert
    assert msgs == [] and state["high_since_flat"] == 104.0
    state, msgs = gl.check(state, 102.4, "2026-10-12")          # 1.54% below the 104 high
    assert msgs and msgs[0].startswith("🔻 BUY")


def test_lot_cap_blocks_buy_alert():
    state = gl.new_state(0, 0.0, 100.0)
    state["lots"] = [lot(i, 10, 100.0) for i in range(1, config.GOLDBEES_LADDER_MAX_LOTS + 1)]
    state["last_trade_price"] = 100.0
    _, msgs = gl.check(state, 98.0, "2026-10-12")
    assert len(msgs) == 1 and "no buy" in msgs[0]


# ── sync(): match the lot list to Zerodha ─────────────────────────────────────

def test_sync_adds_bought_units_as_lot_with_inferred_price():
    state = gl.new_state(100, 120.0, 118.0)       # 100 core units @ avg 120
    # Owner bought 80 units @ 115.50: avg becomes (100*120 + 80*115.5)/180 = 118.0
    state, msgs = gl.sync(state, 180, 118.0, 116.0, "2026-10-13")
    assert state["lots"] == [{"id": 1, "units": 80, "price": 115.5, "date": "2026-10-13"}]
    assert state["last_trade_price"] == 115.5
    assert "80 more units" in msgs[0]


def test_new_lot_is_dated_on_the_buy_alert_day_not_the_sync_day():
    state = gl.new_state(0, 0.0, 100.0)
    state["lots"] = [lot(1, 100, 100.0)]
    state["last_trade_price"] = 100.0
    state, _ = gl.check(state, 98.0, "2026-10-12")
    state["kite_qty"], state["kite_avg"] = 100, 100.0
    state, _ = gl.sync(state, 202, 99.0, 98.5, "2026-10-13")   # 102 units @ 98.04 bought
    assert state["lots"][-1]["date"] == "2026-10-12"
    assert state["pending_buy_date"] is None


def test_sync_after_sell_alert_keeps_the_alerted_sell_price_as_anchor():
    state = gl.new_state(0, 0.0, 100.0)
    state["lots"] = [lot(1, 100, 100.0)]
    state["last_trade_price"] = 100.0
    state, _ = gl.check(state, 108.5, "2026-10-12")            # SELL alert at 108.50
    state, _ = gl.sync(state, 0, 0.0, 111.0, "2026-10-13")     # sold; price moved on
    assert state["last_trade_price"] == 108.5 and state["high_since_flat"] == 108.5


def test_sync_falls_back_to_ltp_when_inferred_price_is_absurd():
    state = gl.new_state(100, 120.0, 118.0)
    state, _ = gl.sync(state, 180, 50.0, 116.0, "2026-10-13")
    assert state["lots"][0]["price"] == 116.0


def test_sync_counts_unsettled_t1_units(sent):
    gl.save_state(gl.new_state(10, 100.0, 100.0))
    gl.run_sync(FakeKite(holdings=gb(10, 100.0, t1=0), ltp=100.0))
    assert gl.load_state()["lots"] == [] and sent == []
    gl.run_sync(FakeKite(holdings=gb(10, 99.5, t1=20), ltp=99.0))   # 20 bought yesterday, not settled
    assert gl.load_state()["lots"][0]["units"] == 20


def test_sync_removes_sold_units_from_the_alerted_lot_first():
    state = gl.new_state(10, 0.0, 120.0)
    state["lots"] = [lot(1, 80, 110.0), lot(2, 85, 130.0)]
    state["pending_sell_ids"] = [2]
    state["last_trade_price"] = 140.4                  # set by the SELL alert
    state, msgs = gl.sync(state, 10 + 80, 0.0, 141.9, "2026-10-13")
    assert [l["id"] for l in state["lots"]] == [1]
    assert state["pending_sell_ids"] == [] and state["last_trade_price"] == 140.4
    assert "85 fewer units" in msgs[0]


def test_sync_without_alert_removes_cheapest_lot_then_core():
    state = gl.new_state(10, 0.0, 120.0)
    state["lots"] = [lot(1, 80, 125.0), lot(2, 85, 110.0)]
    state, _ = gl.sync(state, 0, 0.0, 120.0, "2026-10-13")
    assert state["lots"] == [] and state["core_units"] == 0
    assert state["high_since_flat"] == 120.0


# ── jobs ──────────────────────────────────────────────────────────────────────

def test_first_sync_records_existing_units_as_core(sent):
    gl.run_sync(FakeKite(holdings=gb(500, 95.0), ltp=122.0))
    state = gl.load_state()
    assert state["core_units"] == 500 and state["lots"] == []
    assert "500 units" in sent[0] and "core" in sent[0]


def test_empty_holdings_response_never_wipes_lots(sent):
    state = gl.new_state(10, 100.0, 100.0)
    state["lots"] = [lot(1, 80, 110.0)]
    gl.save_state(state)
    gl.run_sync(FakeKite(holdings=[], ltp=100.0))   # Kite read error returns []
    assert gl.load_state()["lots"] == state["lots"] and sent == []


def test_jobs_never_place_orders(sent):
    kite = FakeKite(holdings=gb(0, 0.0), ltp=100.0)
    gl.run_sync(kite)
    for price in (100.0, 98.0, 96.0, 104.0, 110.0):
        kite.ltp = price
        gl.run_check(kite)
    assert kite.order_calls == []


def test_check_reports_disconnected_kite(sent):
    gl.run_check(FakeKite(connected=False))
    assert "not connected" in sent[0]


def test_disabled_flag_silences_everything(sent, monkeypatch):
    monkeypatch.setattr(config, "GOLDBEES_LADDER_ENABLED", False)
    gl.run_sync(FakeKite(holdings=gb(10, 100.0)))
    gl.run_check(FakeKite())
    assert sent == [] and gl.load_state() is None


def test_state_file_lives_under_isolated_data_dir(tmp_path):
    assert gl._state_path().parent == tmp_path / "data"


def test_scheduler_registers_ladder_jobs_at_10_10_and_10_15():
    from scheduler import build_scheduler
    sched = build_scheduler(bot=object())
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "goldbees_ladder_sync" in jobs and "goldbees_ladder_check" in jobs
    assert config.SCHEDULE["goldbees_ladder_sync"] == "10 10 * * *"
    assert config.SCHEDULE["goldbees_ladder_check"] == "15 10 * * *"
