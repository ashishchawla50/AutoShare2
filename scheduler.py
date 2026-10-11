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

from alerts import goldbees_ladder
from config.config import config
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

    # Job id → what it runs. WHEN it runs lives in config.SCHEDULE (IST crontab).
    actions = {
        # Pre-market data refresh
        "universe_snapshot_refresh": lambda: bot.refresh_universe_snapshot(),
        "fundamentals_refresh":      lambda: bot.refresh_fundamentals(),
        "surveillance_refresh":      lambda: bot.refresh_surveillance(),
        "corporate_events_refresh":  lambda: bot.refresh_corporate_events(),
        "auth_login_prompt":         lambda: bot.send_login_url(),
        # Reconcile DB positions vs Kite holdings daily (live mode only):
        # catches GTT fired offline and manual sells, cancels dangling GTTs
        "holdings_reconcile":        lambda: bot.reconcile_holdings(),
        "universe_ohlcv_refresh":    lambda: bot.refresh_universe_ohlcv(),
        "pre_scan_filters":          lambda: bot.run_pre_scan(),
        "strategy_scan":             lambda: bot.run_strategy_scan(),
        "momentum_scan":             lambda: bot.run_momentum_scan(),
        "breakout_scan":             lambda: bot.run_breakout_scan(),
        "mean_reversion_scan":       lambda: bot.run_mean_reversion_scan(),
        "regime_check_premarket":    lambda: bot.check_regime(),
        "rank_opportunities":        lambda: bot.rank_opportunities(),
        "gtt_verify":                lambda: bot.verify_gtt_orders(),
        # GTT backup monitor during market hours (safety net)
        "gtt_backup":                lambda: bot._gtt_monitor.run_once(),
        # Market hours
        "exit_check":                lambda: bot.check_exits(),
        "entry_window":              lambda: bot.check_entries(),
        "regime_intraday":           lambda: bot.check_regime(),
        "heartbeat":                 lambda: bot.send_heartbeat(),
        # End of day
        "market_close_cleanup":      lambda: bot.market_close(),
        "eod_report":                lambda: bot.generate_eod_report(),
        "database_backup":           lambda: bot.backup_databases(),
        # Weekly report
        "weekly_report":             lambda: bot.generate_weekly_report(),
        # GOLDBEES lot-ladder alert (alert-only, never places orders)
        "goldbees_ladder_sync":      lambda: goldbees_ladder.run_sync(bot._kite),
        "goldbees_ladder_check":     lambda: goldbees_ladder.run_check(bot._kite),
    }
    missing = set(actions) ^ set(config.SCHEDULE)
    if missing:
        raise ValueError(f"config.SCHEDULE and scheduler actions disagree on jobs: {sorted(missing)}")
    jobs = [(job_id, config.SCHEDULE[job_id], func) for job_id, func in actions.items()]

    for job_id, cron, func in jobs:
        wrapped = _trading_day_only(func) if "report" not in job_id and "backup" not in job_id else func
        scheduler.add_job(
            wrapped,
            trigger=CronTrigger.from_crontab(cron, timezone=IST),
            id=job_id,
            name=job_id,
            misfire_grace_time=config.SCHEDULER_MISFIRE_GRACE_SECONDS,
            coalesce=True,
            max_instances=1,
        )
        logger.debug("Scheduled: %s (%s)", job_id, cron)

    logger.info("Scheduler configured with %d jobs", len(jobs))
    return scheduler
