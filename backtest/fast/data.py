"""Load the reconstructed dataset into flat numpy arrays, cached on disk.

Layout: every row of `ohlcv_adjusted`, sorted by (isin, trade_date). Security k
occupies rows offsets[k] : offsets[k+1]. Dates are int64 day numbers
(datetime64[D]). Row-local index i inside a security is what the reference engine
calls `df.iloc[i]` when it loads that ISIN's full history.

The first load reads SQLite (~5 s); later loads read the .npy cache (<0.5 s). The
cache is keyed on the Layer-3 dataset fingerprint, so a rebuilt dataset is never
served stale arrays.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from config.config import config

logger = logging.getLogger("backtest.fast.data")

CACHE_VERSION = "1"


def cache_dir() -> Path:
    d = Path(config.DB_PATH) / "cache" / "fast"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db_path() -> Path:
    p = Path(config.DB_PATH) / config.RECONSTRUCTED_DB
    if not p.exists():
        raise FileNotFoundError(
            f"Reconstructed dataset not found at {p}. Build it with "
            "tools/fetch_bhavcopy.py then tools/reconstruct_history.py --all")
    return p


def dataset_key() -> str:
    """Same fingerprint as tools/capture_reference_oracle.dataset_fingerprint.

    The COUNT(*) queries cost ~0.5 s, so the result is memoised against the
    database file's size and mtime.
    """
    st = _db_path().stat()
    memo = cache_dir() / "dataset_key.json"
    stamp = f"{st.st_size}:{st.st_mtime_ns}"
    try:
        saved = json.loads(memo.read_text())
        if saved.get("stamp") == stamp:
            return saved["key"]
    except (OSError, ValueError):
        pass
    key = _dataset_key_uncached()
    memo.write_text(json.dumps({"stamp": stamp, "key": key}))
    return key


def _dataset_key_uncached() -> str:
    con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    q = lambda s: con.execute(s).fetchone()[0]
    try:
        parts = {
            "adjusted_rows": q("SELECT COUNT(*) FROM ohlcv_adjusted"),
            "max_trade_date": str(q("SELECT MAX(trade_date) FROM ohlcv_adjusted")),
            "isins": q("SELECT COUNT(*) FROM security_master"),
            "quarantine_rows": q("SELECT COUNT(*) FROM quarantine"),
            "applied_actions": q("SELECT COUNT(*) FROM corporate_actions WHERE resolution='applied'"),
            "universe_months": q("SELECT COUNT(DISTINCT effective_from) FROM universe_liquid500"),
        }
    finally:
        con.close()
    return hashlib.sha256(
        "|".join(f"{k}={v}" for k, v in sorted(parts.items())).encode()).hexdigest()[:16]


@dataclass
class MarketData:
    key: str                  # dataset fingerprint
    isins: np.ndarray         # (S,) str, sorted
    offsets: np.ndarray       # (S+1,) int64
    dates: np.ndarray         # (N,) int64 day numbers
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __post_init__(self) -> None:
        self.index = {s: k for k, s in enumerate(self.isins.tolist())}

    def rows(self, isin: str) -> tuple[int, int]:
        k = self.index[isin]
        return int(self.offsets[k]), int(self.offsets[k + 1])


_FIELDS = ("offsets", "dates", "open", "high", "low", "close", "volume")


def load_market_data(rebuild: bool = False) -> MarketData:
    key = dataset_key()
    d = cache_dir() / f"ohlcv_{key}_v{CACHE_VERSION}"
    if d.exists() and not rebuild:
        arrs = {f: np.load(d / f"{f}.npy") for f in _FIELDS}
        isins = np.array(json.loads((d / "isins.json").read_text()))
        return MarketData(key=key, isins=isins, **arrs)

    logger.info("Building OHLCV array cache from %s ...", _db_path())
    con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT isin, trade_date, open, high, low, close, volume "
            "FROM ohlcv_adjusted ORDER BY isin, trade_date").fetchall()
    finally:
        con.close()
    isin_col = [r[0] for r in rows]
    dates = np.array([r[1] for r in rows], dtype="datetime64[D]").astype(np.int64)
    num = np.array([r[2:] for r in rows], dtype=np.float64)
    del rows
    isins, starts = np.unique(np.array(isin_col), return_index=True)
    offsets = np.append(starts, len(isin_col)).astype(np.int64)
    md = MarketData(key=key, isins=isins, offsets=offsets, dates=dates,
                    open=np.ascontiguousarray(num[:, 0]), high=np.ascontiguousarray(num[:, 1]),
                    low=np.ascontiguousarray(num[:, 2]), close=np.ascontiguousarray(num[:, 3]),
                    volume=np.ascontiguousarray(num[:, 4]))
    d.mkdir(parents=True, exist_ok=True)
    for f in _FIELDS:
        np.save(d / f"{f}.npy", getattr(md, f))
    (d / "isins.json").write_text(json.dumps(isins.tolist()))
    logger.info("Cached %d rows / %d securities to %s", len(dates), len(isins), d)
    return md


class Universe:
    """In-memory twin of PointInTimeUniverse.get_universe / symbol_at.

    Same rule as the SQL: membership row covers the date, security not quarantined
    on that date, ordered by rank.
    """

    def __init__(self) -> None:
        con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
        try:
            self._members = con.execute(
                "SELECT effective_from, effective_to, isin, rank FROM universe_liquid500 "
                "ORDER BY effective_from, rank").fetchall()
            self._quarantine: dict[str, list[tuple[str, str]]] = {}
            for isin, f, t in con.execute("SELECT isin, from_date, to_date FROM quarantine"):
                self._quarantine.setdefault(isin, []).append((f, t))
            self._symbols: dict[str, list[tuple[str, str, str]]] = {}
            for isin, sym, f, t in con.execute(
                    "SELECT isin, symbol, from_date, to_date FROM symbol_history "
                    "ORDER BY from_date"):
                self._symbols.setdefault(isin, []).append((f, t, sym))
        finally:
            con.close()
        self._by_period: dict[tuple[str, str], list[str]] = {}
        for ef, et, isin, _ in self._members:
            self._by_period.setdefault((ef, et), []).append(isin)
        # Per period, the few members with a quarantine window touching it — only
        # those need a per-day check.
        self._q_in_period: dict[tuple[str, str], set[str]] = {}
        for (ef, et), isins in self._by_period.items():
            self._q_in_period[(ef, et)] = {
                i for i in isins
                if any(f <= et and t >= ef for f, t in self._quarantine.get(i, ()))}
        self._periods = sorted(self._by_period)
        self._cache: dict[str, list[str]] = {}

    def get_universe(self, iso_date: str) -> list[str]:
        hit = self._cache.get(iso_date)
        if hit is not None:
            return hit
        out: list[str] = []
        for per in self._periods:
            ef, et = per
            if ef > iso_date:
                break
            if iso_date > et:
                continue
            risky = self._q_in_period[per]
            if not risky:
                out.extend(self._by_period[per])
                continue
            for isin in self._by_period[per]:
                if isin in risky and any(f <= iso_date <= t for f, t in self._quarantine[isin]):
                    continue
                out.append(isin)
        self._cache[iso_date] = out
        return out

    def symbol_at(self, isin: str, iso_date: str) -> str | None:
        for f, t, sym in self._symbols.get(isin, ()):
            if f <= iso_date <= t:
                return sym
        return None
