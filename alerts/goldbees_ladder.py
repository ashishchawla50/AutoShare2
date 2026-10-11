"""GOLDBEES lot-ladder alert. ALERT-ONLY: this module never places an order.

The owner's personal side alert (added 2026-10-12), separate from the trading
strategy. Every ladder buy is a lot that keeps its own buy price:

* BUY when the 10:15 price is GOLDBEES_LADDER_BUY_DROP_PCT below the last trade
  price (or below the high since the ladder was last empty), while fewer than
  GOLDBEES_LADDER_MAX_LOTS lots are open.
* SELL a lot when the price is GOLDBEES_LADDER_SELL_GAIN_PCT above that lot's
  own buy price. A lot is never flagged for sale at a loss.

The owner places the orders in Kite. Each morning ``run_sync`` re-reads the
Zerodha holdings and syncs the lot list to what is actually held: extra units
become a new lot (price inferred from Kite's average-price change), missing
units are taken off the lots the last SELL alert named, then the cheapest lots.
Units held when the ladder first starts are "core": they are never part of an
alert. State lives in ``<DB_PATH>/.goldbees_ladder.json``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from alerts.telegram_alert import send_text
from config.config import config
from utils.time_utils import today_ist

logger = logging.getLogger("trading.goldbees_ladder")


# ── State file ────────────────────────────────────────────────────────────────

def _state_path() -> Path:
    return Path(config.DB_PATH) / config.GOLDBEES_LADDER_STATE_FILE


def load_state() -> Optional[dict]:
    path = _state_path()
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def new_state(core_units: int, kite_avg: float, ltp: float) -> dict:
    return {
        "core_units": core_units,      # personal units, never in an alert
        "lots": [],                    # [{"id", "units", "price", "date"}]
        "next_id": 1,
        "last_trade_price": None,      # anchor for the next BUY
        "high_since_flat": ltp,        # BUY anchor while no lot is open
        "pending_sell_ids": [],        # lots named by the last SELL alerts
        "pending_buy_date": None,      # day of the last BUY alert (the lot's date)
        "kite_qty": core_units,        # last Zerodha snapshot, for price inference
        "kite_avg": kite_avg,
    }


# ── Pure logic (no I/O) ───────────────────────────────────────────────────────

def _fmt_day(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d.strftime('%b')}"


def _lot_label(lot: dict) -> str:
    return f"the lot bought {_fmt_day(lot['date'])} @ ₹{lot['price']:.2f}"


def sync(state: dict, held_qty: int, held_avg: float, ltp: float, today: str) -> tuple[dict, list[str]]:
    """Match the lot list to the units Zerodha actually holds."""
    msgs: list[str] = []
    lots = state["lots"]
    expected = state["core_units"] + sum(l["units"] for l in lots)

    if held_qty > expected:
        extra = held_qty - expected
        price = ltp
        prev_qty, prev_avg = state.get("kite_qty"), state.get("kite_avg")
        if prev_qty is not None and held_qty > prev_qty and held_avg > 0:
            inferred = (held_qty * held_avg - prev_qty * (prev_avg or 0.0)) / (held_qty - prev_qty)
            if abs(inferred / ltp - 1) <= config.GOLDBEES_LADDER_PRICE_SANITY_PCT:
                price = inferred
        lot = {"id": state["next_id"], "units": extra, "price": round(price, 2),
               "date": state.get("pending_buy_date") or today}
        state["next_id"] += 1
        lots.append(lot)
        state["last_trade_price"] = lot["price"]
        msgs.append(
            f"➕ GOLDBEES sync: Zerodha shows {extra} more units. "
            f"Added as a new lot @ ₹{lot['price']:.2f}. Open lots: {len(lots)}."
        )

    elif held_qty < expected:
        missing = expected - held_qty
        alerted = bool(state["pending_sell_ids"])
        pending = [l for i in state["pending_sell_ids"] for l in lots if l["id"] == i]
        rest = sorted((l for l in lots if l not in pending), key=lambda l: l["price"])
        removed: list[str] = []
        for lot in pending + rest:
            if missing == 0:
                break
            take = min(missing, lot["units"])
            lot["units"] -= take
            missing -= take
            removed.append(f"{take} units from {_lot_label(lot)}")
        state["lots"] = lots = [l for l in lots if l["units"] > 0]
        if missing:
            state["core_units"] -= missing
            removed.append(f"{missing} core units")
        if not alerted:
            # A sale no alert asked for: today's price is the best estimate of it.
            # After a SELL alert, check() already anchored on the alerted price.
            state["last_trade_price"] = ltp
            if not lots:
                state["high_since_flat"] = ltp
        msgs.append(
            f"➖ GOLDBEES sync: Zerodha shows {expected - held_qty} fewer units. "
            f"Removed {'; '.join(removed)}. Open lots: {len(lots)}."
        )

    state["pending_sell_ids"] = []
    state["pending_buy_date"] = None
    state["kite_qty"] = held_qty
    state["kite_avg"] = held_avg
    return state, msgs


def check(state: dict, ltp: float, today: str) -> tuple[dict, list[str]]:
    """The 10:15 decision: one BUY alert, or a SELL alert per lot at target."""
    lots = state["lots"]
    max_lots = config.GOLDBEES_LADDER_MAX_LOTS

    if lots:
        ref = state["last_trade_price"] or min(l["price"] for l in lots)
    else:
        high = max(state["high_since_flat"] or ltp, ltp)
        state["high_since_flat"] = high
        ref = min(state["last_trade_price"] or high, high)

    if ltp <= ref * (1 - config.GOLDBEES_LADDER_BUY_DROP_PCT):
        state["last_trade_price"] = ltp
        drop = (1 - ltp / ref) * 100
        if len(lots) < max_lots:
            state["pending_buy_date"] = today
            units = int(config.GOLDBEES_LADDER_BUY_AMOUNT // ltp)
            return state, [
                f"🔻 BUY {units} units (₹{units * ltp:,.0f}) @ ₹{ltp:.2f}. "
                f"Down {drop:.1f}% from ₹{ref:.2f}. Open lots after buy: {len(lots) + 1}/{max_lots}."
            ]
        return state, [
            f"🔻 GOLDBEES @ ₹{ltp:.2f} is {drop:.1f}% below ₹{ref:.2f}, "
            f"but all {max_lots} lots are open, so no buy."
        ]

    due = sorted(
        (l for l in lots if ltp >= l["price"] * (1 + config.GOLDBEES_LADDER_SELL_GAIN_PCT)),
        key=lambda l: l["price"],
    )
    msgs = []
    for k, lot in enumerate(due, start=1):
        gain = (ltp / lot["price"] - 1) * 100
        msgs.append(
            f"🔺 SELL {lot['units']} units, {_lot_label(lot)}. "
            f"Now ₹{ltp:.2f} (+{gain:.1f}%). Open lots left: {len(lots) - k}."
        )
    if due:
        state["pending_sell_ids"] = [l["id"] for l in due]
        state["last_trade_price"] = ltp
        if len(due) == len(lots):
            state["high_since_flat"] = ltp
    return state, msgs


# ── Kite reads (read-only) ────────────────────────────────────────────────────

def _instrument() -> str:
    return f"NSE:{config.GOLDBEES_LADDER_SYMBOL}"


def _read_ltp(kite: Any) -> Optional[float]:
    return kite.get_ltp([_instrument()]).get(_instrument())


def _read_holding(kite: Any) -> Optional[tuple[int, float]]:
    """(units incl. unsettled T1, Kite average price), or None if unreadable.

    get_holdings() returns [] on an API error. The owner always holds other
    personal stocks, so an empty list is treated as an error, never as
    "everything sold" (which would wipe the lot list).
    """
    holdings = kite.get_holdings()
    if not holdings:
        return None
    for h in holdings:
        if h.get("tradingsymbol", "").upper() == config.GOLDBEES_LADDER_SYMBOL:
            qty = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
            return qty, float(h.get("average_price") or 0.0)
    return 0, 0.0


# ── Scheduler jobs ────────────────────────────────────────────────────────────

def run_sync(kite: Any) -> None:
    """10:10 job: sync the lot list with Zerodha holdings."""
    if not config.GOLDBEES_LADDER_ENABLED:
        return
    try:
        if not kite.is_connected:
            logger.warning("GOLDBEES ladder sync skipped: Kite not connected")
            return
        holding, ltp = _read_holding(kite), _read_ltp(kite)
        if holding is None or not ltp:
            logger.warning("GOLDBEES ladder sync skipped: holdings or price unavailable")
            return
        qty, avg = holding
        state = load_state()
        if state is None:
            save_state(new_state(qty, avg, ltp))
            send_text(
                f"🪙 GOLDBEES ladder started. Your current {qty} units (avg ₹{avg:.2f}) are "
                f"core: they are never part of a buy/sell alert. Daily check at 10:15."
            )
            logger.info("GOLDBEES ladder initialised: core=%d", qty)
            return
        state, msgs = sync(state, qty, avg, ltp, today_ist().isoformat())
        save_state(state)
        for m in msgs:
            send_text(m)
        logger.info("GOLDBEES ladder synced: held=%d lots=%d", qty, len(state["lots"]))
    except Exception as exc:
        logger.exception("GOLDBEES ladder sync failed")
        send_text(f"⚠️ GOLDBEES ladder sync failed: {exc}")


def run_check(kite: Any) -> None:
    """10:15 job: send a BUY or SELL alert if the price calls for one."""
    if not config.GOLDBEES_LADDER_ENABLED:
        return
    try:
        if not kite.is_connected:
            send_text("⚠️ GOLDBEES ladder: Kite not connected, today's check skipped.")
            return
        state = load_state()
        if state is None:
            logger.warning("GOLDBEES ladder check skipped: not initialised (sync has not run yet)")
            return
        ltp = _read_ltp(kite)
        if not ltp:
            send_text("⚠️ GOLDBEES ladder: price unavailable, today's check skipped.")
            return
        state, msgs = check(state, ltp, today_ist().isoformat())
        save_state(state)
        for m in msgs:
            send_text(m)
        logger.info("GOLDBEES ladder check @ %.2f: %d alert(s)", ltp, len(msgs))
    except Exception as exc:
        logger.exception("GOLDBEES ladder check failed")
        send_text(f"⚠️ GOLDBEES ladder check failed: {exc}")
