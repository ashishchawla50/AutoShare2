"""Persistent pivot watchlist — the live half of the two-stage VCP entry.

Validated config (2026-07-16): a valid tight consolidation does NOT enter
directly; it stores its pivot here, and the entry fires only when a later
candle breaks out through the stored pivot with volume (dev PF 1.11 /
out-of-sample PF 1.61, edge confirmed). Mirrors backtest_engine's vcp_v2
path; keep the two in sync.

State survives restarts via JSON in the data directory — same pattern as
the regime upgrade state.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from config.config import config

logger = logging.getLogger("scanner.pivot_watch")

_WATCH_FILENAME = ".pivot_watchlist.json"


def _watch_path() -> Path:
    return Path(config.DB_PATH) / _WATCH_FILENAME


def load_watch() -> dict[str, dict]:
    """Load the pivot watchlist from disk. Missing/corrupt file → empty."""
    path = _watch_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.error("Failed to load pivot watchlist: %s — starting empty", exc)
        return {}


def save_watch(watch: dict[str, dict]) -> None:
    """Persist the pivot watchlist to disk."""
    try:
        with open(_watch_path(), "w", encoding="utf-8") as fh:
            json.dump(watch, fh, indent=2)
    except Exception as exc:
        logger.error("Failed to save pivot watchlist: %s", exc)


def add_symbol(
    watch: dict[str, dict],
    symbol: str,
    pivot: float,
    vcp_low: float,
    rs_percentile: float,
    tightness: float,
    today: date,
) -> bool:
    """Add a symbol to the watch if not already present.

    Returns:
        True if added, False if the symbol was already being watched.
    """
    key = symbol.upper()
    if key in watch:
        return False
    watch[key] = {
        "pivot": float(pivot),
        "vcp_low": float(vcp_low) if vcp_low else 0.0,
        "rs_percentile": float(rs_percentile),
        "tightness": float(tightness),
        "added": today.isoformat(),
        "expires": (today + timedelta(days=config.VCP2_PIVOT_WATCH_DAYS)).isoformat(),
    }
    return True


def purge_expired(watch: dict[str, dict], today: date) -> int:
    """Drop watches past their expiry date. Returns the number removed."""
    expired = [
        s for s, w in watch.items()
        if date.fromisoformat(w.get("expires", "1970-01-01")) < today
    ]
    for s in expired:
        del watch[s]
    if expired:
        logger.info("Pivot watch expired without breakout: %s", ", ".join(sorted(expired)))
    return len(expired)
