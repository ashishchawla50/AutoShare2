"""Realistic backtest cost model (re-exports from portfolio/pnl_calculator).

Keeps the backtest layer self-contained and avoids circular imports.
"""
from portfolio.pnl_calculator import (
    CNC_DELIVERY_COSTS,
    TradeCosts,
    calculate_buy_cost,
    calculate_net_pnl,
    calculate_sell_cost,
)

COST_PROFILES = {
    "legacy": TradeCosts(),        # what every published result was run under
    "cnc": CNC_DELIVERY_COSTS,     # real Zerodha CNC delivery charges
}

__all__ = [
    "CNC_DELIVERY_COSTS", "COST_PROFILES", "TradeCosts",
    "calculate_buy_cost", "calculate_sell_cost", "calculate_net_pnl",
]
