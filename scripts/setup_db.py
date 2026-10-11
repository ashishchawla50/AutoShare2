"""Initialize all SQLite databases with their schemas.

Run once before first use:
    python scripts/setup_db.py

Safe to re-run — uses CREATE TABLE IF NOT EXISTS throughout.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine

from config.config import config

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DB_BASE = Path(config.DB_PATH)
DB_BASE.mkdir(parents=True, exist_ok=True)


def _engine(db_name: str) -> Engine:
    path = DB_BASE / db_name
    return create_engine(f"sqlite:///{path}", echo=False)


# ── market_data.db ────────────────────────────────────────────────────────────

def setup_market_data_db() -> None:
    engine = _engine("market_data.db")
    meta = MetaData()

    Table(
        "ohlcv_daily",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False),
        Column("date", Date, nullable=False),
        Column("open", Float, nullable=False),
        Column("high", Float, nullable=False),
        Column("low", Float, nullable=False),
        Column("close", Float, nullable=False),
        Column("volume", Integer, nullable=False),
        Column("source", String(20), default="kite"),
        Column("validated", Boolean, default=False),
        Column("updated_at", DateTime),
    )

    Table(
        "ohlcv_data_quality",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False),
        Column("check_date", Date, nullable=False),
        Column("issues", Text),
        Column("passed", Boolean, default=True),
    )

    meta.create_all(engine)

    with engine.connect() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ohlcv_symbol_date "
            "ON ohlcv_daily (symbol, date)"
        ))
        conn.commit()

    logger.info("market_data.db initialized")


# ── fundamentals.db ───────────────────────────────────────────────────────────

def setup_fundamentals_db() -> None:
    engine = _engine("fundamentals.db")
    meta = MetaData()

    Table(
        "fundamentals",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False, unique=True),
        Column("isin", String(15)),
        Column("market_cap_cr", Float),
        Column("promoter_pledge_pct", Float),
        Column("debt_equity_ratio", Float),
        Column("roe_pct", Float),
        Column("face_value", Float),
        Column("total_shares", Integer),
        Column("sector", String(100)),
        Column("industry", String(100)),
        Column("fetched_at", DateTime, nullable=False),
    )

    Table(
        "shareholding_quarterly",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False),
        Column("quarter", String(10), nullable=False),
        Column("promoter_pct", Float),
        Column("fii_pct", Float),
        Column("dii_pct", Float),
        Column("public_pct", Float),
        Column("fetched_at", DateTime),
    )

    meta.create_all(engine)
    logger.info("fundamentals.db initialized")


# ── corporate_events.db ───────────────────────────────────────────────────────

def setup_corporate_events_db() -> None:
    engine = _engine("corporate_events.db")
    meta = MetaData()

    Table(
        "corporate_events",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False),
        Column("event_type", String(30), nullable=False),  # results, dividend, bonus, split
        Column("ex_date", Date),
        Column("record_date", Date),
        Column("announcement_date", Date),
        Column("details", Text),
        Column("fetched_at", DateTime, nullable=False),
    )

    meta.create_all(engine)

    with engine.connect() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_events_symbol_date "
            "ON corporate_events (symbol, ex_date)"
        ))
        conn.commit()

    logger.info("corporate_events.db initialized")


# ── surveillance.db ───────────────────────────────────────────────────────────

def setup_surveillance_db() -> None:
    engine = _engine("surveillance.db")
    meta = MetaData()

    Table(
        "surveillance_list",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False),
        Column("list_type", String(10), nullable=False),  # ASM, ESM, T2T
        Column("stage", String(10)),                       # Stage I, Stage II
        Column("date_added", Date, nullable=False),
        Column("date_removed", Date),
        Column("is_active", Boolean, default=True),
        Column("fetched_at", DateTime, nullable=False),
    )

    meta.create_all(engine)
    logger.info("surveillance.db initialized")


# ── portfolio_state.db ────────────────────────────────────────────────────────

def setup_portfolio_db() -> None:
    engine = _engine("portfolio_state.db")
    meta = MetaData()

    Table(
        "trades",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False),
        Column("entry_date", Date, nullable=False),
        Column("entry_price", Float, nullable=False),
        Column("shares", Integer, nullable=False),
        Column("stop_loss", Float, nullable=False),
        Column("initial_stop_loss", Float, nullable=False),
        Column("pivot_price", Float),
        Column("rs_score", Float),
        Column("vcp_tightness", Float),
        Column("regime_at_entry", String(20)),
        Column("exit_date", Date),
        Column("exit_price", Float),
        Column("exit_reason", String(50)),  # stop_loss, time_stop, profit_target, regime, manual
        Column("realised_pnl", Float),
        Column("realised_pnl_pct", Float),
        Column("is_paper", Boolean, default=True),
        Column("strategy", String(20), default="vcp"),
        Column("gtt_order_id", String(50)),
        Column("notes", Text),
    )

    Table(
        "open_positions",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("symbol", String(20), nullable=False, unique=True),
        Column("entry_date", Date, nullable=False),
        Column("entry_price", Float, nullable=False),
        Column("shares", Integer, nullable=False),
        Column("current_price", Float),
        Column("stop_loss", Float, nullable=False),
        Column("trailing_stop", Float),
        Column("partial_sold_shares", Integer, default=0),
        Column("realised_partial_pnl", Float, default=0.0),
        Column("gtt_order_id", String(50)),
        Column("last_updated", DateTime),
        Column("is_paper", Boolean, default=True),
        Column("strategy", String(20), default="vcp"),   # "vcp" or "momentum"
        Column("target_price", Float, default=0.0),      # fixed target for momentum
    )

    Table(
        "portfolio_metrics",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("date", Date, nullable=False, unique=True),
        Column("portfolio_value", Float),
        Column("cash", Float),
        Column("invested", Float),
        Column("day_pnl", Float),
        Column("total_pnl", Float),
        Column("peak_value", Float),
        Column("drawdown_pct", Float),
        Column("open_positions_count", Integer),
        Column("regime", String(20)),
    )

    Table(
        "loss_tracking",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("date", Date, nullable=False),
        Column("consecutive_losses", Integer, default=0),
        Column("daily_loss_pct", Float, default=0.0),
        Column("weekly_loss_pct", Float, default=0.0),
        Column("monthly_loss_pct", Float, default=0.0),
        Column("pause_until", Date),
        Column("halt_active", Boolean, default=False),
    )

    meta.create_all(engine)
    logger.info("portfolio_state.db initialized")


# ── regime_history.db ─────────────────────────────────────────────────────────

def setup_regime_db() -> None:
    engine = _engine("regime_history.db")
    meta = MetaData()

    Table(
        "regime_log",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("timestamp", DateTime, nullable=False),
        Column("regime", String(30), nullable=False),
        Column("nifty_close", Float),
        Column("nifty_ema200", Float),
        Column("breadth_pct", Float),
        Column("india_vix", Float),
        Column("action_taken", String(100)),
    )

    meta.create_all(engine)
    logger.info("regime_history.db initialized")


def migrate_portfolio_db() -> None:
    """Add momentum columns to existing portfolio_state.db (safe to re-run)."""
    engine = _engine("portfolio_state.db")
    migrations = [
        "ALTER TABLE open_positions ADD COLUMN strategy TEXT DEFAULT 'vcp'",
        "ALTER TABLE open_positions ADD COLUMN target_price REAL DEFAULT 0.0",
        "ALTER TABLE trades ADD COLUMN strategy TEXT DEFAULT 'vcp'",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
                logger.info("Migration applied: %s", sql[:60])
            except Exception:
                pass  # column already exists — safe to ignore


def migrate_fundamentals_db() -> None:
    """Add roe_pct column to existing fundamentals.db (safe to re-run)."""
    engine = _engine("fundamentals.db")
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE fundamentals ADD COLUMN roe_pct REAL"))
            conn.commit()
            logger.info("Migration applied: fundamentals.roe_pct")
        except Exception:
            pass  # column already exists


def setup_all() -> None:
    """Initialize every database. Safe to re-run."""
    logger.info("Initializing all databases in %s/", config.DB_PATH)
    setup_market_data_db()
    setup_fundamentals_db()
    setup_corporate_events_db()
    setup_surveillance_db()
    setup_portfolio_db()
    setup_regime_db()
    migrate_portfolio_db()
    migrate_fundamentals_db()
    logger.info("All databases initialized successfully.")


if __name__ == "__main__":
    setup_all()
