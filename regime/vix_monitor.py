"""India VIX monitoring and volatility regime classification.

India VIX is fetched from NSE's API. It measures expected 30-day volatility
of Nifty 50 options — the higher it goes, the more fear in the market.

VIX thresholds (empirically observed for Indian markets):
  <20: Calm, normal trading
  20–25: Caution — reduce new position sizes
  25–35: High volatility — halve new position sizes
  >35: Extreme — no new trades, begin staged exit
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import requests

from config.config import config

logger = logging.getLogger("regime.vix")

_NSE_VIX_URL = "https://www.nseindia.com/api/equity-stockIndices?index=INDIA%20VIX"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

# Cached last known VIX (in-memory only — re-fetched every 30 min during market hours)
_last_known_vix: Optional[float] = None
_last_fetch_time: float = 0.0
_VIX_CACHE_SECONDS = config.VIX_CACHE_SECONDS


def get_india_vix(force_refresh: bool = False) -> Optional[float]:
    """Return the current India VIX value.

    Caches for 5 minutes to avoid hammering NSE. Returns the last known
    value on fetch failure (better than returning None and blocking trades).

    Args:
        force_refresh: Bypass cache and fetch immediately.

    Returns:
        VIX float, or None if never successfully fetched.
    """
    global _last_known_vix, _last_fetch_time

    age = time.monotonic() - _last_fetch_time
    if not force_refresh and _last_known_vix is not None and age < _VIX_CACHE_SECONDS:
        return _last_known_vix

    vix = _fetch_vix_from_nse()
    if vix is not None:
        _last_known_vix = vix
        _last_fetch_time = time.monotonic()
        logger.debug("India VIX: %.2f", vix)
    elif _last_known_vix is not None:
        logger.warning("VIX fetch failed — using last known value %.2f", _last_known_vix)

    return _last_known_vix


def update_vix_from_kite(vix_value: float) -> None:
    """Update VIX cache with a value fetched from Kite quotes.

    Call this from main.py before regime detection when NSE API is unavailable.
    """
    global _last_known_vix, _last_fetch_time
    _last_known_vix = vix_value
    _last_fetch_time = time.monotonic()
    logger.debug("India VIX updated from Kite: %.2f", vix_value)


def _fetch_vix_from_nse() -> Optional[float]:
    """Fetch India VIX from NSE JSON API."""
    session = requests.Session()
    session.headers.update(_HEADERS)
    try:
        session.get("https://www.nseindia.com/", timeout=config.NSE_HTTP_TIMEOUT_SECONDS)
        resp = session.get(_NSE_VIX_URL, timeout=config.NSE_HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        # NSE returns index data as list; VIX is in "data" key
        for item in data.get("data", []):
            if item.get("index", "").upper() == "INDIA VIX":
                return float(item["last"])
        # Alternative: direct "last" field for VIX index
        last = data.get("metadata", {}).get("lastPrice")
        if last:
            return float(last)
    except Exception as exc:
        logger.error("VIX fetch failed: %s", exc)
    return None


def classify_vix_regime(vix: Optional[float]) -> str:
    """Return a regime label based on VIX level.

    Args:
        vix: India VIX value.

    Returns:
        One of: "CALM", "CAUTION", "HIGH_VOLATILITY", "EXTREME_VOLATILITY", "UNKNOWN"
    """
    if vix is None:
        return "UNKNOWN"
    if vix < config.VIX_CAUTION_LEVEL:
        return "CALM"
    if vix < config.VIX_REDUCE_LEVEL:
        return "CAUTION"
    if vix < config.VIX_HALT_LEVEL:
        return "HIGH_VOLATILITY"
    return "EXTREME_VOLATILITY"


def should_halt_new_trades(vix: Optional[float]) -> bool:
    """Return True if VIX level requires halting all new entries."""
    return vix is not None and vix >= config.VIX_HALT_LEVEL


def position_size_multiplier(vix: Optional[float]) -> float:
    """Return the position size multiplier based on VIX.

    Returns:
        1.0 (full size), 0.5 (half size), or 0.0 (no new trades).
    """
    if vix is None or vix < config.VIX_CAUTION_LEVEL:
        return 1.0
    if vix < config.VIX_REDUCE_LEVEL:
        return config.VIX_CAUTION_SIZE_MULT   # Cautious mode
    if vix < config.VIX_HALT_LEVEL:
        return config.VIX_HIGH_SIZE_MULT      # High volatility
    return 0.0       # Extreme — no new positions
