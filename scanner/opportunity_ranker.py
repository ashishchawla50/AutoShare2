"""Rank and prioritise trade setups after all filters and strategy analysis pass.

Scoring weights:
  RS percentile     (35%): Market leadership signal
  VCP tightness     (25%): Quality of the base/contraction
  Trend template    (20%): Strength of the technical structure
  Volume on breakout(10%): Institutional participation confirmation
  ATR compression   (05%): Objective volatility contraction confirmation
  Weinstein Stage 2 (05%): Weekly timeframe confirmation bonus

Flat bonuses / penalties (added on top of weighted sum):
  52w-high proximity: +5 if within 10% of 52w high, +3 if within 15% (Paul)
  Sector penalty:     −10 if sector < 40% stocks in uptrend

No RS eligibility gate is applied here. The RS >= 70th-percentile drop was
removed 2026-08-10 (DIVERGENCE_INVENTORY.md A1): it was a live-only rule with
no counterpart anywhere in the validated backtest, so it silently made live
stricter than the ruleset that produced the published PF. RS still carries 35%
of the composite score, so weak-RS setups rank low rather than being discarded,
and RS >= 85 still flags market leaders via is_market_leader.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from config.config import config

logger = logging.getLogger("scanner.ranker")


@dataclass
class TradeSetup:
    symbol: str
    rs_percentile: float
    vcp_tightness_score: float
    trend_template_score: float
    volume_multiplier: float
    pivot_price: float
    stop_loss_price: float
    entry_price: float
    sector: str
    strategy: str = "vcp"               # "vcp" or "momentum"
    target_price: float = 0.0           # fixed target for momentum; 0 = trailing for vcp
    atr_compressed: bool = False        # from VCPResult.atr_compressed
    weinstein_stage_bonus: float = 0.0  # +10 if Stage 2
    sector_penalty: float = 0.0         # −10 if sector is weak
    pct_below_52w_high: float = 0.0     # (52w_high - price) / 52w_high; 0 = at high
    composite_score: float = 0.0
    is_market_leader: bool = False
    notes: list[str] = field(default_factory=list)


def score_setup(setup: TradeSetup) -> float:
    """Calculate the composite opportunity score (0–100).

    Args:
        setup: A TradeSetup with all required fields populated.

    Returns:
        Composite score float (0–100). Higher = better setup.
    """
    vol_score = min(100.0, ((setup.volume_multiplier - 2.0) / 3.0) * 100)
    vol_score = max(0.0, vol_score)

    atr_score = 100.0 if setup.atr_compressed else 0.0

    # Prasenjit Paul: stocks within 10% of 52w high are primed to break out
    if setup.pct_below_52w_high <= 0.10:
        proximity_bonus = 5.0
    elif setup.pct_below_52w_high <= 0.15:
        proximity_bonus = 3.0
    else:
        proximity_bonus = 0.0

    composite = (
        setup.rs_percentile          * 0.35
        + setup.vcp_tightness_score  * 0.25
        + setup.trend_template_score * 0.20
        + vol_score                  * 0.10
        + atr_score                  * 0.05
        + setup.weinstein_stage_bonus * 0.05
        + setup.sector_penalty        # flat penalty, already negative
        + proximity_bonus             # flat bonus: near 52w high = ready to break out
    )
    return round(max(0.0, composite), 2)


def rank_setups(setups: list[TradeSetup]) -> list[TradeSetup]:
    """Score and rank all setups, highest composite score first.

    Args:
        setups: List of trade setups that passed all filters and strategy checks.

    Returns:
        Sorted list with composite_score and is_market_leader populated.
    """
    eligible: list[TradeSetup] = []
    for setup in setups:
        setup.composite_score = score_setup(setup)
        setup.is_market_leader = setup.rs_percentile >= 85.0
        eligible.append(setup)

    ranked = sorted(eligible, key=lambda s: s.composite_score, reverse=True)

    for i, s in enumerate(ranked):
        leader_tag = " [LEADER]" if s.is_market_leader else ""
        logger.info(
            "Rank %d: %s score=%.1f RS=%.1f VCP=%.1f%s",
            i + 1, s.symbol, s.composite_score, s.rs_percentile, s.vcp_tightness_score, leader_tag,
        )

    return ranked


def passes_rs_filter(rs_percentile: float, min_percentile: Optional[float] = None) -> bool:
    min_pct = min_percentile or config.MIN_RS_PERCENTILE
    return rs_percentile >= min_pct


def select_top_setups(
    ranked: list[TradeSetup],
    current_positions: int,
    max_positions: int = None,
) -> list[TradeSetup]:
    """Return the top N setups that fit within the remaining position slots.

    Args:
        ranked:             Ranked list of setups (best first).
        current_positions:  Number of open positions.
        max_positions:      Max allowed positions (default from config).

    Returns:
        List of setups to act on today.
    """
    max_pos = max_positions or config.MAX_POSITIONS
    available_slots = max_pos - current_positions
    if available_slots <= 0:
        return []
    return ranked[:available_slots]
