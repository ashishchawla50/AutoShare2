"""Tests for VCP (Volatility Contraction Pattern) detector."""

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.vcp_detector import VCPResult, detect_vcp, _calculate_tightness, Contraction


# ── Synthetic VCP builder ─────────────────────────────────────────────────────

def make_vcp_ohlcv(
    prior_trend_pct: float = 0.50,
    n_contractions: int = 3,
    consolidation_days: int = 60,
) -> pd.DataFrame:
    """Build a synthetic OHLCV with a prior uptrend followed by a VCP pattern.

    Structure (matches what detect_vcp expects with lookback=60):
      - Phase 1: flat background (60 days)    ← not used by detector
      - Phase 2: strong uptrend (60 days)     ← pre_consolidation_df window
      - Phase 3: VCP consolidation (60 days)  ← consolidation_df window
    """
    FLAT_DAYS = 60
    TREND_DAYS = 60   # must match lookback=60 used in tests
    assert consolidation_days == 60, "consolidation_days must equal lookback"

    start_price = 100.0
    flat_prices = np.full(FLAT_DAYS, start_price)

    # Steep uptrend fills exactly the 60-day pre-consolidation window
    trend_end = start_price * (1.0 + prior_trend_pct)
    trend_prices = np.linspace(start_price, trend_end, TREND_DAYS)

    # VCP consolidation: n_contractions shrinking swings, volume declining
    peak = float(trend_prices[-1])
    swing_size = 0.08   # start ~8% swing
    seg = consolidation_days // n_contractions // 2
    vcp_raw: list[float] = []
    for _ in range(n_contractions):
        down = np.linspace(peak, peak * (1 - swing_size), max(seg, 5))
        recover_high = peak * (1 - swing_size * 0.35)
        up = np.linspace(down[-1], recover_high, max(seg, 5))
        vcp_raw.extend(down.tolist())
        vcp_raw.extend(up.tolist())
        peak = recover_high
        swing_size *= 0.55

    vcp_arr = np.array(vcp_raw[:consolidation_days])
    # Pad if short
    if len(vcp_arr) < consolidation_days:
        vcp_arr = np.concatenate([vcp_arr, np.full(consolidation_days - len(vcp_arr), vcp_arr[-1])])

    all_prices = np.concatenate([flat_prices, trend_prices, vcp_arr])
    dates = pd.date_range(end=date.today(), periods=len(all_prices), freq="B")

    volumes = np.full(len(all_prices), 1_000_000.0)
    volumes[-consolidation_days:] = np.linspace(1_000_000.0, 250_000.0, consolidation_days)

    return pd.DataFrame({
        "open": all_prices * 0.99,
        "high": all_prices * 1.005,
        "low": all_prices * 0.985,
        "close": all_prices,
        "volume": volumes.astype(int),
    }, index=dates)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_vcp_detects_valid_pattern():
    df = make_vcp_ohlcv(prior_trend_pct=0.45, n_contractions=3)
    result = detect_vcp(df, lookback=60)
    assert result.is_valid, f"VCP not detected: {result.failure_reason}"
    assert result.pivot_price > 0
    assert result.consolidation_low > 0
    assert result.tightness_score >= 0


def test_vcp_fails_insufficient_data():
    dates = pd.date_range(end=date.today(), periods=50, freq="B")
    df = pd.DataFrame({
        "open": [100.0] * 50, "high": [101.0] * 50,
        "low": [99.0] * 50, "close": [100.0] * 50, "volume": [1_000_000] * 50,
    }, index=dates)
    result = detect_vcp(df)
    assert not result.is_valid
    assert "Insufficient" in result.failure_reason


def test_vcp_fails_no_prior_uptrend():
    """Sideways market with no prior uptrend should fail."""
    n = 200
    dates = pd.date_range(end=date.today(), periods=n, freq="B")
    prices = [100.0] * n  # flat
    df = pd.DataFrame({
        "open": prices, "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices], "close": prices,
        "volume": [1_000_000] * n,
    }, index=dates)
    result = detect_vcp(df)
    assert not result.is_valid


def test_vcp_contraction_count_range():
    df = make_vcp_ohlcv(n_contractions=3)
    result = detect_vcp(df, lookback=60)
    if result.is_valid:
        assert 2 <= result.contraction_count <= 4


def test_vcp_tightness_score_bounds():
    contraction = Contraction(
        high=150.0, low=142.0, high_index=0, low_index=5,
        range_pct=0.053, peak_volume=1_000_000, avg_volume_in_contraction=800_000
    )
    score = _calculate_tightness([contraction])
    assert 0.0 <= score <= 100.0


def test_vcp_tightness_higher_for_tighter_base():
    """A base with 3% final range should score higher than one with 12%."""
    tight_contraction = Contraction(
        high=150.0, low=145.5, high_index=0, low_index=5,
        range_pct=0.03, peak_volume=500_000, avg_volume_in_contraction=400_000
    )
    loose_contraction = Contraction(
        high=150.0, low=132.0, high_index=0, low_index=5,
        range_pct=0.12, peak_volume=1_500_000, avg_volume_in_contraction=1_200_000
    )
    tight_score = _calculate_tightness([tight_contraction])
    loose_score = _calculate_tightness([loose_contraction])
    assert tight_score > loose_score


def test_vcp_result_dataclass_defaults():
    result = VCPResult(
        is_valid=False, pivot_price=0, consolidation_low=0,
        contraction_count=0, volume_contraction_confirmed=False,
        tightness_score=0, atr_compression_ratio=1.0, atr_compressed=False,
        prior_uptrend_pct=0, consolidation_days=0,
    )
    assert result.contractions == []
    assert result.failure_reason == ""


# ── ATR gate logic tests ──────────────────────────────────────────────────────

def test_zero_violation_vcp_passes_without_atr_compression():
    """A clean VCP (zero range violations) must pass even when ATR is not compressed.

    ATR < 0.7 is NOT a universal gate — it only applies when there is a range
    violation (one-violation-allowed rule). Accidentally making it universal
    would tighten and loosen simultaneously, confounding before/after comparisons.
    """
    df = make_vcp_ohlcv(prior_trend_pct=0.45, n_contractions=3)
    # Patch ATR to return non-compressed (ratio = 0.85 > 0.7)
    with patch("strategy.vcp_detector._calculate_atr_compression", return_value=(0.85, False)):
        result = detect_vcp(df, lookback=60)
    assert result.is_valid, (
        f"Zero-violation VCP rejected despite no range violations: {result.failure_reason}"
    )
    assert result.atr_compression_ratio == 0.85
    assert not result.atr_compressed


def test_vcp_one_violation_rejected_without_atr_compression():
    """A VCP with one range violation must be rejected when ATR is not compressed."""
    df = make_vcp_ohlcv(prior_trend_pct=0.45, n_contractions=3)
    # Patch ATR to return non-compressed AND patch contractions to have one violation
    from strategy.vcp_detector import Contraction
    one_violation_contractions = [
        Contraction(high=150, low=137, high_index=0, low_index=5,
                    range_pct=0.087, peak_volume=900_000, avg_volume_in_contraction=700_000),
        Contraction(high=148, low=138, high_index=6, low_index=11,
                    range_pct=0.068, peak_volume=700_000, avg_volume_in_contraction=550_000),
        # Third contraction larger than second — one violation
        Contraction(high=146, low=134, high_index=12, low_index=17,
                    range_pct=0.082, peak_volume=500_000, avg_volume_in_contraction=400_000),
    ]
    with (
        patch("strategy.vcp_detector._calculate_atr_compression", return_value=(0.85, False)),
        patch("strategy.vcp_detector._identify_contractions", return_value=one_violation_contractions),
    ):
        result = detect_vcp(df, lookback=60)
    assert not result.is_valid
    assert "ATR compression" in result.failure_reason or "violation" in result.failure_reason.lower()


def test_vcp_one_violation_passes_with_atr_compression():
    """A VCP with one range violation passes when ATR confirms compression (ratio < 0.7)."""
    df = make_vcp_ohlcv(prior_trend_pct=0.45, n_contractions=3)
    from strategy.vcp_detector import Contraction
    one_violation_contractions = [
        Contraction(high=150, low=137, high_index=0, low_index=5,
                    range_pct=0.087, peak_volume=900_000, avg_volume_in_contraction=700_000),
        Contraction(high=148, low=138, high_index=6, low_index=11,
                    range_pct=0.068, peak_volume=700_000, avg_volume_in_contraction=550_000),
        Contraction(high=146, low=134, high_index=12, low_index=17,
                    range_pct=0.082, peak_volume=500_000, avg_volume_in_contraction=400_000),
    ]
    with (
        patch("strategy.vcp_detector._calculate_atr_compression", return_value=(0.62, True)),
        patch("strategy.vcp_detector._identify_contractions", return_value=one_violation_contractions),
    ):
        result = detect_vcp(df, lookback=60)
    assert result.is_valid, (
        f"One-violation VCP with ATR compressed should pass: {result.failure_reason}"
    )
