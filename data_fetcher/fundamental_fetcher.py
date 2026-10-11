"""Fetch fundamental data from Screener.in.

Screener.in provides: market cap, promoter pledge %, debt/equity ratio,
face value, total shares. We refresh weekly — fundamentals don't change daily.
All data is cached in fundamentals.db to avoid repeated fetches.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config

logger = logging.getLogger("data.fundamentals")

_SCREENER_URL = "https://www.screener.in/company/{symbol}/"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


def _is_stale(symbol: str) -> bool:
    """Return True if the fundamentals for this symbol need refreshing."""
    db_path = Path(config.DB_PATH) / "fundamentals.db"
    if not db_path.exists():
        return True
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        import pandas as pd
        df = pd.read_sql(
            f"SELECT fetched_at FROM fundamentals WHERE symbol='{symbol.upper()}'",
            engine,
        )
        if df.empty:
            return True
        fetched_at = datetime.fromisoformat(str(df["fetched_at"].iloc[0]))
        age_days = (datetime.now() - fetched_at).days
        return age_days >= config.FUNDAMENTALS_REFRESH_DAYS
    except Exception:
        return True


def fetch_fundamentals(symbol: str) -> Optional[dict]:
    """Scrape fundamental data for a single symbol from Screener.in.

    Returns:
        Dict with keys: symbol, market_cap_cr, promoter_pledge_pct,
        debt_equity_ratio, face_value, total_shares, sector, industry.
        Returns None if fetch fails.
    """
    url = _SCREENER_URL.format(symbol=symbol.upper())
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        return _parse_screener_page(symbol, soup)
    except requests.RequestException as exc:
        logger.error("Failed to fetch Screener.in for %s: %s", symbol, exc)
        return None


def _parse_screener_page(symbol: str, soup: BeautifulSoup) -> Optional[dict]:
    """Extract fundamental metrics from a Screener.in company page."""
    result: dict = {"symbol": symbol.upper()}

    # Market cap — in the top summary numbers section
    for li in soup.select("#top-ratios li"):
        label = li.select_one(".name")
        value = li.select_one(".nowrap .number") or li.select_one(".number")
        if not label or not value:
            continue
        label_text = label.get_text(strip=True).lower()
        value_text = value.get_text(strip=True).replace(",", "")

        if "market cap" in label_text:
            result["market_cap_cr"] = _parse_float(value_text)
        elif "debt" in label_text and "equity" in label_text:
            result["debt_equity_ratio"] = _parse_float(value_text)
        elif "return on equity" in label_text or label_text.rstrip() == "roe":
            result["roe_pct"] = _parse_float(value_text)

    # Promoter pledge — in shareholding table
    pledge_row = soup.find(string=lambda t: t and "pledged" in t.lower() if t else False)
    if pledge_row:
        parent = pledge_row.find_parent("tr")
        if parent:
            cells = parent.find_all("td")
            if len(cells) >= 2:
                result["promoter_pledge_pct"] = _parse_float(cells[-1].get_text(strip=True))

    # Face value & shares outstanding — from company info
    for p in soup.select(".company-info p"):
        text_content = p.get_text(separator=" ", strip=True).lower()
        if "face value" in text_content:
            result["face_value"] = _parse_float_from_text(p.get_text())
        if "shares outstanding" in text_content:
            result["total_shares"] = _parse_int_from_text(p.get_text())

    # Sector from breadcrumb or page
    breadcrumb = soup.select(".breadcrumb a")
    if len(breadcrumb) >= 2:
        result["sector"] = breadcrumb[-1].get_text(strip=True)

    result.setdefault("market_cap_cr", None)
    result.setdefault("promoter_pledge_pct", 0.0)
    result.setdefault("debt_equity_ratio", 0.0)
    result.setdefault("roe_pct", None)
    result.setdefault("face_value", None)
    result.setdefault("total_shares", None)
    result.setdefault("sector", "Unknown")
    result.setdefault("industry", "Unknown")

    return result


def save_fundamentals(data: dict) -> None:
    """Upsert fundamental data into the database."""
    db_path = Path(config.DB_PATH) / "fundamentals.db"
    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT OR REPLACE INTO fundamentals "
                "(symbol, market_cap_cr, promoter_pledge_pct, debt_equity_ratio, roe_pct, "
                "face_value, total_shares, sector, industry, fetched_at) "
                "VALUES (:sym, :mktcap, :pledge, :de, :roe, :fv, :shares, :sector, :ind, :now)"
            ),
            {
                "sym": data["symbol"],
                "mktcap": data.get("market_cap_cr"),
                "pledge": data.get("promoter_pledge_pct", 0.0),
                "de": data.get("debt_equity_ratio", 0.0),
                "roe": data.get("roe_pct"),
                "fv": data.get("face_value"),
                "shares": data.get("total_shares"),
                "sector": data.get("sector", "Unknown"),
                "ind": data.get("industry", "Unknown"),
                "now": now,
            },
        )
    logger.debug("Saved fundamentals for %s", data["symbol"])


def get_fundamentals(symbol: str) -> Optional[dict]:
    """Return cached fundamentals from DB, or fetch if stale.

    Args:
        symbol: NSE trading symbol.

    Returns:
        Dict of fundamental metrics, or None if unavailable.
    """
    import pandas as pd

    db_path = Path(config.DB_PATH) / "fundamentals.db"
    if not _is_stale(symbol) and db_path.exists():
        engine = create_engine(f"sqlite:///{db_path}")
        df = pd.read_sql(
            f"SELECT * FROM fundamentals WHERE symbol='{symbol.upper()}'", engine
        )
        if not df.empty:
            return df.iloc[0].to_dict()

    data = fetch_fundamentals(symbol)
    if data:
        save_fundamentals(data)
        return data

    # If fetch failed, return whatever is cached (even if stale)
    if db_path.exists():
        engine = create_engine(f"sqlite:///{db_path}")
        df = pd.read_sql(
            f"SELECT * FROM fundamentals WHERE symbol='{symbol.upper()}'", engine
        )
        if not df.empty:
            logger.warning("Returning stale fundamentals for %s", symbol)
            return df.iloc[0].to_dict()

    return None


def refresh_all_fundamentals(symbols: list[str]) -> None:
    """Refresh fundamentals for all symbols, respecting rate limits.

    Args:
        symbols: List of NSE trading symbols to refresh.
    """
    stale = [s for s in symbols if _is_stale(s)]
    logger.info("Refreshing fundamentals for %d/%d stale symbols", len(stale), len(symbols))
    for i, symbol in enumerate(stale):
        data = fetch_fundamentals(symbol)
        if data:
            save_fundamentals(data)
        # Be respectful to Screener.in — 1 request per 2 seconds
        time.sleep(2.0)
        if (i + 1) % 20 == 0:
            logger.info("Fundamentals progress: %d/%d", i + 1, len(stale))


def _parse_float(text: str) -> Optional[float]:
    text = text.replace(",", "").replace("₹", "").replace("%", "").strip()
    try:
        return float(text)
    except (ValueError, TypeError):
        return None


def _parse_float_from_text(text: str) -> Optional[float]:
    import re
    match = re.search(r"[\d,]+\.?\d*", text)
    if match:
        return _parse_float(match.group())
    return None


def _parse_int_from_text(text: str) -> Optional[int]:
    import re
    match = re.search(r"[\d,]+", text)
    if match:
        try:
            return int(match.group().replace(",", ""))
        except ValueError:
            return None
    return None
