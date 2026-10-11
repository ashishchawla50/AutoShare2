"""Parity: backtest/fast engine vs the FROZEN reference oracle fixtures.

The fixtures were captured from the slow reference engine (3.4 h for the full
window). The fast engine must reproduce every one of them exactly: trade count,
final capital, every candidate/rejection counter, and every trade field by field.
Never edit a fixture to make this pass — diagnose the engine.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "oracle"
DB = REPO / "data" / "reconstructed.db"

TRADE_FIELDS = ("isin", "symbol", "strategy", "entry_date", "exit_date",
                "entry_price", "exit_price", "shares", "position_value",
                "realised_pnl", "realised_pnl_pct", "exit_reason")
PRICE_TOL = 1e-6

pytestmark = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")


def _fixture_paths() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json") if "superseded" not in p.name)


@pytest.fixture(scope="module")
def shared(request):
    """Load arrays once for the module — the point of the fast engine."""
    from config.config import config as cfg
    old = cfg.DB_PATH, cfg.MIN_BREAKOUT_VOLUME_MULTIPLIER
    cfg.DB_PATH = str(REPO / "data")
    # The fixtures were captured at 2.0x (default until 2026-10-11). Both engines read this
    # value in one identical comparison, so the proof holds for any value; it is pinned here
    # because the fixtures froze 2.0x. The reference engine that captured them (with the
    # union-preload fix) was never committed, so they cannot be re-captured from this repo.
    cfg.MIN_BREAKOUT_VOLUME_MULTIPLIER = 2.0
    from backtest.fast.data import Universe, load_market_data
    from backtest.fast.signals import load_signals
    md = load_market_data()
    out = {"md": md, "sig": load_signals(md), "uni": Universe()}
    yield out
    cfg.DB_PATH, cfg.MIN_BREAKOUT_VOLUME_MULTIPLIER = old


@pytest.fixture
def real_data_paths(monkeypatch):
    from config.config import config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(REPO / "data"))


def _run(fx: dict, shared: dict):
    from backtest.fast.engine import FastBacktestEngine
    md, cfg = fx["metadata"], fx["metadata"]["config"]
    assert cfg["strategy"] == "vcp_v2"
    e = FastBacktestEngine(
        start_date=date.fromisoformat(md["start_date"]),
        end_date=date.fromisoformat(md["end_date"]),
        starting_capital=cfg["starting_capital"],
        cost_profile=cfg["cost_profile"],
        market=shared["md"], signals=shared["sig"], universe=shared["uni"],
        verbose=False,
    )
    metrics = e.run()
    trades = [{
        "isin": t["isin"], "symbol": t["symbol"], "strategy": t["strategy"],
        "entry_date": str(t["entry_date"]), "exit_date": str(t["exit_date"]),
        "entry_price": round(float(t["entry_price"]), 6),
        "exit_price": round(float(t["exit_price"]), 6),
        "shares": int(t["shares"]),
        "position_value": round(float(t["entry_price"]) * int(t["shares"]), 6),
        "realised_pnl": round(float(t["realised_pnl"]), 6),
        "realised_pnl_pct": round(float(t["realised_pnl_pct"]), 6),
        "exit_reason": t["exit_reason"],
    } for t in e._closed_trades]
    return e, metrics, trades


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_fast_engine_matches_oracle(path, shared, real_data_paths):
    fx = json.loads(path.read_text())
    from backtest.fast.data import dataset_key
    assert dataset_key() == fx["metadata"]["dataset"]["sha256"], "fixture is for another dataset"

    engine, metrics, trades = _run(fx, shared)

    for k, expected in fx["stats"].items():
        assert engine._stats.get(k) == expected, f"stat {k}: {engine._stats.get(k)} != {expected}"
    assert len(trades) == len(fx["trades"]), "trade count differs"
    for i, (got, want) in enumerate(zip(trades, fx["trades"])):
        for f in TRADE_FIELDS:
            g, w = got.get(f), want.get(f)
            if isinstance(w, float):
                assert abs(g - w) < PRICE_TOL, f"trade {i} {f}: {g} != {w}"
            else:
                assert g == w, f"trade {i} {f}: {g!r} != {w!r}"
    assert metrics.total_trades == fx["portfolio"]["n_trades"]
    assert abs(engine.capital - fx["portfolio"]["final_capital"]) < 1e-6, "final capital differs"


def test_fast_engine_is_deterministic(shared, real_data_paths):
    fx = json.loads((FIXTURES / "smoke.json").read_text())
    e1, _, t1 = _run(fx, shared)
    e2, _, t2 = _run(fx, shared)
    assert e1.capital == e2.capital and t1 == t2


def test_missing_benchmark_aborts(shared, real_data_paths, monkeypatch):
    import backtest.fast.engine as fe
    monkeypatch.setattr(fe, "load_nifty_ohlcv", lambda *a, **k: None)
    e = fe.FastBacktestEngine(date(2023, 1, 2), date(2023, 1, 10), market=shared["md"],
                              signals=shared["sig"], universe=shared["uni"], verbose=False)
    with pytest.raises(fe.MissingBenchmarkError):
        e.run()
