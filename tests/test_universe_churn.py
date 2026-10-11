"""Universe-churn validation for the research engine (Step 2).

`tests/test_data_foundation.py` proves the *dataset* is point-in-time. These tests
prove the **engine** honours it — a different and easier thing to get wrong, because
an optimisation can quietly substitute today's universe, the start-date universe, or
a current ticker without changing any dataset.

Specifically guarded:
  - membership evolves month to month (not frozen at the run's start date)
  - a security present historically disappears after it stops trading (HDFC)
  - a ticker change does not split or duplicate a security (ISIN identity)
  - delisted securities are present in their own era and absent later
  - quarantined securities never enter the universe
  - the engine preloads the UNION of all monthly universes over the run window,
    not just the first month (regression: `_preload_data` originally loaded only
    the start-date universe, silently starving every later month)

These run against the reconstructed dataset directly and do NOT execute a backtest,
so they stay fast enough to run on every change.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
DB = REPO / "data" / "reconstructed.db"
pytestmark = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")

HDFC = "INE001A01036"          # merged into HDFCBANK, July 2023
IBULHSG = "INE148I01020"       # IBULHSGFIN -> SAMMAANCAP (2024-07-26)
SUVEN = "INE03QK01018"         # SUVENPHAR -> COHANCE
EASEMYTRIP = "INE07O001018"    # unrepairable corporate action -> full-history quarantine


@pytest.fixture
def real_data_paths(monkeypatch):
    """conftest isolates DB_PATH to tmp_path; these tests need the real dataset."""
    from config.config import config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(REPO / "data"))
    return REPO / "data"


@pytest.fixture
def pit(real_data_paths):
    from backtest.point_in_time_universe import PointInTimeUniverse
    return PointInTimeUniverse()


@pytest.fixture(scope="module")
def con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    yield c
    c.close()


# ── membership genuinely churns ──────────────────────────────────────────────

def test_membership_changes_month_to_month(pit, con):
    """If the engine ever froze the universe, every month would be identical."""
    months = [r[0] for r in con.execute(
        "SELECT DISTINCT effective_from FROM universe_liquid500 "
        "ORDER BY effective_from LIMIT 14")]
    sets = [set(pit.get_universe(date.fromisoformat(m))) for m in months]
    assert all(len(s) == 500 for s in sets), "every month should be a full 500"
    changes = [len(sets[i] - sets[i - 1]) for i in range(1, len(sets))]
    assert all(c > 0 for c in changes), f"a month with zero entries is suspicious: {changes}"
    assert sum(changes) / len(changes) > 5, f"implausibly static universe: {changes}"


def test_universe_is_not_the_start_date_universe(pit):
    """Regression: the engine once preloaded only the start-date membership."""
    early = set(pit.get_universe(date(2021, 10, 1)))
    late = set(pit.get_universe(date(2026, 7, 1)))
    assert early and late
    assert len(early - late) > 100, (
        "2021 and 2026 memberships are too similar — universe may be frozen")


def test_universe_is_not_todays_list(pit, con):
    """The reconstructed universe must not equal the current fabricated snapshot."""
    f = sqlite3.connect(f"file:{REPO / 'data' / 'fundamentals.db'}?mode=ro", uri=True)
    try:
        today = {r[0] for r in f.execute(
            "SELECT symbol FROM universe_snapshots "
            "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM universe_snapshots)")}
    except sqlite3.Error:
        pytest.skip("no fabricated snapshot present to compare against")
    finally:
        f.close()
    if not today:
        pytest.skip("no fabricated snapshot rows")
    hist = {pit.symbol_at(i, date(2022, 6, 1)) for i in pit.get_universe(date(2022, 6, 1))}
    hist.discard(None)
    overlap = len(hist & today) / max(len(hist), 1)
    assert overlap < 0.95, (
        f"2022 universe is {overlap:.0%} identical to today's list — "
        "the engine may have fallen back to the current universe")


# ── securities entering and leaving ──────────────────────────────────────────

def test_hdfc_present_historically_then_gone(pit):
    """HDFC was the 8th most-traded stock in Oct 2022 and merged away in 2023."""
    assert HDFC in pit.get_universe(date(2022, 10, 27)), "HDFC missing from 2022"
    assert HDFC in pit.get_universe(date(2023, 1, 2)), "HDFC missing from early 2023"
    assert HDFC not in pit.get_universe(date(2024, 10, 27)), "HDFC still present after merger"
    assert HDFC not in pit.get_universe(date(2026, 7, 1))


def test_delisted_securities_appear_in_their_own_era(pit, con):
    """Securities that stopped trading must still populate historical universes —
    otherwise survivorship bias is back."""
    rows = con.execute(
        "SELECT isin, first_date, last_date FROM security_master "
        "WHERE status != 'active' AND n_days > 400 ORDER BY last_date LIMIT 60").fetchall()
    seen_early = 0
    for isin, first, last in rows:
        f = date.fromisoformat(str(first)[:10])
        l = date.fromisoformat(str(last)[:10])
        # Probe the MIDDLE of the security's life. Probing near `last` would land
        # inside the tail quarantine (60 sessions before a series ends), which
        # correctly excludes it — that is the engine working, not failing.
        mid = date.fromordinal((f.toordinal() + l.toordinal()) // 2)
        if isin in pit.get_universe(mid):
            seen_early += 1
            assert isin not in pit.get_universe(date(2026, 7, 1)), (
                f"{isin} stopped trading {last} but is in the 2026 universe")
    assert seen_early > 0, (
        "no delisted security appears in any historical universe — survivorship "
        "bias would be back")


def test_universe_additions_have_sufficient_history(pit, con):
    """A newly added member must satisfy the 60-session history rule; a security
    cannot enter the universe on its listing day."""
    row = con.execute(
        "SELECT effective_from, computed_asof, isin FROM universe_liquid500 "
        "WHERE effective_from LIKE '2024-06%' ORDER BY rank DESC LIMIT 1").fetchone()
    ef, asof, isin = row
    first = con.execute("SELECT MIN(trade_date) FROM ohlcv_adjusted WHERE isin=?",
                        (isin,)).fetchone()[0]
    n = con.execute("SELECT COUNT(*) FROM ohlcv_adjusted WHERE isin=? AND trade_date<=?",
                    (isin, asof)).fetchone()[0]
    assert n >= 60, f"{isin} entered with only {n} sessions of history (first {first})"


# ── identity across ticker changes ───────────────────────────────────────────

@pytest.mark.parametrize("isin,old,new,switch", [
    (IBULHSG, "IBULHSGFIN", "SAMMAANCAP", "2024-07-26"),
    (SUVEN, "SUVENPHAR", "COHANCE", None),
])
def test_ticker_change_is_one_continuous_security(pit, con, isin, old, new, switch):
    before = date(2022, 6, 1)
    after = date(2026, 7, 15)
    assert pit.isin_at(old, before) == isin
    assert pit.isin_at(new, after) == isin
    assert pit.symbol_at(isin, before) == old
    assert pit.symbol_at(isin, after) == new
    # exactly one security, never two
    n = con.execute("SELECT COUNT(*) FROM security_master WHERE isin=?", (isin,)).fetchone()[0]
    assert n == 1


def test_universe_never_contains_the_same_security_twice(pit):
    """A ticker change must not produce a duplicate membership row."""
    for d in (date(2024, 7, 1), date(2024, 8, 1), date(2024, 9, 2)):
        u = pit.get_universe(d)
        assert len(u) == len(set(u)), f"duplicate ISIN in universe on {d}"


def test_symbol_resolution_is_era_correct(pit):
    """Resolving a ticker must use the era's mapping, not today's."""
    assert pit.symbol_at(IBULHSG, date(2023, 1, 2)) == "IBULHSGFIN"
    assert pit.symbol_at(IBULHSG, date(2025, 1, 2)) == "SAMMAANCAP"
    assert pit.isin_at("SAMMAANCAP", date(2023, 1, 2)) is None, (
        "SAMMAANCAP did not exist as a ticker in 2023")


# ── quarantine ───────────────────────────────────────────────────────────────

def test_quarantined_security_never_enters_universe(pit, con):
    assert not any(EASEMYTRIP in pit.get_universe(date(y, m, 1))
                   for y in (2021, 2022, 2023, 2024, 2025, 2026) for m in (1, 7)), \
        "EASEMYTRIP has an unrepairable corporate action and must never be tradeable"


def test_no_universe_month_contains_a_quarantined_security(con):
    n = con.execute("""SELECT COUNT(*) FROM universe_liquid500 u JOIN quarantine q
                       ON q.isin = u.isin
                       WHERE q.from_date <= u.effective_to
                         AND q.to_date   >= u.effective_from""").fetchone()[0]
    assert n == 0, f"{n} quarantined memberships leaked into the universe"


# ── the engine actually loads the churning universe ──────────────────────────

def test_engine_preloads_union_not_start_month(real_data_paths, monkeypatch):
    """Regression: `_preload_data` originally loaded only the start-date universe,
    so every later month silently had no data. Assert it loads the union."""
    from datetime import date as _d
    from backtest.backtest_engine import BacktestEngine
    from backtest.point_in_time_universe import PointInTimeUniverse

    start, end = _d(2023, 1, 2), _d(2023, 12, 29)
    pit = PointInTimeUniverse()
    first_month = set(pit.get_universe(start))
    union = set()
    for m in range(1, 13):
        union |= set(pit.get_universe(_d(2023, m, 15)))
    assert len(union) > len(first_month), "test window has no churn — pick another"

    captured = {}
    eng = BacktestEngine(start_date=start, end_date=end, starting_capital=50000.0,
                         strategy="vcp_v2", cost_profile="cnc", use_panel=True)

    # Intercept the loader: we only need to know WHICH ISINs it asks for.
    import backtest.backtest_engine as be
    def spy(isin, a, b):
        captured.setdefault("asked", set()).add(isin)
        return None
    monkeypatch.setattr(be, "load_ohlcv", spy)
    monkeypatch.setattr(be, "load_nifty_ohlcv", lambda *a, **k: None)
    with pytest.raises(be.MissingBenchmarkError):
        eng.run()          # aborts at the benchmark, AFTER the universe preload

    asked = captured.get("asked", set())
    assert asked, "preload requested no symbols"
    assert len(asked) >= len(union) * 0.95, (
        f"preload asked for {len(asked)} ISINs but the 2023 union is {len(union)} — "
        "the engine may be using only the start-date universe")
    assert len(asked) > len(first_month), (
        f"preload asked for {len(asked)}, first month alone is {len(first_month)}")
