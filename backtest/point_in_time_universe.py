"""Point-in-time universe — Historical Liquid-500, backed by the reconstructed dataset.

REPLACED 2026-08-11. The previous implementation read `universe_snapshots` in
fundamentals.db. That table was fabricated: its only writers persist the CURRENT
NSE index composition, and its single pre-2026 row set ("2019-01-01") was the
July-2026 Nifty 500 backdated — 499 of 500 symbols identical to today. Every
backtest run against it carried total survivorship and inclusion look-ahead bias.
See docs/UNIVERSE_RECONSTRUCTION_PLAN.md and docs/DATA_FOUNDATION_REVIEW.md.

This implementation reads `universe_liquid500` from data/reconstructed.db, built
by tools/reconstruct_history.py from the immutable NSE bhavcopy archive.

Membership rule (fixed; see the reconstruction tool for the authority):
  - monthly membership, effective for whole calendar months
  - ranked by MEDIAN DAILY TURNOVER over the trailing 63 sessions
  - the window ends on the LAST SESSION OF THE PREVIOUS MONTH, so membership is
    known before the first trade of the month — no same-day turnover leakage
  - requires >= 15 trading days in that window
  - requires >= 60 sessions of history in the trailing 250 (listing-age proxy)
  - top 500
  - securities under quarantine (unresolved corporate-action discontinuity) are
    excluded

Identity is ISIN, never symbol. get_universe() returns ISINs. Use symbol_at() to
resolve the ticker a security traded under on a given date.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

from config.config import config

logger = logging.getLogger("backtest.universe")


class FabricatedUniverseError(RuntimeError):
    """Raised when anything tries to source a historical universe from the old
    fabricated `universe_snapshots` table."""


class PointInTimeUniverse:
    """Historical Liquid-500 membership, keyed by ISIN.

    Deterministic: for a given reconstructed.db, get_universe(d) returns exactly
    the same ISIN list in the same rank order on every call.

        pit = PointInTimeUniverse()
        isins = pit.get_universe(date(2022, 10, 27))
        sym   = pit.symbol_at(isins[0], date(2022, 10, 27))
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if config.BACKTEST_DATA_SOURCE != "reconstructed":
            raise FabricatedUniverseError(
                f"BACKTEST_DATA_SOURCE={config.BACKTEST_DATA_SOURCE!r}. The historical "
                "universe is only available from the reconstructed dataset. The old "
                "universe_snapshots table is fabricated and must never be used for "
                "backtesting. See docs/DATA_FOUNDATION_REVIEW.md."
            )
        self._db_path = Path(db_path or (Path(config.DB_PATH) / config.RECONSTRUCTED_DB))
        if not self._db_path.exists():
            raise FileNotFoundError(
                f"Reconstructed dataset not found at {self._db_path}. Build it with:\n"
                "  python tools/fetch_bhavcopy.py --start 2021-06-01 --end <today>\n"
                "  python tools/reconstruct_history.py --root data --all"
            )
        # Read-only: a backtest must never be able to mutate the research dataset.
        self._con = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        self._cache: dict[date, list[str]] = {}

    # ── universe ──────────────────────────────────────────────────────────────

    def get_universe(self, as_of_date: date) -> list[str]:
        """Return Liquid-500 ISINs effective on as_of_date, best-ranked first.

        Returns [] for dates before the first eligible month (the first calendar
        month of the archive has no prior month to rank from) — an empty universe
        is correct there, and is never silently backfilled from another date.
        """
        if as_of_date in self._cache:
            return self._cache[as_of_date]
        d = as_of_date.isoformat()
        rows = self._con.execute(
            "SELECT u.isin FROM universe_liquid500 u "
            "WHERE u.effective_from <= ? AND u.effective_to >= ? "
            # Defence in depth: the table is already built with quarantine applied,
            # but re-check so a stale table can never leak a corrupted security.
            "AND NOT EXISTS (SELECT 1 FROM quarantine q WHERE q.isin = u.isin "
            "                AND q.from_date <= ? AND q.to_date >= ?) "
            "ORDER BY u.rank", (d, d, d, d)).fetchall()
        out = [r[0] for r in rows]
        self._cache[as_of_date] = out
        return out

    # ── identity ──────────────────────────────────────────────────────────────

    def symbol_at(self, isin: str, on: date) -> Optional[str]:
        """Ticker this ISIN traded under on a given date."""
        r = self._con.execute(
            "SELECT symbol FROM symbol_history WHERE isin=? AND from_date<=? AND to_date>=? "
            "ORDER BY from_date LIMIT 1", (isin, on.isoformat(), on.isoformat())).fetchone()
        return r[0] if r else None

    def isin_at(self, symbol: str, on: date) -> Optional[str]:
        """ISIN behind a ticker on a given date (tickers are reused over time)."""
        r = self._con.execute(
            "SELECT isin FROM symbol_history WHERE symbol=? AND from_date<=? AND to_date>=? "
            "ORDER BY from_date LIMIT 1", (symbol, on.isoformat(), on.isoformat())).fetchone()
        return r[0] if r else None

    def is_quarantined(self, isin: str, on: date) -> bool:
        d = on.isoformat()
        return self._con.execute(
            "SELECT 1 FROM quarantine WHERE isin=? AND from_date<=? AND to_date>=? LIMIT 1",
            (isin, d, d)).fetchone() is not None

    # ── introspection ─────────────────────────────────────────────────────────

    def get_available_dates(self) -> list[date]:
        return [date.fromisoformat(r[0]) for r in self._con.execute(
            "SELECT DISTINCT effective_from FROM universe_liquid500 ORDER BY effective_from")]

    def describe(self) -> dict:
        q = lambda s: self._con.execute(s).fetchone()[0]
        return {
            "source": str(self._db_path),
            "months": q("SELECT COUNT(DISTINCT effective_from) FROM universe_liquid500"),
            "isins": q("SELECT COUNT(*) FROM security_master"),
            "quarantined": q("SELECT COUNT(DISTINCT isin) FROM quarantine"),
            "first_month": q("SELECT MIN(effective_from) FROM universe_liquid500"),
            "last_month": q("SELECT MAX(effective_to) FROM universe_liquid500"),
        }
