"""Tests for regime detector logic and VIX classification."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from regime.regime_detector import Regime, RegimeDetector, _regime_rank
from regime.vix_monitor import classify_vix_regime, position_size_multiplier, should_halt_new_trades


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_nifty_bull(n: int = 300) -> pd.DataFrame:
    """Nifty in strong uptrend — should trigger Bull regime."""
    dates = pd.date_range(end="2024-12-31", periods=n, freq="B")
    prices = np.linspace(15_000, 22_000, n)
    return pd.DataFrame({
        "open": prices * 0.998,
        "high": prices * 1.005,
        "low": prices * 0.995,
        "close": prices,
        "volume": [1_000_000_000] * n,
    }, index=dates)


def make_nifty_bear(n: int = 300) -> pd.DataFrame:
    """Nifty in downtrend — should trigger Bear regime."""
    dates = pd.date_range(end="2024-12-31", periods=n, freq="B")
    prices = np.linspace(22_000, 15_000, n)
    return pd.DataFrame({
        "open": prices * 0.998,
        "high": prices * 1.005,
        "low": prices * 0.995,
        "close": prices,
        "volume": [1_000_000_000] * n,
    }, index=dates)


def make_universe_bull(n: int = 300, n_stocks: int = 50) -> dict:
    """50 stocks all in uptrend."""
    universe = {}
    for i in range(n_stocks):
        dates = pd.date_range(end="2024-12-31", periods=n, freq="B")
        prices = np.linspace(100 + i, 200 + i, n)
        universe[f"STOCK{i:02d}"] = pd.DataFrame({
            "open": prices * 0.99, "high": prices * 1.01,
            "low": prices * 0.98, "close": prices,
            "volume": [1_000_000] * n,
        }, index=dates)
    return universe


def make_universe_bear(n: int = 300, n_stocks: int = 50) -> dict:
    """50 stocks all in downtrend."""
    universe = {}
    for i in range(n_stocks):
        dates = pd.date_range(end="2024-12-31", periods=n, freq="B")
        prices = np.linspace(200 + i, 100 + i, n)  # declining
        universe[f"STOCK{i:02d}"] = pd.DataFrame({
            "open": prices * 0.99, "high": prices * 1.01,
            "low": prices * 0.98, "close": prices,
            "volume": [1_000_000] * n,
        }, index=dates)
    return universe


# ── Regime Detector Tests ─────────────────────────────────────────────────────

def _fresh_detector(universe, nifty):
    """Create a RegimeDetector with no prior DB state to prevent test cross-contamination."""
    import unittest.mock as mock
    with mock.patch.object(RegimeDetector, "_load_last_regime", return_value=None):
        return RegimeDetector(universe, nifty)


def test_bull_regime_detected():
    nifty = make_nifty_bull()
    universe = make_universe_bull()
    import unittest.mock as mock
    with mock.patch("regime.regime_detector.get_india_vix", return_value=15.0):
        detector = _fresh_detector(universe, nifty)
        state = detector.detect()
    assert state.regime == Regime.BULL, f"Expected BULL, got {state.regime}"
    assert state.allow_new_trades
    assert state.position_size_multiplier == 1.0


def test_bear_regime_detected():
    nifty = make_nifty_bear()
    universe = make_universe_bear()
    import unittest.mock as mock
    with mock.patch("regime.regime_detector.get_india_vix", return_value=15.0):
        detector = _fresh_detector(universe, nifty)
        state = detector.detect()
    assert state.regime in (Regime.BEAR, Regime.CAUTIOUS)
    if state.regime == Regime.BEAR:
        assert not state.allow_new_trades


def test_extreme_vix_overrides_bull():
    nifty = make_nifty_bull()
    universe = make_universe_bull()
    import unittest.mock as mock
    with mock.patch("regime.regime_detector.get_india_vix", return_value=40.0):
        detector = _fresh_detector(universe, nifty)
        state = detector.detect()
    assert state.regime == Regime.EXTREME_VOLATILITY
    assert not state.allow_new_trades
    assert state.position_size_multiplier == 0.0


# ── VIX Monitor Tests ─────────────────────────────────────────────────────────

def test_vix_calm():
    assert classify_vix_regime(15.0) == "CALM"


def test_vix_caution():
    assert classify_vix_regime(22.0) == "CAUTION"


def test_vix_high_volatility():
    assert classify_vix_regime(30.0) == "HIGH_VOLATILITY"


def test_vix_extreme():
    assert classify_vix_regime(40.0) == "EXTREME_VOLATILITY"


def test_vix_none_returns_unknown():
    assert classify_vix_regime(None) == "UNKNOWN"


def test_should_halt_at_vix_35():
    assert should_halt_new_trades(36.0)
    assert not should_halt_new_trades(34.0)
    assert not should_halt_new_trades(None)


def test_position_multiplier_values():
    assert position_size_multiplier(15.0) == 1.0
    assert position_size_multiplier(22.0) == 0.5
    assert position_size_multiplier(30.0) == 0.5
    assert position_size_multiplier(40.0) == 0.0


# ── Regime Rank Tests ─────────────────────────────────────────────────────────

def test_regime_rank_ordering():
    assert _regime_rank(Regime.BULL) > _regime_rank(Regime.CAUTIOUS)
    assert _regime_rank(Regime.CAUTIOUS) > _regime_rank(Regime.BEAR)
    assert _regime_rank(Regime.BEAR) > _regime_rank(Regime.EXTREME_VOLATILITY)
