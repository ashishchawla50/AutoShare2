"""Fast parity: optimized engine vs FROZEN reference oracle fixtures.

These tests never execute the reference engine. It was run once by
`tools/capture_reference_oracle.py` and its output frozen under
`tests/fixtures/oracle/`. Verification therefore costs one optimized run instead of
one slow reference run, which is what makes iterative development possible.

A matching final capital is NOT sufficient — balances can agree while individual
trades differ — so every trade is compared field by field, in order, along with the
candidate/rejection counters.

Fixtures are immutable. If a test fails, diagnose the engine; never edit the fixture.
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
PRICE_TOL = 1e-6      # float64 through identical arithmetic; larger means a real bug


def _fixtures() -> list:
    """Frozen fixtures, or a single skipped placeholder before any are captured
    (an empty parametrize list breaks collection)."""
    found = (sorted(p for p in FIXTURES.glob("*.json") if "superseded" not in p.name)
             if FIXTURES.exists() else [])
    return found or [pytest.param(None, marks=pytest.mark.skip(reason="no oracle fixture yet"))]


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def _run_optimized(fx: dict):
    """Run the optimized engine under the fixture's exact configuration."""
    from backtest.backtest_engine import BacktestEngine
    cfg, md = fx["metadata"]["config"], fx["metadata"]
    e = BacktestEngine(
        start_date=date.fromisoformat(md["start_date"]),
        end_date=date.fromisoformat(md["end_date"]),
        starting_capital=cfg["starting_capital"],
        strategy=cfg["strategy"],
        cost_profile=cfg["cost_profile"],
        use_panel=True,
    )
    metrics = e.run()
    trades = [{
        "isin": t.get("isin"), "symbol": t.get("symbol"), "strategy": t.get("strategy"),
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


pytestmark = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")


@pytest.fixture
def real_data_paths(monkeypatch):
    """conftest isolates DB_PATH to tmp_path; tests that touch the real research
    dataset (fingerprints, engine runs) must point back at it."""
    from config.config import config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(REPO / "data"))
    return REPO / "data"




# ── fixture integrity (cheap, no engine run) ─────────────────────────────────

@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: getattr(p, "stem", "none"))
def test_fixture_not_tampered(path):
    """A fixture is a frozen artifact, not ordinary mutable test data."""
    from tools.capture_reference_oracle import canonical_hash
    fx = _load(path)
    assert fx.get("content_sha256"), "fixture missing content hash"
    assert canonical_hash(fx) == fx["content_sha256"], (
        f"{path.name} content changed after capture — do not hand-edit oracle fixtures")


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: getattr(p, "stem", "none"))
def test_fixture_schema(path):
    fx = _load(path)
    for k in ("metadata", "portfolio", "stats", "trades"):
        assert k in fx, f"{path.name} missing {k}"
    md = fx["metadata"]
    for k in ("fixture_version", "window", "start_date", "end_date", "git_commit",
              "dataset", "config", "reference_runtime_s"):
        assert k in md, f"{path.name} metadata missing {k}"
    assert md["dataset"].get("sha256") and md["config"].get("sha256")
    assert fx["portfolio"]["ledger_error"] < 5.0, "oracle captured a broken ledger"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: getattr(p, "stem", "none"))
def test_fixture_dataset_still_matches(path, real_data_paths):
    """A fixture must not silently apply to a different dataset."""
    from tools.capture_reference_oracle import dataset_fingerprint
    fx = _load(path)
    assert dataset_fingerprint()["sha256"] == fx["metadata"]["dataset"]["sha256"], (
        f"{path.name} was captured against a different research dataset — "
        "recapture the oracle rather than comparing across datasets")


def test_smoke_fixture_reproduces_known_oracle():
    """The independently-known pre-Step-2 values. Never edit these to pass."""
    p = FIXTURES / "smoke.json"
    if not p.exists():
        pytest.skip("smoke fixture not captured yet")
    fx = _load(p)
    assert fx["portfolio"]["n_trades"] == 3
    assert abs(fx["portfolio"]["final_capital"] - 49255.590597) < 1e-6
    assert fx["stats"]["fail_tt"] == 19008
    assert fx["stats"]["fail_vcp"] == 5624


# ── parity: optimized engine vs frozen oracle ────────────────────────────────

@pytest.mark.slow
@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: getattr(p, "stem", "none"))
def test_optimized_matches_oracle(path, real_data_paths):
    fx = _load(path)
    engine, metrics, trades = _run_optimized(fx)

    # portfolio
    assert metrics.total_trades == fx["portfolio"]["n_trades"], "trade COUNT differs"
    assert abs(engine.capital - fx["portfolio"]["final_capital"]) < 1e-6, "final capital differs"
    total_pnl = sum(t["realised_pnl"] for t in trades)
    assert abs(total_pnl - (engine.capital - fx["portfolio"]["starting_capital"])) < 5.0

    # candidate / rejection counters — catches divergence even with zero trades
    for k, expected in fx["stats"].items():
        assert engine._stats.get(k) == expected, f"stat {k}: {engine._stats.get(k)} != {expected}"

    # every trade, in order, field by field
    assert len(trades) == len(fx["trades"]), "trade count differs"
    for i, (got, want) in enumerate(zip(trades, fx["trades"])):
        for f in TRADE_FIELDS:
            g, w = got.get(f), want.get(f)
            if isinstance(w, float):
                assert abs(g - w) < PRICE_TOL, f"trade {i} {f}: {g} != {w}"
            else:
                assert g == w, f"trade {i} {f}: {g!r} != {w!r}"


@pytest.mark.slow
def test_optimized_is_deterministic(real_data_paths):
    """Same input, same output — twice."""
    p = FIXTURES / "smoke.json"
    if not p.exists():
        pytest.skip("smoke fixture not captured yet")
    fx = _load(p)
    e1, _, t1 = _run_optimized(fx)
    e2, _, t2 = _run_optimized(fx)
    assert e1.capital == e2.capital
    assert t1 == t2


# ── research-integrity: the benchmark must fail closed ───────────────────────

@pytest.fixture
def real_data_paths(monkeypatch):
    """conftest isolates DB_PATH to tmp_path; these tests need the real dataset so
    the run reaches the benchmark check rather than failing earlier on the universe."""
    from config.config import config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(REPO / "data"))
    return REPO / "data"



def test_missing_benchmark_aborts_the_backtest(monkeypatch, real_data_paths):
    """A missing NIFTY50 series must raise, never silently disable the regime gate.

    Regression for 2026-08-12: local market_data.db had 0 NIFTY50 rows, so
    build_regime_series was skipped and every day was treated as BULL. The run
    completed and would have frozen a wrong oracle.
    """
    import backtest.backtest_engine as be
    monkeypatch.setattr(be, "load_nifty_ohlcv", lambda *a, **k: None)
    e = be.BacktestEngine(start_date=date(2023, 1, 2), end_date=date(2023, 1, 10),
                          starting_capital=50000.0, strategy="vcp_v2",
                          cost_profile="cnc", use_panel=True)
    with pytest.raises(be.MissingBenchmarkError, match="NIFTY50"):
        e.run()


def test_short_benchmark_also_aborts(monkeypatch, real_data_paths):
    """A truncated benchmark is as dangerous as a missing one."""
    import pandas as pd
    import backtest.backtest_engine as be
    stub = pd.DataFrame(
        {"open": [1.0] * 10, "high": [1.0] * 10, "low": [1.0] * 10,
         "close": [1.0] * 10, "volume": [1] * 10},
        index=pd.date_range("2022-12-01", periods=10, freq="D"))
    monkeypatch.setattr(be, "load_nifty_ohlcv", lambda *a, **k: stub)
    e = be.BacktestEngine(start_date=date(2023, 1, 2), end_date=date(2023, 1, 10),
                          starting_capital=50000.0, strategy="vcp_v2",
                          cost_profile="cnc", use_panel=True)
    with pytest.raises(be.MissingBenchmarkError):
        e.run()
