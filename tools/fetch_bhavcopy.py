"""Download and archive NSE daily bhavcopy — LAYER 2 of the three-layer data design.

    LAYER 1  market_data.db.orig-*        original vendor data, never touched
    LAYER 2  bhavcopy_raw/ + bhavcopy.db  THIS MODULE — raw NSE files, never edited
    LAYER 3  reconstructed.db             corrected dataset, regenerable from layer 2

Raw .zip files are stored byte-for-byte as NSE served them and are never modified.
Every download is logged with a SHA-256 so the corrected dataset can be proven to
derive from exactly these bytes.

NSE publishes two incompatible formats:
    legacy  (<= 2024-07-01)  cm{DD}{MON}{YYYY}bhav.csv.zip
    UDiFF   (>= 2024-01-02)  BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
They overlap through H1-2024, so coverage is continuous.

Idempotent and resumable: a date already logged `ok` is skipped, so the job can be
re-run after an interruption without re-fetching or corrupting anything.

Usage:
    python tools/fetch_bhavcopy.py --start 2021-06-01 --end 2026-08-10
    python tools/fetch_bhavcopy.py --start 2021-06-01 --end 2026-08-10 --verify
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import sqlite3
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("tools.bhavcopy")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# Below this date only the legacy format exists; above it, only UDiFF is reliable.
UDIFF_FROM = date(2024, 7, 1)

DDL = """
CREATE TABLE IF NOT EXISTS bhav_daily (
    trade_date  TEXT NOT NULL,
    isin        TEXT,
    symbol      TEXT NOT NULL,
    series      TEXT NOT NULL,
    open        REAL, high REAL, low REAL, close REAL, prevclose REAL,
    qty         REAL, turnover REAL, trades REAL,
    PRIMARY KEY (trade_date, symbol, series)
);
CREATE INDEX IF NOT EXISTS ix_bhav_isin ON bhav_daily(isin, trade_date);
CREATE INDEX IF NOT EXISTS ix_bhav_date ON bhav_daily(trade_date);
CREATE INDEX IF NOT EXISTS ix_bhav_sym  ON bhav_daily(symbol, trade_date);

-- Provenance: every fetch attempt, with the hash of the exact bytes stored.
CREATE TABLE IF NOT EXISTS download_log (
    trade_date  TEXT PRIMARY KEY,
    url         TEXT,
    filename    TEXT,
    fmt         TEXT,
    bytes       INTEGER,
    sha256      TEXT,
    rows        INTEGER,
    status      TEXT NOT NULL,      -- ok | holiday | error
    note        TEXT,
    fetched_at  TEXT
);
"""


def _urls(d: date) -> list[tuple[str, str, str]]:
    """Candidate (format, url, filename) for a date, best-first."""
    ds = d.strftime("%Y%m%d")
    udiff_name = f"BhavCopy_NSE_CM_0_0_0_{ds}_F_0000.csv.zip"
    udiff = ("udiff", f"https://nsearchives.nseindia.com/content/cm/{udiff_name}", udiff_name)
    mon = MONTHS[d.month - 1]
    legacy_name = f"cm{d.strftime('%d')}{mon}{d.year}bhav.csv.zip"
    legacy = ("legacy",
              f"https://nsearchives.nseindia.com/content/historical/EQUITIES/"
              f"{d.year}/{mon}/{legacy_name}", legacy_name)
    return [udiff, legacy] if d >= UDIFF_FROM else [legacy, udiff]


def _parse(fmt: str, blob: bytes) -> list[dict]:
    """Parse a bhavcopy zip into normalised rows. Equities only (STK/EQ-like)."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    out: list[dict] = []
    for r in csv.DictReader(io.StringIO(text)):
        r = { (k or "").strip(): (v or "").strip() for k, v in r.items() }
        try:
            if fmt == "udiff":
                if r.get("FinInstrmTp") != "STK":
                    continue
                out.append({
                    "isin": r.get("ISIN") or None,
                    "symbol": r["TckrSymb"], "series": r.get("SctySrs", ""),
                    "open": r.get("OpnPric"), "high": r.get("HghPric"),
                    "low": r.get("LwPric"), "close": r.get("ClsPric"),
                    "prevclose": r.get("PrvsClsgPric"),
                    "qty": r.get("TtlTradgVol"), "turnover": r.get("TtlTrfVal"),
                    "trades": r.get("TtlNbOfTxsExctd"),
                })
            else:
                out.append({
                    "isin": r.get("ISIN") or None,
                    "symbol": r["SYMBOL"], "series": r.get("SERIES", ""),
                    "open": r.get("OPEN"), "high": r.get("HIGH"),
                    "low": r.get("LOW"), "close": r.get("CLOSE"),
                    "prevclose": r.get("PREVCLOSE"),
                    "qty": r.get("TOTTRDQTY"), "turnover": r.get("TOTTRDVAL"),
                    "trades": r.get("TOTALTRADES"),
                })
        except KeyError:
            continue
    return out


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_range(start: date, end: date, root: Path, delay: float = 1.2) -> None:
    raw_dir = root / "bhavcopy_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "bhavcopy.db")
    con.executescript(DDL)
    con.commit()

    done = {r[0] for r in con.execute("SELECT trade_date FROM download_log WHERE status='ok'")}
    sess = requests.Session()
    sess.headers.update(HEADERS)

    d, n_ok, n_hol, n_err, n_skip = start, 0, 0, 0, 0
    while d <= end:
        if d.weekday() >= 5:                      # weekend — NSE never publishes
            d += timedelta(days=1); continue
        key = d.isoformat()
        if key in done:
            n_skip += 1; d += timedelta(days=1); continue

        got = False
        for fmt, url, name in _urls(d):
            try:
                r = sess.get(url, timeout=40)
            except Exception as exc:
                logger.warning("%s %s: %s", key, fmt, type(exc).__name__)
                continue
            if r.status_code != 200 or len(r.content) < 500:
                continue

            blob = r.content
            sha = hashlib.sha256(blob).hexdigest()
            ydir = raw_dir / str(d.year)
            ydir.mkdir(exist_ok=True)
            (ydir / name).write_bytes(blob)        # LAYER 2 — never modified again

            try:
                rows = _parse(fmt, blob)
            except Exception as exc:
                con.execute(
                    "INSERT OR REPLACE INTO download_log VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                    (key, url, name, fmt, len(blob), sha, 0, "error", f"parse: {exc}"))
                con.commit(); n_err += 1; got = True; break

            con.executemany(
                "INSERT OR REPLACE INTO bhav_daily "
                "(trade_date,isin,symbol,series,open,high,low,close,prevclose,qty,turnover,trades) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(key, x["isin"], x["symbol"], x["series"],
                  _num(x["open"]), _num(x["high"]), _num(x["low"]), _num(x["close"]),
                  _num(x["prevclose"]), _num(x["qty"]), _num(x["turnover"]), _num(x["trades"]))
                 for x in rows])
            con.execute(
                "INSERT OR REPLACE INTO download_log VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                (key, url, name, fmt, len(blob), sha, len(rows), "ok", None))
            con.commit()
            n_ok += 1; got = True
            if n_ok % 25 == 0:
                logger.info("… %s  ok=%d holiday=%d err=%d", key, n_ok, n_hol, n_err)
            break

        if not got:
            # Both formats 404 -> NSE holiday. Recorded explicitly, never silently
            # skipped: a silent gap would distort rolling turnover medians.
            con.execute(
                "INSERT OR REPLACE INTO download_log VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                (key, None, None, None, 0, None, 0, "holiday", "no file in either format"))
            con.commit(); n_hol += 1
        time.sleep(delay)
        d += timedelta(days=1)

    logger.warning("DONE  ok=%d  holiday=%d  error=%d  skipped(existing)=%d",
                   n_ok, n_hol, n_err, n_skip)
    con.close()


def verify(root: Path) -> int:
    """Re-hash every archived file against download_log. Returns mismatch count."""
    con = sqlite3.connect(root / "bhavcopy.db")
    bad = 0; checked = 0
    for td, name, sha in con.execute(
            "SELECT trade_date, filename, sha256 FROM download_log WHERE status='ok'"):
        p = root / "bhavcopy_raw" / td[:4] / name
        if not p.exists():
            print(f"  MISSING {p}"); bad += 1; continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            print(f"  HASH MISMATCH {p}"); bad += 1; continue
        checked += 1
    print(f"verified {checked} archived files, {bad} problems")
    con.close()
    return bad


def rebuild_from_archive(root: Path) -> None:
    """Rebuild bhavcopy.db purely from the retained raw .zip files — no network.

    This is what makes layer 3 provably regenerable by anyone holding the archive:
    the corrected dataset never depends on NSE still serving these URLs.
    """
    raw_dir = root / "bhavcopy_raw"
    files = sorted(raw_dir.glob("*/*.zip"))
    if not files:
        raise FileNotFoundError(f"no archived bhavcopy files under {raw_dir}")
    con = sqlite3.connect(root / "bhavcopy.db")
    con.executescript(DDL)
    con.execute("DELETE FROM bhav_daily")
    con.execute("DELETE FROM download_log")
    con.commit()

    n_ok = 0
    for p in files:
        blob = p.read_bytes()
        fmt = "udiff" if p.name.startswith("BhavCopy") else "legacy"
        rows = _parse(fmt, blob)
        if not rows:
            continue
        # Trade date comes from the filename, matching how it was archived.
        if fmt == "udiff":
            ds = p.name.split("_")[6]
            key = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
        else:
            stem = p.name[2:11]           # DDMONYYYY
            key = (f"{stem[5:]}-{MONTHS.index(stem[2:5].upper())+1:02d}-{stem[:2]}")
        con.executemany(
            "INSERT OR REPLACE INTO bhav_daily "
            "(trade_date,isin,symbol,series,open,high,low,close,prevclose,qty,turnover,trades) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(key, x["isin"], x["symbol"], x["series"],
              _num(x["open"]), _num(x["high"]), _num(x["low"]), _num(x["close"]),
              _num(x["prevclose"]), _num(x["qty"]), _num(x["turnover"]), _num(x["trades"]))
             for x in rows])
        con.execute(
            "INSERT OR REPLACE INTO download_log VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            (key, "(from local archive)", p.name, fmt, len(blob),
             hashlib.sha256(blob).hexdigest(), len(rows), "ok", "offline rebuild"))
        n_ok += 1
        if n_ok % 200 == 0:
            con.commit()
            logger.info("  rebuilt %d/%d files", n_ok, len(files))
    con.commit()
    logger.warning("offline rebuild complete: %d files -> %d rows", n_ok,
                   con.execute("SELECT COUNT(*) FROM bhav_daily").fetchone()[0])
    con.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    ap = argparse.ArgumentParser(description="Archive NSE daily bhavcopy (layer 2)")
    ap.add_argument("--rebuild-from-archive", action="store_true",
                    help="rebuild bhavcopy.db from retained .zip files, no network")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--root", default="data")
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument("--verify", action="store_true", help="re-hash archive and exit")
    a = ap.parse_args()
    root = Path(a.root)
    if a.rebuild_from_archive:
        rebuild_from_archive(root)
        sys.exit(0)
    if a.verify:
        sys.exit(1 if verify(root) else 0)
    fetch_range(date.fromisoformat(a.start), date.fromisoformat(a.end), root, a.delay)
