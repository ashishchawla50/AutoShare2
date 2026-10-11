"""P&L calculation with full cost model.

All trading costs on a round trip:
  Brokerage:          ₹40 (₹20 × 2)
  STT buy:            0.1% on buy value
  STT sell:           0.1% on sell value
  Exchange charges:   0.00335% NSE both sides
  SEBI charges:       0.0001% both sides
  Stamp duty:         0.015% on buy only
  GST:                18% on (brokerage + exchange charges)
  Slippage:           0.1% each side = 0.2% round trip

All amounts in rupees. No floats for the final P&L — use float with careful rounding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config.config import config

logger = logging.getLogger("trading.pnl")


@dataclass
class TradeCosts:
    """All costs on a single round trip (buy + sell).

    The defaults are the LEGACY profile every published backtest result was
    produced under (including vcp_v2's PF 1.11 dev / 1.61 OOS). They overstate
    real CNC delivery cost — see CNC_DELIVERY_COSTS below — so they are kept
    as the default purely so no historical number silently changes.
    """
    brokerage_per_side: float = config.COST_LEGACY_BROKERAGE_PER_SIDE   # ₹ per order (intraday fee)
    stt_buy_pct: float = config.COST_STT_BUY_PCT
    stt_sell_pct: float = config.COST_STT_SELL_PCT
    exchange_charge_pct: float = config.COST_LEGACY_EXCHANGE_PCT         # NSE per side
    sebi_charge_pct: float = config.COST_SEBI_PCT                        # per side
    stamp_duty_pct: float = config.COST_STAMP_DUTY_PCT                   # buy only
    gst_rate: float = config.COST_GST_RATE                               # on brokerage + exchange
    slippage_pct: float = config.COST_SLIPPAGE_PCT                       # per side (market impact)
    dp_charge_per_sell: float = 0.0         # ₹ per scrip per sell day (CDSL)


# Real Zerodha CNC delivery charges (2026). Differences from the legacy
# profile, all of which matter most at small position sizes:
#   brokerage  ₹20/side → ₹0   (delivery is free; ₹20 is the intraday rate)
#   exchange   0.00335% → 0.00297% (post-Oct-2024 NSE rate)
#   DP charge  missing  → ₹13.5 + 18% GST per scrip per sell day
# Net effect: fixed cost per round trip falls from ₹47.20 to ₹15.93. At a
# ₹10k position that is 0.70% → 0.38% of position; at ₹1L it is 0.47% →
# 0.24%. Slippage is left at 0.1%/side — a market-impact assumption, not a
# fee. See DUST_FLOOR_DECISION.md for the full derivation.
CNC_DELIVERY_COSTS = TradeCosts(
    brokerage_per_side=config.COST_CNC_BROKERAGE_PER_SIDE,
    exchange_charge_pct=config.COST_CNC_EXCHANGE_PCT,
    dp_charge_per_sell=config.COST_CNC_DP_CHARGE * (1 + config.COST_GST_RATE),
)

_DEFAULT_COSTS = TradeCosts()


def calculate_buy_cost(
    buy_value: float,
    costs: TradeCosts = _DEFAULT_COSTS,
) -> float:
    """Total cost of placing a buy order.

    Args:
        buy_value: Total value of shares bought (price × shares).

    Returns:
        Total buy-side cost in rupees.
    """
    brokerage = costs.brokerage_per_side
    stt = buy_value * costs.stt_buy_pct
    exchange = buy_value * costs.exchange_charge_pct
    sebi = buy_value * costs.sebi_charge_pct
    stamp = buy_value * costs.stamp_duty_pct
    slippage = buy_value * costs.slippage_pct
    gst = (brokerage + exchange) * costs.gst_rate

    return brokerage + stt + exchange + sebi + stamp + slippage + gst


def calculate_sell_cost(
    sell_value: float,
    costs: TradeCosts = _DEFAULT_COSTS,
) -> float:
    """Total cost of placing a sell order.

    Args:
        sell_value: Total value of shares sold (price × shares).

    Returns:
        Total sell-side cost in rupees.
    """
    brokerage = costs.brokerage_per_side
    stt = sell_value * costs.stt_sell_pct
    exchange = sell_value * costs.exchange_charge_pct
    sebi = sell_value * costs.sebi_charge_pct
    slippage = sell_value * costs.slippage_pct
    gst = (brokerage + exchange) * costs.gst_rate

    return brokerage + stt + exchange + sebi + slippage + gst + costs.dp_charge_per_sell


def calculate_net_pnl(
    entry_price: float,
    exit_price: float,
    shares: int,
    costs: TradeCosts = _DEFAULT_COSTS,
) -> tuple[float, float]:
    """Calculate net P&L after all costs.

    Args:
        entry_price: Buy price per share.
        exit_price:  Sell price per share.
        shares:      Number of shares.

    Returns:
        (net_pnl_rupees, net_pnl_pct) where pnl_pct is relative to buy value.
    """
    buy_value = entry_price * shares
    sell_value = exit_price * shares

    gross_pnl = sell_value - buy_value
    total_costs = calculate_buy_cost(buy_value, costs) + calculate_sell_cost(sell_value, costs)
    net_pnl = gross_pnl - total_costs

    pnl_pct = net_pnl / buy_value if buy_value > 0 else 0.0
    return net_pnl, pnl_pct


def estimate_break_even_price(
    entry_price: float,
    shares: int,
    costs: TradeCosts = _DEFAULT_COSTS,
) -> float:
    """Return the minimum exit price to break even after all costs.

    Args:
        entry_price: Buy price per share.
        shares:      Number of shares.

    Returns:
        Break-even sell price per share.
    """
    buy_value = entry_price * shares
    buy_side_costs = calculate_buy_cost(buy_value, costs)

    # Approximate: solve for exit_price where net_pnl = 0
    # sell_value - buy_value - buy_cost - sell_cost = 0
    # sell_value - sell_cost ≈ sell_value * (1 - sell_cost_pct)
    sell_cost_pct = (
        costs.stt_sell_pct +
        costs.exchange_charge_pct * (1 + costs.gst_rate) +
        costs.sebi_charge_pct +
        costs.slippage_pct +
        costs.brokerage_per_side / buy_value  # fixed brokerage as pct
    )
    break_even_sell_value = (buy_value + buy_side_costs) / (1 - sell_cost_pct)
    return break_even_sell_value / shares
