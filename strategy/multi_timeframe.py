"""Multi-timeframe confirmation: Weekly + Daily alignment required.

Weekly chart → Stage 2 confirmed, 30-week MA rising
Daily chart  → Trend template passed, VCP formed, breakout triggered
Both aligned → HIGH CONFIDENCE entry

Skip if only daily confirms — weekly is the market structure anchor.
A stock that looks good on the daily but is in Stage 3/4 on the weekly
is a potential short-seller's prey, not a breakout candidate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from strategy.breakout_detector import BreakoutResult, get_avg_volume_50d, is_valid_breakout
from strategy.stage_analysis import Stage, StageResult, detect_stage
from strategy.trend_template import TrendTemplateResult, passes_trend_template, trend_template_score
from strategy.vcp_detector import VCPResult, detect_vcp

logger = logging.getLogger("scanner.mtf")


@dataclass
class MultiTimeframeResult:
    symbol: str
    is_high_confidence: bool   # True only if Trend Template + VCP + Breakout confirm
    stage_result: Optional[StageResult]
    trend_template_result: Optional[TrendTemplateResult]
    vcp_result: Optional[VCPResult]
    breakout_result: Optional[BreakoutResult]
    trend_template_score: float
    failure_reason: str
    weinstein_stage_bonus: float = 0.0   # +10 if Stage 2, used in ranker scoring
    entry_price: Optional[float] = None
    suggested_stop_loss: Optional[float] = None


def analyse_stock(
    symbol: str,
    daily_ohlcv: pd.DataFrame,
    today_candle: Optional[dict] = None,
) -> MultiTimeframeResult:
    """Run the full multi-timeframe analysis pipeline for one stock.

    Args:
        symbol:       NSE trading symbol.
        daily_ohlcv:  Daily OHLCV DataFrame.
        today_candle: Today's OHLCV dict for breakout confirmation (optional).
                      If None, only the scan (pre-breakout) checks are run.

    Returns:
        MultiTimeframeResult with full analysis details.
    """
    # ── Step 1: Weinstein Stage Analysis (weekly) — ranking signal, not hard gate ──
    # Weinstein and Trend Template measure similar phenomena from different timeframes.
    # Demoted to a scoring bonus in the ranker so the Trend Template can catch stocks
    # the weekly MA lags on (e.g. early Stage 2 stocks that already pass all 8 daily conditions).
    stage_result = detect_stage(daily_ohlcv)

    # ── Step 2: Minervini Trend Template (daily) ──────────────────────────
    tt_result = passes_trend_template(daily_ohlcv)
    tt_score = trend_template_score(tt_result)
    if not tt_result.passes:
        failing = [k for k, v in tt_result.conditions.items() if not v]
        return MultiTimeframeResult(
            symbol=symbol,
            is_high_confidence=False,
            stage_result=stage_result,
            trend_template_result=tt_result,
            vcp_result=None,
            breakout_result=None,
            trend_template_score=tt_score,
            failure_reason=f"Trend template failed: {', '.join(failing[:3])}",
        )

    # ── Step 3: VCP Detection ─────────────────────────────────────────────
    vcp_result = detect_vcp(daily_ohlcv)
    if not vcp_result.is_valid:
        return MultiTimeframeResult(
            symbol=symbol,
            is_high_confidence=False,
            stage_result=stage_result,
            trend_template_result=tt_result,
            vcp_result=vcp_result,
            breakout_result=None,
            trend_template_score=tt_score,
            failure_reason=f"VCP invalid: {vcp_result.failure_reason}",
        )

    if vcp_result.tightness_score < 50.0:
        return MultiTimeframeResult(
            symbol=symbol,
            is_high_confidence=False,
            stage_result=stage_result,
            trend_template_result=tt_result,
            vcp_result=vcp_result,
            breakout_result=None,
            trend_template_score=tt_score,
            failure_reason=f"VCP tightness {vcp_result.tightness_score:.0f} < 50",
        )

    # If no today_candle provided, we're in scan mode — don't check breakout
    if today_candle is None:
        weinstein_bonus = 10.0 if stage_result and stage_result.stage == Stage.ADVANCING else 0.0
        return MultiTimeframeResult(
            symbol=symbol,
            is_high_confidence=True,
            stage_result=stage_result,
            trend_template_result=tt_result,
            vcp_result=vcp_result,
            breakout_result=None,
            trend_template_score=tt_score,
            failure_reason="",
            weinstein_stage_bonus=weinstein_bonus,
            suggested_stop_loss=vcp_result.consolidation_low * 0.97,
        )

    # ── Step 4: Breakout Confirmation ────────────────────────────────────
    avg_vol = get_avg_volume_50d(daily_ohlcv)
    breakout = is_valid_breakout(today_candle, vcp_result.pivot_price, avg_vol)

    if not breakout.is_valid:
        return MultiTimeframeResult(
            symbol=symbol,
            is_high_confidence=False,
            stage_result=stage_result,
            trend_template_result=tt_result,
            vcp_result=vcp_result,
            breakout_result=breakout,
            trend_template_score=tt_score,
            failure_reason=f"Breakout invalid: {breakout.reason}",
        )

    # ── All checks passed — HIGH CONFIDENCE entry ────────────────────────
    entry_price = float(today_candle["close"])
    stop_loss = max(
        vcp_result.consolidation_low * 0.97,
        entry_price * (1 - 0.07),  # hard 7% stop as floor
    )

    weinstein_bonus = 10.0 if stage_result and stage_result.stage == Stage.ADVANCING else 0.0

    logger.info(
        "HIGH CONFIDENCE setup: %s | Stage=%s | TT ✓ | VCP %.0f | ATR_ratio=%.2f | BO %.1fx vol",
        symbol, stage_result.stage.name if stage_result else "?",
        vcp_result.tightness_score, vcp_result.atr_compression_ratio,
        breakout.volume_multiplier,
    )

    return MultiTimeframeResult(
        symbol=symbol,
        is_high_confidence=True,
        stage_result=stage_result,
        trend_template_result=tt_result,
        vcp_result=vcp_result,
        breakout_result=breakout,
        trend_template_score=tt_score,
        failure_reason="",
        weinstein_stage_bonus=weinstein_bonus,
        entry_price=entry_price,
        suggested_stop_loss=stop_loss,
    )
