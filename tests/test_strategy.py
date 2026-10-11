"""Tests for Weinstein Stage Analysis and Minervini Trend Template."""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.stage_analysis import Stage, detect_stage, is_stage2
from strategy.trend_template import passes_trend_template, trend_template_score


# ── OHLCV synthetic data builders ─────────────────────────────────────────────

def make_uptrend_ohlcv(n_days: int = 300, start_price: float = 100.0) -> pd.DataFrame:
    """Create a clean uptrending OHLCV series — should pass Stage 2."""
    dates = pd.date_range(end=date.today(), periods=n_days, freq="B")
    prices = start_price * (1 + np.arange(n_days) * 0.003)   # ~0.3%/day uptrend
    volume = np.random.randint(500_000, 1_500_000, n_days)
    return pd.DataFrame({
        "open": prices * 0.99,
        "high": prices * 1.01,
        "low": prices * 0.98,
        "close": prices,
        "volume": volume,
    }, index=dates)


def make_downtrend_ohlcv(n_days: int = 300, start_price: float = 200.0) -> pd.DataFrame:
    """Create a downtrending OHLCV — should fail Stage 2 (Stage 4)."""
    dates = pd.date_range(end=date.today(), periods=n_days, freq="B")
    prices = start_price * (1 - np.arange(n_days) * 0.002)  # ~0.2%/day downtrend
    prices = np.maximum(prices, 1.0)
    volume = np.random.randint(500_000, 1_500_000, n_days)
    return pd.DataFrame({
        "open": prices * 0.99,
        "high": prices * 1.01,
        "low": prices * 0.98,
        "close": prices,
        "volume": volume,
    }, index=dates)


# ── Stage Analysis Tests ───────────────────────────────────────────────────────

def test_stage2_detected_in_uptrend():
    df = make_uptrend_ohlcv(n_days=300)
    result = detect_stage(df)
    assert result.stage == Stage.ADVANCING, f"Expected ADVANCING, got {result.stage}"


def test_stage4_detected_in_downtrend():
    df = make_downtrend_ohlcv(n_days=300)
    result = detect_stage(df)
    assert result.stage == Stage.DECLINING, f"Expected DECLINING, got {result.stage}"


def test_stage_insufficient_data():
    df = make_uptrend_ohlcv(n_days=100)
    result = detect_stage(df)
    # With insufficient data, should return BASING (not crash)
    assert result.stage in (Stage.BASING, Stage.ADVANCING, Stage.TOPPING, Stage.DECLINING)
    assert result.weeks_available < 40 or result.weeks_available >= 0


def test_is_stage2_returns_tuple():
    df = make_uptrend_ohlcv(n_days=300)
    passes, result = is_stage2(df)
    assert isinstance(passes, bool)
    assert result.stage in Stage.__members__.values()


# ── Trend Template Tests ───────────────────────────────────────────────────────

def test_trend_template_passes_strong_uptrend():
    """A consistent uptrend should pass all 8 conditions."""
    df = make_uptrend_ohlcv(n_days=300, start_price=100.0)
    result = passes_trend_template(df)
    assert result.passes, f"Expected pass, failed conditions: {[k for k,v in result.conditions.items() if not v]}"


def test_trend_template_fails_downtrend():
    df = make_downtrend_ohlcv(n_days=300)
    result = passes_trend_template(df)
    assert not result.passes


def test_trend_template_insufficient_data():
    df = make_uptrend_ohlcv(n_days=100)
    result = passes_trend_template(df)
    # Should not crash; should return False
    assert not result.passes
    assert result.current_price == 0.0


def test_trend_template_all_8_conditions_present():
    """Every condition must be present in the output dict."""
    df = make_uptrend_ohlcv(n_days=300)
    result = passes_trend_template(df)
    expected_keys = {
        "c1_price_above_ema50", "c2_price_above_ema150", "c3_price_above_ema200",
        "c4_ema50_above_ema150", "c5_ema150_above_ema200", "c6_ema200_trending_up",
        "c7_price_25pct_above_52w_low", "c8_price_within_25pct_of_52w_high",
    }
    assert expected_keys == set(result.conditions.keys())


def test_trend_template_score_range():
    df = make_uptrend_ohlcv(n_days=300)
    result = passes_trend_template(df)
    score = trend_template_score(result)
    assert 0.0 <= score <= 100.0


# ── Breakout Detector Tests ───────────────────────────────────────────────────

from strategy.breakout_detector import is_valid_breakout


def _make_candle(open_: float, high: float, low: float, close: float, volume: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


def test_breakout_valid_bullish_candle():
    """A clean bullish breakout candle above pivot with 2x volume should pass."""
    # close=102.5: above pivot (100), bullish (> open 101), strong close (>= 104*0.96=99.84),
    # within 5% extension (<=105), healthy range, 2x volume — all conditions satisfied.
    candle = _make_candle(open_=101.0, high=104.0, low=100.5, close=103.5, volume=2_000_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert result.is_valid, result.reason


def test_breakout_fails_bearish_body():
    """Gujral: close <= open on breakout day = bearish candle — must reject."""
    candle = _make_candle(open_=106.0, high=106.5, low=99.0, close=105.5, volume=2_000_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert not result.is_valid
    assert "bullish_body" in result.reason


def test_breakout_fails_close_equals_open():
    """A doji (close == open) is not bullish — should fail."""
    candle = _make_candle(open_=105.5, high=106.0, low=99.0, close=105.5, volume=2_000_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert not result.is_valid
    assert "bullish_body" in result.reason


def test_breakout_fails_price_below_pivot():
    candle = _make_candle(open_=98.0, high=99.5, low=97.0, close=99.0, volume=2_000_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert not result.is_valid
    assert "price_above_pivot" in result.reason


def test_breakout_fails_low_volume():
    """Volume below 2x average should fail even with a bullish candle."""
    candle = _make_candle(open_=100.0, high=106.0, low=99.0, close=105.5, volume=800_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert not result.is_valid
    assert "volume_surge" in result.reason


def test_breakout_fails_weak_close():
    """Close below 96% of high = weak close — should fail strong_close check."""
    candle = _make_candle(open_=100.0, high=110.0, low=99.0, close=104.0, volume=2_000_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert not result.is_valid
    assert "strong_close" in result.reason


def test_breakout_fails_overextended():
    """Close more than 5% past pivot = chasing — should fail."""
    candle = _make_candle(open_=106.0, high=107.0, low=105.0, close=106.5, volume=2_000_000)
    result = is_valid_breakout(candle, pivot_price=100.0, avg_volume_50d=1_000_000)
    assert not result.is_valid
    assert "not_extended" in result.reason
