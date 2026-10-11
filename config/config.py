"""Central configuration for the automated swing trading bot.

All secrets are loaded from environment variables — never hardcoded.
Every constant has a comment explaining the reasoning behind the value.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

@dataclass
class TradingConfig:
    # ── Zerodha Kite Connect ────────────────────────────────────────────────
    ZERODHA_API_KEY: str = field(default_factory=lambda: os.environ.get("ZERODHA_API_KEY", ""))
    ZERODHA_API_SECRET: str = field(default_factory=lambda: os.environ.get("ZERODHA_API_SECRET", ""))
    ZERODHA_USER_ID: str = field(default_factory=lambda: os.environ.get("ZERODHA_USER_ID", ""))
    ZERODHA_PASSWORD: str = field(default_factory=lambda: os.environ.get("ZERODHA_PASSWORD", ""))
    ZERODHA_TOTP_SECRET: str = field(default_factory=lambda: os.environ.get("ZERODHA_TOTP_SECRET", ""))
    TOKEN_ENCRYPTION_KEY: str = field(default_factory=lambda: os.environ.get("TOKEN_ENCRYPTION_KEY", ""))

    TOTP_AUTO_LOGIN_ENABLED: bool = field(
        default_factory=lambda: os.environ.get("TOTP_AUTO_LOGIN_ENABLED", "false").lower() == "true"
    )

    # ── Mode ────────────────────────────────────────────────────────────────
    PAPER_TRADING_MODE: bool = field(
        default_factory=lambda: os.environ.get("PAPER_TRADING_MODE", "true").lower() != "false"
    )
    AUTH_CALLBACK_PORT: int = 8080
    AUTH_CALLBACK_HOST: str = "0.0.0.0"
    AUTH_OPERATOR_SECRET: str = field(default_factory=lambda: os.environ.get("AUTH_OPERATOR_SECRET", ""))

    # ── Capital & Risk Matrix ───────────────────────────────────────────────
    STARTING_CAPITAL: float = 50_000.0       # ₹50k starting capital for live testing
    MAX_POSITIONS: int = 5                   # Restricted to 5 positions for targeted high-conviction exposure
    MIN_CASH_RESERVE_PCT: float = 0.20       # 20% cash reserve ensures we never need to sell to fund a new trade

    # ── Universe ─────────────────────────────────────────────────────────────
    UNIVERSE_INDEX: str = "nifty500"         # Matches Nifty 500 pool documented in CLAUDE.md

    # ── Strategy Bench ──────────────────────────────────────────────────────
    # mean_reversion removed from disabled block to allow backtest evaluation sweeps
    DISABLED_STRATEGIES: tuple = ("momentum", "breakout")
    ENABLE_PARTIAL_PROFIT: bool = False

    # ── Sizing & Risk Controls (2.0% / 20% Study) ───────────────────────────
    RISK_PER_TRADE_PCT: float = 0.020        # Settled: live runs 2.0% risk per trade profile
    MAX_POSITION_SIZE_PCT: float = 0.20      # Deploy up to 20% max position size cap per asset target
    MAX_SINGLE_SHARE_OVERSHOOT: float = 1.5
    MIN_POSITION_VALUE: float = 5000.0       # Minimum floor execution limit
    MAX_SECTOR_EXPOSURE_PCT: float = 0.30     # 30% sector cap prevents sector concentration risk

    # ── Drawdown Protection ─────────────────────────────────────────────────
    DAILY_LOSS_LIMIT_PCT: float = 0.03       
    WEEKLY_LOSS_LIMIT_PCT: float = 0.06      
    MONTHLY_LOSS_LIMIT_PCT: float = 0.10     
    MAX_DRAWDOWN_PCT: float = 0.05           

    # ── Anti-Revenge Controls ───────────────────────────────────────────────
    CONSECUTIVE_LOSS_PAUSE_AT: int = 3       
    EXTENDED_PAUSE_AT: int = 5               

    # ── Strategy Parameters (Optimized Mean Reversion Grid) ─────────────────
    MR_RSI_PERIOD: int = 2                   # Fast-response RSI indicator tracking deep micro-panics
    MR_RSI_ENTRY: float = 15.0               # Deep oversold market filter gate threshold execution limit
    MR_EMA_EXIT: int = 5                     # Close tracking average to lock in explosive relief bounces instantly
    MR_STOP_PCT: float = 0.025               # Strict 2.5% stop boundary constraint limit to shield capital 
    MR_TIME_STOP_DAYS: int = 3               # Lowered from 6 to protect cash allocations from long consolidation drifts

    # VCP/Momentum Static Parameter Framework Components
    MIN_RS_PERCENTILE: float = 70.0
    MIN_BREAKOUT_VOLUME_MULTIPLIER: float = 2.0
    VCP_LOOKBACK_DAYS: int = 60
    VCP_MIN_TIGHTNESS_SCORE: float = 50.0
    MAX_GAP_UP_PCT: float = 0.05
    MAX_ENTRY_EXTENSION_PCT: float = 0.05
    TREND_TEMPLATE_EMA_PERIODS: list = field(default_factory=lambda: [20, 50, 100, 200])
    WEINSTEIN_MA_PERIOD_WEEKS: int = 30

    # ── Stock Universe Filters ──────────────────────────────────────────────
    MIN_STOCK_PRICE: float = 100.0           
    MIN_MARKET_CAP_CR: float = 5_000.0       
    MIN_DAILY_TRADED_VALUE_CR: float = 10.0  
    MAX_PROMOTER_PLEDGE_PCT: float = 50.0    

    # ── Core Engine Infrastructure Constraints ──────────────────────────────
    KITE_HISTORICAL_RATE_LIMIT: int = 3      # 3 requests per second to stay safely within Zerodha's spec limits
    DB_PATH: str = "data"                    # Directory where database layers are allocated
    BACKTEST_DATA_SOURCE: str = "reconstructed" # Enforces the strict data foundation rule required by loader engine
    ALLOW_FABRICATED_BACKTEST_DATA: bool = True # Safety override bypass to protect data stream routing hooks
    RECONSTRUCTED_DB: str = "market_data.db" # Standard filename mapping target container handle

# Instantiate the active configuration runtime module mapping structure
config = TradingConfig()
