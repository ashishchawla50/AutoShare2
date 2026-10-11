"""Tests for all 9 stock universe filters."""

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.stock_filter import (
    filter_circuit_breaker,
    filter_corporate_events,
    filter_data_quality,
    filter_fundamentals,
    filter_ipo_age,
    filter_liquidity,
    filter_market_cap,
    filter_price,
    filter_surveillance,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_ohlcv(n_days: int = 90, price: float = 500.0, volume: int = 5_000_000) -> pd.DataFrame:
    """Create a clean synthetic OHLCV DataFrame."""
    import numpy as np
    dates = pd.date_range(end=date.today(), periods=n_days, freq="B")
    close = np.full(n_days, price, dtype=float)
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close.copy(),
        "volume": np.full(n_days, float(volume)),
    }, index=dates)


# ── Filter 1: Price ───────────────────────────────────────────────────────────

def test_price_filter_passes():
    ok, reason = filter_price("TEST", 200.0)
    assert ok
    assert reason == ""


def test_price_filter_fails_below_100():
    ok, reason = filter_price("TEST", 50.0)
    assert not ok
    assert "100" in reason


def test_price_filter_passes_exactly_100():
    ok, _ = filter_price("TEST", 100.0)
    assert ok


# ── Filter 2: Market Cap ──────────────────────────────────────────────────────

def test_market_cap_passes():
    ok, _ = filter_market_cap("TEST", 10_000.0)
    assert ok


def test_market_cap_fails_below_5000():
    ok, reason = filter_market_cap("TEST", 3_000.0)
    assert not ok
    assert "5000" in reason or "5,000" in reason


def test_market_cap_passes_none():
    # None means Screener.in was unreachable — filter passes by default.
    # Nifty 200 membership implies large-cap so false-positive risk is low.
    ok, reason = filter_market_cap("TEST", None)
    assert ok
    assert reason == ""


# ── Filter 3: Liquidity ───────────────────────────────────────────────────────

def test_liquidity_passes():
    """Avg 5M shares × ₹500 = ₹250Cr daily value — well above ₹10Cr."""
    df = make_ohlcv(n_days=35, price=500.0, volume=5_000_000)
    ok, reason = filter_liquidity("TEST", df)
    assert ok, reason


def test_liquidity_fails_low_value():
    """Very low volume: ₹100 × 5000 shares = ₹5L daily value."""
    df = make_ohlcv(n_days=35, price=100.0, volume=5_000)
    ok, _ = filter_liquidity("TEST", df)
    assert not ok


def test_liquidity_passes_high_price_low_share_count():
    """₹3,000 stock at 3L shares/day moves ₹90Cr — liquid by value even
    though it would have failed the old 5-lakh-share floor (removed 2026-07-29)."""
    df = make_ohlcv(n_days=35, price=3000.0, volume=300_000)
    ok, reason = filter_liquidity("TEST", df)
    assert ok, reason


def test_liquidity_fails_insufficient_data():
    df = make_ohlcv(n_days=10, price=500.0, volume=5_000_000)
    ok, reason = filter_liquidity("TEST", df, lookback_days=30)
    assert not ok
    assert "Insufficient" in reason


# ── Filter 4: Surveillance ────────────────────────────────────────────────────

def test_surveillance_passes():
    ok, _ = filter_surveillance("RELIANCE", set(), "EQ")
    assert ok


def test_surveillance_fails_if_on_list():
    ok, reason = filter_surveillance("SPIKESTOCK", {"SPIKESTOCK"}, "EQ")
    assert not ok
    assert "surveillance" in reason.lower()


def test_surveillance_fails_z_category():
    ok, reason = filter_surveillance("BADSTOCK", set(), "Z")
    assert not ok
    assert "Z" in reason


# ── Filter 5: Corporate Events — tested via mock ──────────────────────────────
# (Actual DB interaction skipped in unit tests — see integration tests)

# ── Filter 6: Circuit Breaker ─────────────────────────────────────────────────

def test_circuit_breaker_passes_normal():
    df = make_ohlcv(n_days=65)
    ok, _ = filter_circuit_breaker("TEST", df)
    assert ok


def test_circuit_breaker_fails_many_circuits():
    df = make_ohlcv(n_days=65, price=500.0)
    # Insert 4 circuit hit days (>19% move) using .iloc to avoid chained-assignment
    close_col = df.columns.get_loc("close")
    for i in [10, 20, 30, 40]:
        df.iloc[i, close_col] = df.iloc[i - 1, close_col] * 1.21
    ok, reason = filter_circuit_breaker("TEST", df)
    assert not ok
    assert "circuit" in reason.lower()


# ── Filter 7: Fundamentals ────────────────────────────────────────────────────

def test_fundamentals_passes():
    ok, _ = filter_fundamentals("TEST", 10.0, 0.5, roe_pct=20.0)
    assert ok


def test_fundamentals_fails_high_pledge():
    ok, reason = filter_fundamentals("TEST", 60.0, 0.5)
    assert not ok
    assert "pledge" in reason.lower()


def test_fundamentals_fails_high_de():
    ok, reason = filter_fundamentals("TEST", 10.0, 3.0)
    assert not ok
    assert "D/E" in reason or "debt" in reason.lower()


def test_fundamentals_passes_with_none():
    """None values should not trigger a failure — data may be unavailable."""
    ok, _ = filter_fundamentals("TEST", None, None)
    assert ok


def test_fundamentals_ignores_low_roe_by_default():
    """ROE gate is disabled by default (2026-07-29) — it was never part of
    the validated vcp_v2 ruleset, so low ROE must not reject a stock."""
    ok, _ = filter_fundamentals("TEST", 10.0, 0.5, roe_pct=8.0)
    assert ok


def test_fundamentals_fails_low_roe_when_enforced(monkeypatch):
    """With ENFORCE_MIN_ROE on, ROE below 15% fails the quality gate."""
    from config.config import config
    monkeypatch.setattr(config, "ENFORCE_MIN_ROE", True)
    ok, reason = filter_fundamentals("TEST", 10.0, 0.5, roe_pct=8.0)
    assert not ok
    assert "ROE" in reason


def test_fundamentals_passes_exactly_min_roe(monkeypatch):
    """ROE exactly at 15% should pass even when the gate is enforced."""
    from config.config import config
    monkeypatch.setattr(config, "ENFORCE_MIN_ROE", True)
    ok, _ = filter_fundamentals("TEST", 10.0, 0.5, roe_pct=15.0)
    assert ok


def test_fundamentals_passes_none_roe():
    """None ROE (Screener.in blocked) should not fail — pass by default."""
    ok, _ = filter_fundamentals("TEST", 10.0, 0.5, roe_pct=None)
    assert ok


def test_fundamentals_fails_pledge_even_with_good_roe():
    """High pledge should still fail even when ROE is good."""
    ok, reason = filter_fundamentals("TEST", 60.0, 0.5, roe_pct=25.0)
    assert not ok
    assert "pledge" in reason.lower()


# ── Filter 8: IPO Age ─────────────────────────────────────────────────────────

def test_ipo_age_passes():
    listing = date.today() - timedelta(days=400)
    ok, _ = filter_ipo_age("TEST", listing)
    assert ok


def test_ipo_age_fails_recent():
    listing = date.today() - timedelta(days=100)
    ok, reason = filter_ipo_age("TEST", listing)
    assert not ok
    assert "days ago" in reason


def test_ipo_age_passes_none():
    """Unknown listing date should not block trading."""
    ok, _ = filter_ipo_age("TEST", None)
    assert ok


# ── Filter 9: Data Quality ────────────────────────────────────────────────────

def test_data_quality_passes():
    df = make_ohlcv(n_days=95)
    ok, _ = filter_data_quality("TEST", df)
    assert ok


def test_data_quality_fails_high_low_violation():
    df = make_ohlcv(n_days=95)
    df.loc[df.index[5], "high"] = df.loc[df.index[5], "low"] - 1  # high < low
    ok, reason = filter_data_quality("TEST", df)
    assert not ok
    assert "high < low" in reason


# ── validate_ohlcv volume spike logic ─────────────────────────────────────────

from utils.indicator_utils import validate_ohlcv


def _make_volume_df(n: int = 60, normal_vol: int = 1_000_000) -> pd.DataFrame:
    dates = pd.date_range(end="2026-07-06", periods=n, freq="B")
    close = np.full(n, 500.0)
    vols = np.full(n, float(normal_vol))
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close.copy(),
        "volume": vols,
    }, index=dates)


def test_volume_spike_no_alert_on_normal_data():
    df = _make_volume_df()
    errors = validate_ohlcv(df)
    assert not any("median" in e for e in errors)


def test_volume_spike_no_alert_on_historical_event():
    """A high-volume event in the past (e.g. F&O expiry) must NOT trigger an alert."""
    df = _make_volume_df()
    df.iloc[20, df.columns.get_loc("volume")] = 50_000_000  # huge spike 30 rows ago
    errors = validate_ohlcv(df)
    assert not any("median" in e for e in errors)


def test_volume_spike_fires_on_latest_bar_anomaly():
    """A 30x spike on today's bar should trigger the alert."""
    df = _make_volume_df()
    df.iloc[-1, df.columns.get_loc("volume")] = 30_000_000  # 30x normal
    errors = validate_ohlcv(df)
    assert any("median" in e for e in errors)


def test_volume_spike_no_alert_on_legitimate_high_volume():
    """A 10x volume day (e.g. results day) must NOT trigger an alert (threshold is 25x)."""
    df = _make_volume_df()
    df.iloc[-1, df.columns.get_loc("volume")] = 10_000_000  # 10x — real market event
    errors = validate_ohlcv(df)
    assert not any("median" in e for e in errors)


def test_volume_spike_skipped_when_insufficient_history():
    """With fewer than 10 bars, the check is skipped entirely."""
    df = _make_volume_df(n=5)
    df.iloc[-1, df.columns.get_loc("volume")] = 999_000_000
    errors = validate_ohlcv(df)
    assert not any("median" in e for e in errors)
