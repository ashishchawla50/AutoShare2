"""APScheduler job definitions for the daily trading workflow.

All jobs are defined here and registered in main.py on startup.
Times are in IST (Asia/Kolkata timezone).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config.holidays import is_trading_day
from utils.time_utils import today_ist

logger = logging.getLogger("trading.scheduler")
IST = pytz.timezone("Asia/Kolkata")


def _trading_day_only(func: Callable) -> Callable:
    """Wrapper: skip the job if today is not a trading day."""
    def wrapper(*args, **kwargs):
        if not is_trading_day(today_ist()):
            logger.debug("Holiday/weekend — skipping %s", func.__name__)
            return
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper


def build_scheduler(bot) -> BackgroundScheduler:
    """Create and return the fully configured APScheduler instance.

    Args:
        bot: The TradingBot instance (jobs call methods on this).

    Returns:
        A configured (but not yet started) BackgroundScheduler.
    """
    scheduler = BackgroundScheduler(timezone=IST)

    jobs = [
        # Pre-market data refresh
        ("universe_snapshot_refresh","30 5 * * *", lambda: bot.refresh_universe_snapshot()),
        ("fundamentals_refresh",    "0 6 * * *",  lambda: bot.refresh_fundamentals()),
        ("surveillance_refresh",    "0 7 * * *",  lambda: bot.refresh_surveillance()),
        ("corporate_events_refresh","30 7 * * *", lambda: bot.refresh_corporate_events()),
        ("auth_login_prompt",       "0 8 * * *",  lambda: bot.send_login_url()),
        # Reconcile DB positions vs Kite holdings daily (live mode only):
        # catches GTT fired offline and manual sells, cancels dangling GTTs
        ("holdings_reconcile",      "20 8 * * *", lambda: bot.reconcile_holdings()),
        ("universe_ohlcv_refresh",  "30 8 * * *", lambda: bot.refresh_universe_ohlcv()),
        ("pre_scan_filters",        "45 8 * * *", lambda: bot.run_pre_scan()),
        ("strategy_scan",           "0 9 * * *",  lambda: bot.run_strategy_scan()),
        ("momentum_scan",           "5 9 * * *",  lambda: bot.run_momentum_scan()),
        ("breakout_scan",           "7 9 * * *",  lambda: bot.run_breakout_scan()),
        ("mean_reversion_scan",     "8 9 * * *",  lambda: bot.run_mean_reversion_scan()),
        ("regime_check_premarket",  "10 9 * * *", lambda: bot.check_regime()),
        ("rank_opportunities",      "15 9 * * *", lambda: bot.rank_opportunities()),
        ("gtt_verify",              "20 9 * * *", lambda: bot.verify_gtt_orders()),

        # GTT backup monitor — every 60 seconds during market hours (safety net)
        ("gtt_backup",              "* 9-15 * * *",    lambda: bot._gtt_monitor.run_once()),

        # Market hours — every 5 minutes, 9:30–15:30
        ("exit_check",              "*/5 9-15 * * *",  lambda: bot.check_exits()),
        ("entry_window",            "*/5 9-10 * * *",  lambda: bot.check_entries()),

        # Regime re-check every 30 minutes
        ("regime_intraday",         "*/30 9-15 * * *", lambda: bot.check_regime()),

        # Heartbeat every 30 minutes during market hours
        ("heartbeat",               "*/30 9-15 * * *", lambda: bot.send_heartbeat()),

        # End of day
        ("market_close_cleanup",    "30 15 * * *", lambda: bot.market_close()),
        ("eod_report",              "45 15 * * *", lambda: bot.generate_eod_report()),
        ("database_backup",         "30 16 * * *", lambda: bot.backup_databases()),

        # Weekly report (Monday 7 AM)
        ("weekly_report",           "0 7 * * 1",   lambda: bot.generate_weekly_report()),
    ]

    for job_id, cron, func in jobs:
        wrapped = _trading_day_only(func) if "report" not in job_id and "backup" not in job_id else func
        scheduler.add_job(
            wrapped,
            trigger=CronTrigger.from_crontab(cron, timezone=IST),
            id=job_id,
            name=job_id,
            misfire_grace_time=300,
            coalesce=True,
            max_instances=1,
        )
        logger.debug("Scheduled: %s (%s)", job_id, cron)

    logger.info("Scheduler configured with %d jobs", len(jobs))
    return scheduler
