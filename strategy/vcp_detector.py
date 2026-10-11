"""Volatility Contraction Pattern (VCP) detection — full mathematical algorithm.

A valid VCP requires:
  1. Prior uptrend of ≥30% from a prior low (base prerequisite)
  2. Consolidation phase: 15–60 trading days of sideways/slight downward movement
  3. Within consolidation: 2–4 clearly identifiable "contractions" (swings)
  4. Each contraction must be smaller in % range than the previous one
  5. Each contraction must have lower peak volume than the previous one
  6. Final contraction must be the tightest (< 10% range ideal)
  7. Price must not undercut the prior consolidation low by more than 3%

Reference: Mark Minervini's "Trade Like a Stock Market Wizard"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from config.config import config
from utils.indicator_utils import swing_highs, swing_lows

logger = logging.getLogger("scanner.vcp")


@dataclass
class Contraction:
    """One swing cycle within the VCP consolidation."""
    high: float
    low: float
    high_index: int
    low_index: int
    range_pct: float           # (high - low) / high
    peak_volume: float
    avg_volume_in_contraction: float


@dataclass
class VCPResult:
    is_valid: bool
    pivot_price: float                  # Breakout level (consolidation high)
    consolidation_low: float            # Stop loss reference
    contraction_count: int
    volume_contraction_confirmed: bool  # Each swing has lower peak volume
    tightness_score: float              # 0–100, higher = tighter/better
    atr_compression_ratio: float        # 10-day ATR / 50-day ATR; < 0.7 = compressed
    atr_compressed: bool                # True if ratio < 0.7 (objective volatility contraction)
    prior_uptrend_pct: float            # % gain before consolidation
    consolidation_days: int
    contractions: list[Contraction] = field(default_factory=list)
    failure_reason: str = ""


def detect_vcp(
    daily_ohlcv: pd.DataFrame,
    lookback: int = None,
) -> VCPResult:
    """Detect a Volatility Contraction Pattern in the given OHLCV data.

    Args:
        daily_ohlcv: DataFrame with DatetimeIndex and [open, high, low, close, volume].
                     Must contain at least 60 trading days before the potential VCP.
        lookback:    Max days to look back for the consolidation. Default: 60.

    Returns:
        VCPResult with is_valid flag and all pattern metrics.
    """
    lookback = lookback or config.VCP_LOOKBACK_DAYS

    if len(daily_ohlcv) < lookback + 60:
        return VCPResult(
            is_valid=False,
            pivot_price=0,
            consolidation_low=0,
            contraction_count=0,
            volume_contraction_confirmed=False,
            tightness_score=0,
            atr_compression_ratio=1.0,
            atr_compressed=False,
            prior_uptrend_pct=0,
            consolidation_days=0,
            failure_reason="Insufficient data",
        )

    # ── Step 1: Find the consolidation window ────────────────────────────────
    consolidation_df = daily_ohlcv.tail(lookback)
    # Use all available data before the consolidation — Minervini's VCP prior
    # uptrend can be months or years old, not just the 60 days immediately
    # preceding the base.
    pre_consolidation_df = daily_ohlcv.iloc[:-lookback]

    # ── Step 2: Verify prior uptrend ≥ 30% ──────────────────────────────────
    prior_low = float(pre_consolidation_df["low"].min())
    consolidation_start_price = float(consolidation_df["close"].iloc[0])

    if prior_low <= 0:
        return VCPResult(
            is_valid=False, pivot_price=0, consolidation_low=0,
            contraction_count=0, volume_contraction_confirmed=False,
            tightness_score=0, prior_uptrend_pct=0, consolidation_days=0,
            failure_reason="Invalid prior low",
        )

    prior_uptrend_pct = (consolidation_start_price - prior_low) / prior_low
    if prior_uptrend_pct < 0.30:
        return VCPResult(
            is_valid=False, pivot_price=0, consolidation_low=0,
            contraction_count=0, volume_contraction_confirmed=False,
            tightness_score=0, atr_compression_ratio=1.0, atr_compressed=False,
            prior_uptrend_pct=prior_uptrend_pct,
            consolidation_days=len(consolidation_df),
            failure_reason=f"Prior uptrend only {prior_uptrend_pct:.1%} (need ≥30%)",
        )

    # ── ATR compression (objective volatility measure) ───────────────────────
    atr_compression_ratio, atr_compressed = _calculate_atr_compression(daily_ohlcv)

    # ── Step 3: Identify swing highs and lows within consolidation ───────────
    contractions = _identify_contractions(consolidation_df)

    if len(contractions) < 2:
        return VCPResult(
            is_valid=False, pivot_price=0, consolidation_low=0,
            contraction_count=len(contractions),
            volume_contraction_confirmed=False,
            tightness_score=0, atr_compression_ratio=atr_compression_ratio, atr_compressed=atr_compressed,
            prior_uptrend_pct=prior_uptrend_pct,
            consolidation_days=len(consolidation_df),
            failure_reason=f"Only {len(contractions)} contractions (need 2–4)",
        )

    if len(contractions) > 4:
        # Too many swings = messy base; trim to last 4
        contractions = contractions[-4:]

    # ── Step 4: Each contraction must be smaller than the previous ───────────
    # One violation is allowed; if violated, ATR compression (ratio < 0.7) must
    # confirm the base is genuinely coiling — prevents passing disorderly bases.
    range_violations = sum(
        1 for i in range(1, len(contractions))
        if contractions[i].range_pct >= contractions[i - 1].range_pct
    )
    if range_violations > 1:
        return VCPResult(
            is_valid=False, pivot_price=0, consolidation_low=0,
            contraction_count=len(contractions),
            volume_contraction_confirmed=False,
            tightness_score=0, atr_compression_ratio=atr_compression_ratio, atr_compressed=atr_compressed,
            prior_uptrend_pct=prior_uptrend_pct,
            consolidation_days=len(consolidation_df),
            contractions=contractions,
            failure_reason=f"{range_violations} range violations in contractions (max 1 allowed)",
        )
    if range_violations == 1 and not atr_compressed:
        return VCPResult(
            is_valid=False, pivot_price=0, consolidation_low=0,
            contraction_count=len(contractions),
            volume_contraction_confirmed=False,
            tightness_score=0, atr_compression_ratio=atr_compression_ratio, atr_compressed=atr_compressed,
            prior_uptrend_pct=prior_uptrend_pct,
            consolidation_days=len(consolidation_df),
            contractions=contractions,
            failure_reason=f"Range violation requires ATR compression (ratio={atr_compression_ratio:.2f} >= 0.7)",
        )

    # ── Step 5: Volume contraction — each swing has lower peak volume ─────────
    volume_contracting = all(
        contractions[i].peak_volume < contractions[i - 1].peak_volume
        for i in range(1, len(contractions))
    )

    # ── Step 6: Final contraction tightness ──────────────────────────────────
    final_range = contractions[-1].range_pct
    tightness_score = _calculate_tightness(contractions)

    # ── Step 7: Pivot price = high of the consolidation ──────────────────────
    pivot_price = float(consolidation_df["high"].max())

    # ── Step 8: Consolidation low must not be undercut > 3% ─────────────────
    consolidation_low = float(consolidation_df["low"].min())
    base_low = float(pre_consolidation_df["low"].min())
    if consolidation_low < base_low * 0.97:
        return VCPResult(
            is_valid=False, pivot_price=pivot_price, consolidation_low=consolidation_low,
            contraction_count=len(contractions),
            volume_contraction_confirmed=volume_contracting,
            tightness_score=tightness_score,
            atr_compression_ratio=atr_compression_ratio, atr_compressed=atr_compressed,
            prior_uptrend_pct=prior_uptrend_pct,
            consolidation_days=len(consolidation_df),
            contractions=contractions,
            failure_reason="Consolidation undercut prior base low by >3%",
        )

    return VCPResult(
        is_valid=True,
        pivot_price=pivot_price,
        consolidation_low=consolidation_low,
        contraction_count=len(contractions),
        volume_contraction_confirmed=volume_contracting,
        tightness_score=tightness_score,
        atr_compression_ratio=atr_compression_ratio,
        atr_compressed=atr_compressed,
        prior_uptrend_pct=prior_uptrend_pct,
        consolidation_days=len(consolidation_df),
        contractions=contractions,
        failure_reason="",
    )


def _identify_contractions(df: pd.DataFrame) -> list[Contraction]:
    """Find swing high→low cycles within the consolidation window.

    A contraction is defined as a swing from a local high to a subsequent local low.
    """
    highs_mask = swing_highs(df["high"], left=2, right=2)
    lows_mask = swing_lows(df["low"], left=2, right=2)

    high_indices = [i for i, v in enumerate(highs_mask) if v]
    low_indices = [i for i, v in enumerate(lows_mask) if v]

    contractions: list[Contraction] = []
    avg_vol = float(df["volume"].mean()) if df["volume"].mean() > 0 else 1.0

    for h_idx in high_indices:
        # Find the first swing low that comes AFTER this swing high
        subsequent_lows = [l for l in low_indices if l > h_idx]
        if not subsequent_lows:
            continue
        l_idx = subsequent_lows[0]

        swing_high = float(df["high"].iloc[h_idx])
        swing_low = float(df["low"].iloc[l_idx])

        if swing_high <= 0:
            continue

        range_pct = (swing_high - swing_low) / swing_high
        segment_volume = df["volume"].iloc[h_idx : l_idx + 1]
        peak_volume = float(segment_volume.max())
        avg_vol_in_swing = float(segment_volume.mean())

        if range_pct < 0.01:  # ignore micro swings
            continue

        contractions.append(
            Contraction(
                high=swing_high,
                low=swing_low,
                high_index=h_idx,
                low_index=l_idx,
                range_pct=range_pct,
                peak_volume=peak_volume,
                avg_volume_in_contraction=avg_vol_in_swing,
            )
        )

    # Remove overlapping contractions (keep the cleanest set)
    return _deduplicate_contractions(contractions)


def _deduplicate_contractions(contractions: list[Contraction]) -> list[Contraction]:
    """Remove overlapping contractions, keeping the largest non-overlapping set."""
    if len(contractions) <= 1:
        return contractions

    result: list[Contraction] = [contractions[0]]
    for c in contractions[1:]:
        last = result[-1]
        if c.high_index > last.low_index:
            result.append(c)
    return result


def _calculate_atr_compression(daily_ohlcv: pd.DataFrame) -> tuple[float, bool]:
    """Compute 10-day ATR / 50-day ATR ratio as an objective volatility contraction measure.

    A ratio < 0.7 means current volatility is less than 70% of its 50-day average —
    the stock is genuinely coiling, not just showing a subjective VCP pattern.
    This is parameter-stable and doesn't require swing-high/low detection.

    Returns:
        (ratio, is_compressed) where is_compressed = ratio < 0.7
    """
    if len(daily_ohlcv) < 55:
        return 1.0, False

    high = daily_ohlcv["high"]
    low = daily_ohlcv["low"]
    close = daily_ohlcv["close"]

    # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr10 = float(tr.tail(10).mean())
    atr50 = float(tr.tail(50).mean())

    if atr50 <= 0:
        return 1.0, False

    ratio = atr10 / atr50
    return round(ratio, 3), ratio < 0.7


def _calculate_tightness(contractions: list[Contraction]) -> float:
    """Score VCP tightness (0–100). Higher = tighter = better quality base.

    Scoring:
      - Final contraction < 10% range = 60 base points
      - Each additional contraction adds up to 10 points
      - Volume contraction confirmed = 15 bonus points
      - Final contraction < 5% = 15 bonus points
    """
    if not contractions:
        return 0.0

    final_range = contractions[-1].range_pct
    score = 0.0

    # Base: final contraction tightness
    if final_range < 0.05:
        score += 60.0
    elif final_range < 0.10:
        score += 45.0
    elif final_range < 0.15:
        score += 25.0

    # Bonus for number of clean contractions (2=+5, 3=+10, 4=+15)
    score += min(15.0, (len(contractions) - 1) * 5.0)

    # Bonus: range reduction ratio — higher = more orderly contraction
    if len(contractions) >= 2:
        reduction_ratio = contractions[-1].range_pct / contractions[0].range_pct
        if reduction_ratio < 0.3:
            score += 10.0  # final range is <30% of initial range
        elif reduction_ratio < 0.5:
            score += 5.0

    # Volume contraction bonus
    volume_contracting = all(
        contractions[i].peak_volume < contractions[i - 1].peak_volume
        for i in range(1, len(contractions))
    )
    if volume_contracting:
        score += 15.0

    return min(100.0, score)
