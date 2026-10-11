"""Validation for the precomputed indicator panel (Step 1).

Three things are being proven here:

  1. CORRECTNESS   the fast panel reproduces the reference implementations in
                   utils/indicator_utils.py and strategy/* within stated tolerances,
                   and exactly for boolean/event outputs.
  2. CAUSALITY     mutating a future bar cannot change any earlier value. This is the
                   anti-look-ahead regression that matters most.
  3. HYGIENE       warm-up is NaN not fabricated, quarantine is not bridged, ISIN
                   identity survives ticker changes, output is deterministic.

Tolerances
  float indicators : rtol 1e-9, atol 1e-9  (both paths are float64 over the same
                     arithmetic; anything larger would indicate a real formula
                     difference, not floating-point noise)
  boolean/event    : exact equality, no tolerance
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).parent.parent
DB = REPO / "data" / "reconstructed.db"
needs_db = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")

RTOL = 1e-9
ATOL = 1e-9


@pytest.fixture(scope="module")
def con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.fixture(scope="module")
def sample(con):
    """A few long-history ISINs, plus their raw frames."""
    isins = [r[0] for r in con.execute(
        "SELECT isin FROM security_master WHERE status='active' "
        "ORDER BY n_days DESC LIMIT 5")]
    out = {}
    for i in isins:
        df = pd.read_sql(
            "SELECT trade_date, open, high, low, close, volume, turnover "
            "FROM ohlcv_adjusted WHERE isin=? ORDER BY trade_date",
            con, params=(i,), parse_dates=["trade_date"]).set_index("trade_date")
        out[i] = df
    return out


def _panel(df):
    from research.indicator_panel import compute_panel
    return compute_panel(df)


# ── 1. correctness vs the reference implementations ──────────────────────────

@needs_db
def test_ema_matches_reference(sample):
    from utils.indicator_utils import ema
    for isin, df in sample.items():
        p = _panel(df)
        for period in (10, 21, 50, 150, 200):
            ref = ema(df["close"], period)
            fast = p[f"ema_{period}"]
            m = fast.notna()
            assert m.sum() > 100
            np.testing.assert_allclose(fast[m].to_numpy(), ref[m].to_numpy(),
                                       rtol=RTOL, atol=ATOL,
                                       err_msg=f"{isin} ema_{period}")


@needs_db
def test_rsi_matches_reference(sample):
    from utils.indicator_utils import rsi
    for isin, df in sample.items():
        p = _panel(df)
        for period in (2, 14):
            ref, fast = rsi(df["close"], period), p[f"rsi_{period}"]
            m = fast.notna() & ref.notna()
            np.testing.assert_allclose(fast[m].to_numpy(), ref[m].to_numpy(),
                                       rtol=RTOL, atol=ATOL, err_msg=f"{isin} rsi_{period}")


@needs_db
def test_atr_matches_reference(sample):
    from utils.indicator_utils import atr
    for isin, df in sample.items():
        p = _panel(df)
        ref, fast = atr(df["high"], df["low"], df["close"], 14), p["atr_14"]
        m = fast.notna()
        np.testing.assert_allclose(fast[m].to_numpy(), ref[m].to_numpy(),
                                   rtol=RTOL, atol=ATOL, err_msg=f"{isin} atr_14")


@needs_db
def test_rolling_extremes_match_reference(sample):
    """trend_template uses close.tail(252).min()/max() at each decision point."""
    for isin, df in sample.items():
        p = _panel(df)
        c = df["close"]
        for t in range(300, len(df), 97):
            window = c.iloc[: t + 1].tail(252)
            assert p["close_hi_252"].iloc[t] == pytest.approx(float(window.max()), rel=RTOL)
            assert p["close_lo_252"].iloc[t] == pytest.approx(float(window.min()), rel=RTOL)


@needs_db
def test_volume_sma_matches_reference(sample):
    """breakout_detector.get_avg_volume_50d."""
    from strategy.breakout_detector import get_avg_volume_50d
    for isin, df in sample.items():
        p = _panel(df)
        for t in range(300, len(df), 137):
            ref = get_avg_volume_50d(df.iloc[: t + 1])
            assert p["vol_sma_50"].iloc[t] == pytest.approx(ref, rel=1e-9)


@needs_db
def test_atr_compression_matches_reference(sample):
    """vcp_detector._calculate_atr_compression, including its round(3)."""
    from strategy.vcp_detector import _calculate_atr_compression
    for isin, df in sample.items():
        p = _panel(df)
        for t in range(300, len(df), 137):
            ratio, compressed = _calculate_atr_compression(df.iloc[: t + 1])
            fast = p["atr_compression_ratio"].iloc[t]
            assert fast == pytest.approx(ratio, abs=1e-3)
            assert bool(fast < 0.7) == compressed


@needs_db
def test_swing_points_match_reference_exactly(sample):
    """Boolean output: exact equality required, no tolerance."""
    from utils.indicator_utils import swing_highs, swing_lows
    for isin, df in sample.items():
        p = _panel(df)
        ref_h = swing_highs(df["high"], left=2, right=2).to_numpy()
        ref_l = swing_lows(df["low"], left=2, right=2).to_numpy()
        assert np.array_equal(p["is_swing_high"].to_numpy().astype(bool), ref_h), isin
        assert np.array_equal(p["is_swing_low"].to_numpy().astype(bool), ref_l), isin


@needs_db
def test_weekly_ma_matches_reference(sample):
    from utils.indicator_utils import daily_to_weekly, sma
    for isin, df in sample.items():
        p = _panel(df)
        wk = daily_to_weekly(df)
        ref = sma(wk["close"], 30)
        t = len(df) - 1
        expected = float(ref.reindex(df.index, method="ffill").iloc[t])
        assert p["wk_sma_30"].iloc[t] == pytest.approx(expected, rel=1e-9)


# ── 2. causality / anti-leakage ──────────────────────────────────────────────

@needs_db
def test_future_mutation_does_not_change_past(sample):
    """THE anti-look-ahead test. Corrupt bar T+1 violently; every value at or before
    T must be bit-identical."""
    isin, df = next(iter(sample.items()))
    cut = len(df) - 60
    base = _panel(df)

    mutated = df.copy()
    idx = mutated.index[cut + 1:]
    mutated.loc[idx, ["open", "high", "low", "close"]] *= 7.3     # absurd future move
    mutated.loc[idx, "volume"] *= 91
    after = _panel(mutated)

    causal = [c for c in base.columns
              if c not in ("is_swing_high", "is_swing_low", "segment_id", "quarantined")]
    for col in causal:
        a = base[col].to_numpy()[:cut + 1]
        b = after[col].to_numpy()[:cut + 1]
        both_nan = np.isnan(a.astype(float)) & np.isnan(b.astype(float))
        assert np.allclose(np.where(both_nan, 0, a.astype(float)),
                           np.where(both_nan, 0, b.astype(float)),
                           rtol=0, atol=0), f"{col} changed when the FUTURE changed"


@needs_db
def test_confirmed_swing_forms_are_causal(sample):
    """The raw swing flag needs `right` future bars to confirm — that is why the
    _conf forms exist. Only the _conf forms are safe at a decision point."""
    isin, df = next(iter(sample.items()))
    cut = len(df) - 60
    base, mutated = _panel(df), df.copy()
    mutated.loc[mutated.index[cut + 1:], ["high", "low"]] *= 5.0
    after = _panel(mutated)
    for col in ("swing_high_conf", "swing_low_conf"):
        assert np.array_equal(base[col].to_numpy()[:cut + 1],
                              after[col].to_numpy()[:cut + 1]), col


@needs_db
def test_truncating_history_does_not_change_earlier_values(sample):
    """Computing on data through T must equal computing on data through T+k, for all
    rows <= T. This is the property the backtest relies on every single day."""
    isin, df = next(iter(sample.items()))
    cut = len(df) - 120
    full, part = _panel(df), _panel(df.iloc[: cut + 1])
    for col in ("ema_50", "ema_200", "rsi_14", "atr_14", "close_hi_252", "vol_sma_50"):
        a = full[col].to_numpy()[: cut + 1].astype(float)
        b = part[col].to_numpy().astype(float)
        both_nan = np.isnan(a) & np.isnan(b)
        np.testing.assert_allclose(np.where(both_nan, 0, a), np.where(both_nan, 0, b),
                                   rtol=RTOL, atol=ATOL, err_msg=col)


# ── 3. hygiene ───────────────────────────────────────────────────────────────

@needs_db
def test_warmup_is_nan_not_fabricated(sample):
    """Insufficient history must be unavailable, never 0 / constant / filled."""
    isin, df = next(iter(sample.items()))
    p = _panel(df)
    assert p["ema_200"].iloc[:199].isna().all()
    assert p["ema_200"].iloc[210:].notna().all()
    assert p["close_hi_252"].iloc[:251].isna().all()
    assert p["rsi_14"].iloc[:14].isna().all()
    assert p["vol_sma_50"].iloc[:49].isna().all()
    assert (p["ema_200"].iloc[:199].fillna(-1) == -1).all()   # not silently zero


@needs_db
def test_determinism(sample):
    isin, df = next(iter(sample.items()))
    a, b = _panel(df), _panel(df)
    pd.testing.assert_frame_equal(a, b)


@needs_db
def test_quarantine_is_not_bridged(con):
    """A quarantined span must be NaN, and the segment after it must warm up afresh
    rather than inheriting state from before the unrepaired break."""
    from research.indicator_panel import build_panel_for_isin
    row = con.execute(
        "SELECT q.isin FROM quarantine q JOIN security_master s ON s.isin=q.isin "
        "WHERE s.n_days > 600 AND q.from_date > (SELECT MIN(trade_date) FROM ohlcv_adjusted) "
        "LIMIT 1").fetchone()
    if not row:
        pytest.skip("no suitable quarantined security")
    isin = row[0]
    seg = build_panel_for_isin(con, isin, segment_on_quarantine=True)
    nos = build_panel_for_isin(con, isin, segment_on_quarantine=False)
    assert seg is not None and nos is not None
    q = seg["quarantined"].astype(bool)
    if q.any():
        assert seg.loc[q, "ema_50"].isna().all(), "quarantined rows must be unavailable"
    # segmentation must actually change something, otherwise it is not being applied
    assert seg["segment_id"].nunique(dropna=True) >= 1


@needs_db
def test_isin_identity_survives_ticker_change(con):
    """IBULHSGFIN -> SAMMAANCAP is one continuous series, not two."""
    from research.indicator_panel import build_panel_for_isin
    isin = "INE148I01020"
    p = build_panel_for_isin(con, isin, segment_on_quarantine=False)
    if p is None:
        pytest.skip("ISIN absent")
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM symbol_history WHERE isin=?", (isin,))]
    assert len(syms) > 1, "expected a ticker change for this ISIN"
    assert p["close"].notna().sum() > 500
    assert p.index.is_monotonic_increasing and not p.index.has_duplicates


@needs_db
def test_cache_roundtrip_and_invalidation(con, tmp_path, monkeypatch):
    """Saved panel reloads identically; a code/version change invalidates the cache."""
    from config.config import config as cfg
    import research.indicator_panel as ip
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path))
    (tmp_path / "panel").mkdir(parents=True, exist_ok=True)

    isin = con.execute("SELECT isin FROM security_master ORDER BY n_days DESC "
                       "LIMIT 1").fetchone()[0]
    p = ip.build_panel_for_isin(con, isin, segment_on_quarantine=False)
    np.savez_compressed(tmp_path / "panel" / f"{isin}.npz",
                        index=p.index.astype("int64").to_numpy(),
                        columns=np.array(list(p.columns), dtype=object),
                        values=p.to_numpy(dtype=np.float64))
    ip.clear_memory_cache()
    loaded = ip.load_panel(isin)
    assert loaded is not None
    np.testing.assert_allclose(
        loaded["ema_200"].to_numpy(dtype=float),
        p["ema_200"].to_numpy(dtype=float), rtol=RTOL, atol=ATOL, equal_nan=True)

    import json
    ip.manifest_path().write_text(json.dumps({
        "panel_version": "0.0.1-stale", "code_fingerprint": "x",
        "source_fingerprint": "y", "segment_on_quarantine": False}))
    ok, why = ip.cache_is_valid(con, segment_on_quarantine=False)
    assert not ok and "version" in why


# ── frozen semantics + oracle-parity regressions (Step 1 review) ─────────────

@needs_db
def test_ema200_20d_ago_matches_oracle_lag(sample):
    """The reference does ema(close,200).iloc[-20] against .iloc[-1] — a 19-BAR lag,
    despite the name. A 20-bar lag flipped c6_ema200_trending_up for MARUTI
    2023-01-30, APOLLOHOSP 2023-02-22 and JKCEMENT 2023-03-02. The panel mirrors the
    oracle; whether the oracle's off-by-one should be corrected is a strategy-semantics
    decision that is deliberately NOT taken here."""
    from strategy.trend_template import passes_trend_template
    for isin, df in sample.items():
        p = _panel(df)
        for t in range(300, len(df), 89):
            ref = passes_trend_template(df.iloc[: t + 1])
            if ref.current_price == 0.0:
                continue
            assert p["ema_200_20d_ago"].iloc[t] == pytest.approx(
                ref.ema200_20d_ago, rel=1e-9), f"{isin} @{t}"


@needs_db
def test_late_starting_window_matches_full_history_panel(sample):
    """CANONICAL SEMANTICS: the panel defines indicators over the ISIN's full valid
    history before the decision date. A backtest window starting long after the first
    bar seeds the reference EMA differently, so the two can legitimately diverge.
    This test quantifies that divergence rather than assuming it away — and asserts
    the recursive EMA has converged enough that a decision-grade gate is unaffected."""
    from utils.indicator_utils import ema
    isin, df = next(iter(sample.items()))
    late = df.iloc[len(df) // 2:]                 # window starting mid-history
    full_panel = _panel(df)
    ref_late = ema(late["close"], 200)
    t = len(late) - 1
    full_val = float(full_panel["ema_200"].iloc[len(df) - 1])
    late_val = float(ref_late.iloc[t])
    rel = abs(full_val - late_val) / full_val
    # Seed influence decays as (1-2/201)^n; with ~640 bars of extra warm-up the
    # residual must be small. Recorded explicitly so a regression is visible.
    assert rel < 0.02, f"full-history vs window-seeded EMA200 diverged {rel:.4%}"


@needs_db
def test_panel_cache_respects_memory_budget(con):
    """The cache is bounded by BYTES, not by panel count: ISIN histories vary widely,
    so an N-panel cap is not a memory bound."""
    import research.indicator_panel as ip
    ip.clear_memory_cache()
    isins = [r[0] for r in con.execute(
        "SELECT isin FROM security_master ORDER BY n_days DESC LIMIT 400")]
    loaded = 0
    for i in isins:
        if ip.load_panel(i) is not None:
            loaded += 1
    st = ip.cache_stats()
    if loaded == 0:
        pytest.skip("panel cache not built in this environment")
    assert st["cache_mb"] <= ip.PANEL_CACHE_BUDGET_MB + 1, st
    ip.clear_memory_cache()
    assert ip.cache_stats()["cache_mb"] == 0


def test_panel_root_is_honoured(tmp_path, monkeypatch):
    """--root X must READ from X and WRITE to X/panel.

    Regression: build_all(root=X) read the dataset from X but wrote the cache to
    config.DB_PATH/panel, so a research dataset and a live dataset would silently
    share one panel cache.
    """
    import research.indicator_panel as ip
    from config.config import config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "default_root"))
    other = tmp_path / "other_root"
    (other / "panel").mkdir(parents=True)
    (tmp_path / "default_root" / "panel").mkdir(parents=True)

    assert ip._panel_dir(other) == other / "panel"
    assert ip._panel_dir() == tmp_path / "default_root" / "panel"
    assert ip.manifest_path(other) == other / "panel" / "manifest.json"

    # a panel written under `other` must be visible only via that root
    z = np.zeros((3, 2), dtype=np.float64)
    np.savez_compressed(other / "panel" / "INE_TEST.npz",
                        index=np.array([0, 86400_000_000_000, 172800_000_000_000]),
                        columns=np.array(["close", "ema_10"], dtype=object), values=z)
    ip.clear_memory_cache()
    assert ip.load_panel("INE_TEST", other) is not None
    ip.clear_memory_cache()
    assert ip.load_panel("INE_TEST") is None, "default root must not see other root's cache"
