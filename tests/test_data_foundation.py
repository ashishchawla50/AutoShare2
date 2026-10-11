"""Regression tests for the data foundation.

These exist to make the old failure modes impossible to reintroduce silently:
  - a fabricated, survivorship-biased universe (universe_snapshots backdated)
  - symbol-keyed historical identity that breaks on a ticker change
  - phantom pre-IPO price rows
  - corporate-action artifacts (missed splits, multi-action ex-dates, demergers
    silently "repaired")
  - a backtest quietly reading market_data.db again

Tests needing the reconstructed dataset skip cleanly when it is absent, so the
suite still runs on a fresh clone. The data-source guard tests always run.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
DB = REPO / "data" / "reconstructed.db"
needs_db = pytest.mark.skipif(not DB.exists(), reason="reconstructed.db not built here")


@pytest.fixture(scope="module")
def con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    yield c
    c.close()


@pytest.fixture
def pit(monkeypatch):
    from config.config import config
    monkeypatch.setattr(config, "BACKTEST_DATA_SOURCE", "reconstructed")
    monkeypatch.setattr(config, "DB_PATH", str(REPO / "data"))
    from backtest.point_in_time_universe import PointInTimeUniverse
    return PointInTimeUniverse()


# ── data-source guard (always runs) ───────────────────────────────────────────

def test_default_source_is_reconstructed():
    from config.config import config
    assert config.BACKTEST_DATA_SOURCE == "reconstructed"
    assert config.ALLOW_FABRICATED_BACKTEST_DATA is False


def test_backtest_cannot_silently_use_market_data_db(monkeypatch):
    """Selecting the fabricated dataset must raise, not quietly produce numbers."""
    from config.config import config
    from backtest.data_loader import FabricatedDataSourceError, load_ohlcv
    monkeypatch.setattr(config, "BACKTEST_DATA_SOURCE", "legacy_fabricated")
    monkeypatch.setattr(config, "ALLOW_FABRICATED_BACKTEST_DATA", False)
    with pytest.raises(FabricatedDataSourceError):
        load_ohlcv("INE001A01036", date(2022, 1, 1), date(2022, 6, 1))


def test_universe_refuses_fabricated_source(monkeypatch):
    from config.config import config
    from backtest.point_in_time_universe import (FabricatedUniverseError,
                                                 PointInTimeUniverse)
    monkeypatch.setattr(config, "BACKTEST_DATA_SOURCE", "legacy_fabricated")
    with pytest.raises(FabricatedUniverseError):
        PointInTimeUniverse()


def test_unknown_source_rejected(monkeypatch):
    from config.config import config
    from backtest.data_loader import FabricatedDataSourceError, load_ohlcv
    monkeypatch.setattr(config, "BACKTEST_DATA_SOURCE", "something_else")
    with pytest.raises(FabricatedDataSourceError):
        load_ohlcv("INE001A01036", date(2022, 1, 1), date(2022, 6, 1))


def test_universe_module_does_not_query_snapshots():
    txt = (REPO / "backtest" / "point_in_time_universe.py").read_text(encoding="utf8")
    assert "FROM universe_snapshots" not in txt
    assert "universe_liquid500" in txt


def test_loader_historical_path_does_not_read_ohlcv_daily():
    """Only the Nifty benchmark may still come from market_data.db."""
    txt = (REPO / "backtest" / "data_loader.py").read_text(encoding="utf8")
    body = txt.split("def load_nifty_ohlcv")[0]
    assert "ohlcv_daily" not in body
    assert "ohlcv_adjusted" in body


# ── universe correctness ──────────────────────────────────────────────────────

@needs_db
def test_historical_universe_differs_from_today(pit):
    """The core survivorship fix: 2022 membership must NOT equal today's."""
    old = set(pit.get_universe(date(2022, 10, 27)))
    new = set(pit.get_universe(date(2026, 7, 1)))
    assert old and new
    assert old != new
    assert len(old - new) > 50, "2022 universe suspiciously similar to today's"


@needs_db
def test_hdfc_exists_historically_then_disappears(pit):
    """HDFC was the 8th most-traded stock in 2022 and merged into HDFCBANK in 2023."""
    hdfc = "INE001A01036"
    assert hdfc in pit.get_universe(date(2022, 10, 27))
    assert hdfc not in pit.get_universe(date(2025, 10, 27))


@needs_db
def test_ticker_change_preserves_identity(pit):
    """IBULHSGFIN -> SAMMAANCAP is one security, not two."""
    isin = "INE148I01020"
    assert pit.isin_at("IBULHSGFIN", date(2021, 9, 30)) == isin
    assert pit.isin_at("SAMMAANCAP", date(2026, 7, 15)) == isin
    assert pit.symbol_at(isin, date(2021, 9, 30)) == "IBULHSGFIN"
    assert pit.symbol_at(isin, date(2026, 7, 15)) == "SAMMAANCAP"


@needs_db
def test_suvenphar_cohance_rename(pit):
    isin = "INE03QK01018"
    assert pit.isin_at("SUVENPHAR", date(2021, 6, 15)) == isin
    assert pit.symbol_at(isin, date(2026, 7, 15)) == "COHANCE"


@needs_db
def test_universe_is_deterministic(pit):
    d = date(2023, 5, 10)
    assert pit.get_universe(d) == pit.get_universe(d)


@needs_db
def test_month_boundary_membership_is_stable(pit, con):
    """Membership is monthly: it must not change within a calendar month."""
    ef, et = con.execute(
        "SELECT effective_from, effective_to FROM universe_liquid500 "
        "WHERE effective_from LIKE '2023-05%' LIMIT 1").fetchone()
    assert pit.get_universe(date.fromisoformat(ef)) == pit.get_universe(date.fromisoformat(et))


@needs_db
def test_first_month_has_no_universe(pit, con):
    """The archive's first month has no prior month to rank from -> empty, never
    silently backfilled from a later snapshot (the old fallback bug)."""
    first = con.execute("SELECT MIN(trade_date) FROM ohlcv_adjusted").fetchone()[0]
    assert pit.get_universe(date.fromisoformat(first)) == []


@needs_db
def test_no_same_day_turnover_leakage(con):
    """Membership must be computed strictly BEFORE it takes effect."""
    n = con.execute("SELECT COUNT(*) FROM universe_liquid500 "
                    "WHERE computed_asof >= effective_from").fetchone()[0]
    assert n == 0


@needs_db
def test_eligibility_thresholds_enforced(con):
    """>=15 sessions in the 63-day window and >=60 sessions of history."""
    ef, asof, isin = con.execute(
        "SELECT effective_from, computed_asof, isin FROM universe_liquid500 "
        "WHERE effective_from LIKE '2024-06%' ORDER BY rank LIMIT 1").fetchone()
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM ohlcv_adjusted WHERE trade_date<=? "
        "ORDER BY trade_date DESC LIMIT 250", (asof,))]
    n63 = con.execute("SELECT COUNT(*) FROM ohlcv_adjusted WHERE isin=? AND "
                      "trade_date<=? AND trade_date>=?", (isin, asof, days[62])).fetchone()[0]
    n250 = con.execute("SELECT COUNT(*) FROM ohlcv_adjusted WHERE isin=? AND "
                       "trade_date<=? AND trade_date>=?", (isin, asof, days[-1])).fetchone()[0]
    assert n63 >= 15 and n250 >= 60


@needs_db
def test_delisted_securities_are_present(con):
    n = con.execute("SELECT COUNT(*) FROM security_master "
                    "WHERE status!='active'").fetchone()[0]
    assert n > 300, "delisted securities missing -> survivorship bias is back"


# ── corporate actions ─────────────────────────────────────────────────────────

@needs_db
def test_tatasteel_split_applied(con):
    """1:10 split, 2022-07-28. Was missed because NSE writes 'Re 1/-' not 'Rs 1/-'."""
    r = con.execute("SELECT ratio, resolution FROM corporate_actions "
                    "WHERE symbol='TATASTEEL' AND ex_date='2022-07-28'").fetchone()
    assert r and abs(r[0] - 10.0) < 0.01 and r[1] == "applied"


@needs_db
def test_cesc_split_applied(con):
    r = con.execute("SELECT ratio, resolution FROM corporate_actions "
                    "WHERE symbol='CESC' AND ex_date='2021-09-17'").fetchone()
    assert r and abs(r[0] - 10.0) < 0.01 and r[1] == "applied"


@needs_db
def test_bajajfinsv_multi_action_date(con):
    """2022-09-13 was a 1:5 split AND a 1:1 bonus — ratios must multiply to 10."""
    r = con.execute("SELECT ratio FROM corporate_actions WHERE symbol='BAJAJFINSV' "
                    "AND ex_date='2022-09-13'").fetchone()
    assert r and abs(r[0] - 10.0) < 0.5, f"expected ~10, got {r}"


@needs_db
@pytest.mark.parametrize("sym,ex", [("VEDL", "2026-04-30"), ("SIEMENS", "2025-04-07"),
                                    ("ABFRL", "2025-05-22"), ("NMDC", "2022-10-27")])
def test_demergers_are_quarantined_not_invented(con, sym, ex):
    """Demergers must never be silently 'fixed' by a coincidental ratio match."""
    r = con.execute("SELECT resolution, ratio FROM corporate_actions "
                    "WHERE symbol=? AND ex_date=?", (sym, ex)).fetchone()
    assert r, f"{sym} {ex} not detected"
    assert r[0] == "UNVERIFIABLE", f"{sym} was adjusted without corroboration"


@needs_db
@pytest.mark.parametrize("sym,listed", [("RAINBOW", "2022-05-10"),
                                        ("LATENTVIEW", "2021-11-23")])
def test_no_phantom_pre_ipo_rows(con, sym, listed):
    """Rows predating the real listing inflated the VCP prior-uptrend gate to
    ~+59,900%, permanently exempting these two from it."""
    r = con.execute("SELECT MIN(trade_date) FROM ohlcv_adjusted WHERE symbol=?",
                    (sym,)).fetchone()
    assert r[0] is None or r[0] >= listed, f"{sym} has rows before {listed}: {r[0]}"


@needs_db
def test_no_unquarantined_residual_breaks(con):
    n = con.execute("""SELECT COUNT(*) FROM corporate_actions ca
                       WHERE ca.resolution IN ('applied_residual_break','UNVERIFIABLE')
                       AND NOT EXISTS (SELECT 1 FROM quarantine q
                                       WHERE q.isin=ca.isin AND q.ex_date=ca.ex_date)""").fetchone()[0]
    assert n == 0


@needs_db
def test_easemytrip_quarantined_out_of_universe(con):
    """EASEMYTRIP carried an unrepaired break while sitting in the universe for
    ~16 months. It must no longer appear."""
    n = con.execute("SELECT COUNT(*) FROM universe_liquid500 "
                    "WHERE isin='INE07O001018'").fetchone()[0]
    assert n == 0


@needs_db
def test_no_quarantined_security_in_universe(con):
    n = con.execute("""SELECT COUNT(*) FROM universe_liquid500 u JOIN quarantine q
                       ON q.isin=u.isin
                       WHERE q.from_date<=u.effective_to AND q.to_date>=u.effective_from
                    """).fetchone()[0]
    assert n == 0


# ── dataset integrity ─────────────────────────────────────────────────────────

@needs_db
def test_no_impossible_ohlc(con):
    n = con.execute("""SELECT COUNT(*) FROM ohlcv_adjusted
                       WHERE high<low OR close>high OR close<low
                          OR open>high OR open<low OR close<=0""").fetchone()[0]
    assert n == 0


@needs_db
def test_no_missing_or_duplicate_isin(con):
    assert con.execute("SELECT COUNT(*) FROM ohlcv_adjusted "
                       "WHERE isin IS NULL OR isin=''").fetchone()[0] == 0
    assert con.execute("""SELECT COUNT(*) FROM (SELECT isin, trade_date
                          FROM ohlcv_adjusted GROUP BY 1,2 HAVING COUNT(*)>1)
                       """).fetchone()[0] == 0
