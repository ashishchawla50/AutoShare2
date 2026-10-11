"""Sector regime filter — penalise stocks from weak sectors in the ranker.

A stock breaking out in a weak sector has significantly lower success probability
than one breaking out in a strong sector. This filter adds a -10 penalty to the
composite score for stocks whose sector is in a downtrend.

Sector health is determined by the % of stocks in that sector that are:
  - Above their own 50-day EMA  (short-term trend)
  - Making a higher close than 20 days ago (momentum)

If < 40% of a sector's stocks meet both criteria → sector is WEAK → -10 penalty.
If 40–60% → sector is NEUTRAL → no penalty.
If > 60% → sector is STRONG → no penalty (Weinstein bonus handles upside already).

NSE sector mapping uses Nifty 200 constituents grouped by their standard
sector classification. Unknown sectors default to NEUTRAL.
"""

from __future__ import annotations

import logging
from enum import Enum

import pandas as pd

from config.config import config

logger = logging.getLogger("scanner.sector_regime")


class SectorStrength(str, Enum):
    STRONG = "STRONG"
    NEUTRAL = "NEUTRAL"
    WEAK = "WEAK"


# NSE sector groupings — standard NSE sector classifications
NSE_SECTOR_GROUPS: dict[str, list[str]] = {
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
                "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "PNB", "BANKBARODA", "CANBK"],
    "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS", "PERSISTENT",
           "COFORGE", "LTTS"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "BIOCON", "TORNTPHARM",
               "ALKEM", "LUPIN", "AUROPHARMA", "GLENMARK"],
    "AUTO": ["MARUTI", "M&M", "TATAMOTORS", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO",
             "TVSMOTOR", "ASHOKLEY", "MRF", "BALKRISIND"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO",
             "GODREJCP", "COLPAL", "EMAMILTD", "VBL"],
    "METALS": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "SAIL", "NMDC",
               "COALINDIA", "HINDZINC", "NATIONALUM", "MOIL"],
    "ENERGY": ["RELIANCE", "ONGC", "BPCL", "IOC", "POWERGRID", "NTPC",
               "ADANIPORTS", "ADANIGREEN", "TATAPOWER", "TORNTPOWER"],
    "REALTY": ["DLF", "GODREJPROP", "OBEROIRLTY", "PHOENIXLTD", "PRESTIGE", "BRIGADE"],
    "NBFC": ["BAJFINANCE", "BAJAJFINSV", "CHOLAMANDLAM", "MUTHOOTFIN", "L&TFH", "SHRIRAMFIN"],
    "CONSUMER": ["TITAN", "PIDILITIND", "PAGEIND", "HAVELLS", "WHIRLPOOL", "VOLTAS",
                 "CROMPTON", "POLYCAB", "VGUARD"],
}

# Reverse map: symbol → sector
_SYMBOL_TO_SECTOR: dict[str, str] = {
    sym: sector
    for sector, symbols in NSE_SECTOR_GROUPS.items()
    for sym in symbols
}


def get_symbol_sector(symbol: str) -> str:
    """Return the sector for a given NSE symbol, or 'OTHER' if unknown."""
    return _SYMBOL_TO_SECTOR.get(symbol.upper(), "OTHER")


def calculate_sector_strengths(
    universe_ohlcv: dict[str, pd.DataFrame],
) -> dict[str, SectorStrength]:
    """Compute sector strength for all known sectors based on universe OHLCV.

    Args:
        universe_ohlcv: Dict mapping symbol → daily OHLCV DataFrame.

    Returns:
        Dict mapping sector name → SectorStrength enum.
    """
    sector_scores: dict[str, list[bool]] = {s: [] for s in NSE_SECTOR_GROUPS}

    for symbol, df in universe_ohlcv.items():
        sector = get_symbol_sector(symbol)
        if sector == "OTHER" or sector not in sector_scores:
            continue
        if len(df) < config.SECTOR_MIN_HISTORY:
            continue

        close = df["close"]
        ema50 = close.ewm(span=config.SECTOR_EMA_DAYS, adjust=False).mean()
        current_close = float(close.iloc[-1])
        td = config.SECTOR_TREND_DAYS
        close_20d_ago = float(close.iloc[-td]) if len(close) >= td else current_close

        above_ema50 = current_close > float(ema50.iloc[-1])
        higher_than_20d = current_close > close_20d_ago

        sector_scores[sector].append(above_ema50 and higher_than_20d)

    results: dict[str, SectorStrength] = {}
    for sector, votes in sector_scores.items():
        if not votes:
            results[sector] = SectorStrength.NEUTRAL
            continue
        pct_healthy = sum(votes) / len(votes)
        if pct_healthy >= config.SECTOR_STRONG_AT:
            results[sector] = SectorStrength.STRONG
        elif pct_healthy >= config.SECTOR_NEUTRAL_AT:
            results[sector] = SectorStrength.NEUTRAL
        else:
            results[sector] = SectorStrength.WEAK

        logger.debug(
            "Sector %s: %.0f%% healthy → %s",
            sector, pct_healthy * 100, results[sector].value,
        )

    return results


def get_sector_penalty(sector: str, strengths: dict[str, SectorStrength]) -> float:
    """Return the score penalty for a stock based on its sector regime.

    Args:
        sector:    The stock's sector (from get_symbol_sector).
        strengths: Pre-computed sector strength dict.

    Returns:
        0.0 for STRONG/NEUTRAL sectors, -10.0 for WEAK sectors.
    """
    strength = strengths.get(sector, SectorStrength.NEUTRAL)
    if strength == SectorStrength.WEAK:
        return config.SECTOR_WEAK_PENALTY
    return 0.0
