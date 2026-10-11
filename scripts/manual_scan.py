#!/usr/bin/env python3
"""Run the morning scan jobs manually outside the scheduled window.

Usage:
    python scripts/manual_scan.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger import setup_logging
setup_logging()

import logging
logger = logging.getLogger("manual_scan")

from main import TradingBot

bot = TradingBot()

logger.info("Connecting to Kite (loading saved token)...")
bot.startup()

if not bot._kite.is_connected:
    logger.error("Kite not connected — no saved token. Run TOTP login first.")
    sys.exit(1)

logger.info("Refreshing Nifty 200 universe snapshot from NSE...")
bot.refresh_universe_snapshot()

logger.info("Refreshing universe OHLCV (this takes a few minutes)...")
bot.refresh_universe_ohlcv()
logger.info("OHLCV loaded: %d stocks", len(bot._universe_ohlcv))

if not bot._universe_ohlcv:
    logger.error("No OHLCV data — universe may be empty. Check fundamentals.db.")
    sys.exit(1)

logger.info("Checking market regime...")
bot.check_regime()
if bot._regime_state:
    logger.info(
        "Regime: %s | Allow new trades: %s | Position size multiplier: %.1f",
        bot._regime_state.regime.value,
        bot._regime_state.allow_new_trades,
        bot._regime_state.position_size_multiplier,
    )

logger.info("Running pre-scan filters...")
bot.run_pre_scan()
logger.info("After filters: %d stocks", len(bot._universe_ohlcv))

logger.info("Running strategy scan (VCP + trend template)...")
bot.run_strategy_scan()

logger.info("=" * 50)
logger.info("SCAN RESULTS: %d setups found", len(bot._watchlist))
logger.info("=" * 50)
if bot._watchlist:
    for s in bot._watchlist[:15]:
        logger.info(
            "  %-15s  RS=%5.1f  VCP=%5.1f  pivot=%8.2f  stop=%8.2f",
            s.symbol,
            s.rs_percentile,
            s.vcp_tightness_score,
            s.pivot_price,
            s.stop_loss_price,
        )
else:
    logger.info("  No setups today.")
