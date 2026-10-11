"""Central configuration for the automated swing trading bot.

All secrets are loaded from environment variables — never hardcoded.
Every constant has a comment explaining the reasoning behind the value.
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # ── Zerodha Kite Connect ────────────────────────────────────────────────
    ZERODHA_API_KEY: str = field(default_factory=lambda: os.environ.get("ZERODHA_API_KEY", ""))
    ZERODHA_API_SECRET: str = field(default_factory=lambda: os.environ.get("ZERODHA_API_SECRET", ""))
    ZERODHA_USER_ID: str = field(default_factory=lambda: os.environ.get("ZERODHA_USER_ID", ""))
    ZERODHA_PASSWORD: str = field(default_factory=lambda: os.environ.get("ZERODHA_PASSWORD", ""))
    # WARNING: storing TOTP secret enables fully automated login.
    # Zerodha ToS may prohibit this. Use at your own risk.
    ZERODHA_TOTP_SECRET: str = field(default_factory=lambda: os.environ.get("ZERODHA_TOTP_SECRET", ""))
    TOKEN_ENCRYPTION_KEY: str = field(default_factory=lambda: os.environ.get("TOKEN_ENCRYPTION_KEY", ""))

    # Explicit opt-in for TOTP automation. Zerodha's internal login endpoints
    # (/api/login, /api/twofa) are not part of the official Kite Connect spec.
    # Scripting them may trigger abuse detection and risk API key revocation.
    # Set TOTP_AUTO_LOGIN_ENABLED=True in .env only after accepting this risk.
    # When False (default), the bot sends a manual login URL via Telegram at 8 AM.
    TOTP_AUTO_LOGIN_ENABLED: bool = field(
        default_factory=lambda: os.environ.get("TOTP_AUTO_LOGIN_ENABLED", "false").lower() == "true"
    )

    # ── Mode ────────────────────────────────────────────────────────────────
    # Set PAPER_TRADING_MODE=false in .env to go live. Default is True (safe).
    PAPER_TRADING_MODE: bool = field(
        default_factory=lambda: os.environ.get("PAPER_TRADING_MODE", "true").lower() != "false"
    )
    AUTH_CALLBACK_PORT: int = 8080
    # Flask auth server hostname — use 0.0.0.0 for VPS, 127.0.0.1 for local
    # (must stay publicly reachable: the Zerodha OAuth redirect arrives via
    # the operator's browser, which can be on any network)
    AUTH_CALLBACK_HOST: str = "0.0.0.0"
    # Shared secret required (X-Operator-Secret header) for state-changing
    # auth endpoints like /logout. Empty = endpoint disabled entirely, since
    # port 8080 is internet-facing and /logout would otherwise let anyone
    # deauthenticate the bot mid-session.
    AUTH_OPERATOR_SECRET: str = field(
        default_factory=lambda: os.environ.get("AUTH_OPERATOR_SECRET", "")
    )

    # ── Capital ─────────────────────────────────────────────────────────────
    STARTING_CAPITAL: float = 50_000.0       # ₹50k starting capital for live testing
    MAX_POSITIONS: int = 8                   # 8 concurrent positions for higher daily trade frequency
    # 20% cash reserve ensures we never need to sell to fund a new trade
    MIN_CASH_RESERVE_PCT: float = 0.20

    # ── Universe ─────────────────────────────────────────────────────────────
    # Widened from nifty200 on 2026-07-14: quality gates stay unchanged, the
    # pond gets bigger. VCP-class setups live disproportionately in midcaps;
    # 200 large caps produced too few setups to compound or validate.
    UNIVERSE_INDEX: str = "nifty500"    # "nifty200" or "nifty500"

    # ── Strategy bench ──────────────────────────────────────────────────────
    # Strategies whose edge was statistically rejected by the 5-yr gated
    # backtest (2026-07-14: momentum PF 0.69, breakout PF 0.69,
    # mean_reversion PF 0.61 — all bootstrap-rejected). Benched strategies
    # take no new entries; exits keep managing any open positions. Remove a
    # strategy from this set ONLY after a revised variant passes both the
    # dev (2021-2024) and untouched validation (2025-2026) backtest windows.
    DISABLED_STRATEGIES: tuple = ("momentum", "breakout", "mean_reversion")

    # Exit geometry harmonization: False = positions ride 100% to the trailing
    # stop ratchet (matches the validated backtest engine PF 1.75).
    # True = sell 33% at +20% and 33% at +30% profit targets.
    ENABLE_PARTIAL_PROFIT: bool = False

    # ── Risk per trade ──────────────────────────────────────────────────────
    # 2.0%/20% chosen by the 2026-07-17 sizing study: doubles CAGR vs
    # 1.5%/15% (dev +7.5% vs +2.9%) for ~0.5pp more drawdown, and PF
    # improves with size (fixed costs shrink proportionally). 2.5%/25% is
    # DISQUALIFIED — it breached the 20% drawdown halt in the dev window
    # (Apr 2023). Do not raise these without rerunning both study windows.
    RISK_PER_TRADE_PCT: float = 0.020
    MAX_POSITION_SIZE_PCT: float = 0.20
    # On small capital a single share of an expensive stock can far exceed the
    # intended position value (₹4,500 share vs ₹2,000 target = 2.3x exposure).
    # Allow the 1-share fallback only up to this multiple of the size target.
    MAX_SINGLE_SHARE_OVERSHOOT: float = 1.5
    # Minimum position value — shared by backtest AND live (scaled by the
    # regime multiplier in both). ₹6,000 is where the real CNC round-trip
    # cost crosses ~0.5% of position (₹15.93 fixed DP charge + 0.222%
    # variable — see DUST_FLOOR_DECISION.md, 2026-08-07). The old ₹30k
    # backtest-only floor was ~5x above any cost-justified level and made a
    # ₹50k backtest structurally unable to trade (20% cap = ₹10k positions),
    # while live had no floor at all.
    MIN_POSITION_VALUE: float = 6_000.0
    # 30% sector cap prevents sector concentration risk
    MAX_SECTOR_EXPOSURE_PCT: float = 0.30

    # ── Drawdown protection ─────────────────────────────────────────────────
    # 3% daily limit = roughly 2 max-loss trades in one day
    DAILY_LOSS_LIMIT_PCT: float = 0.03
    # 6% weekly limit = two bad days before forced reflection
    WEEKLY_LOSS_LIMIT_PCT: float = 0.06
    # 10% monthly limit = strategy review trigger
    MONTHLY_LOSS_LIMIT_PCT: float = 0.10
    # 20% from peak = emergency halt; drawdowns beyond this are hard to recover from
    MAX_DRAWDOWN_PCT: float = 0.20

    # ── Anti-revenge trading (Mark Douglas) ─────────────────────────────────
    # 3 consecutive losses = pause 1 day; psychological reset
    CONSECUTIVE_LOSS_PAUSE_AT: int = 3
    # 5 consecutive losses = 3-day pause + mandatory review
    EXTENDED_PAUSE_AT: int = 5

    # ── Strategy parameters ─────────────────────────────────────────────────
    # Top 30% RS stocks significantly outperform; 85th+ = true market leaders
    MIN_RS_PERCENTILE: float = 70.0
    # Breakout-day volume vs 50-day average (institutional participation).
    # 1.5x since 2026-10-11: the only candidate to pass the pre-registered October
    # test plan (docs/TEST_RESULTS_2026_10.md, B3c) — more trades and better PF and
    # return in both windows vs 2.0x. Oracle fixtures were captured at 2.0x;
    # tests/test_fast_engine.py pins that value.
    MIN_BREAKOUT_VOLUME_MULTIPLIER: float = 1.5
    # 60-day VCP lookback: long enough to see full contraction cycle
    VCP_LOOKBACK_DAYS: int = 60
    # Tightness score > 50 = meaningful contraction; lower = loose/risky
    VCP_MIN_TIGHTNESS_SCORE: float = 50.0
    # 5% gap-up = R/R destroyed; better setups available next cycle
    MAX_GAP_UP_PCT: float = 0.05
    # Chasing more than 5% past pivot = poor risk/reward
    MAX_ENTRY_EXTENSION_PCT: float = 0.05
    TREND_TEMPLATE_EMA_PERIODS: list = field(default_factory=lambda: [50, 150, 200])
    # Weinstein uses 30-week MA as the primary trend anchor
    WEINSTEIN_MA_PERIOD_WEEKS: int = 30

    # ── Stock universe filters ──────────────────────────────────────────────
    # ₹100 min price: prevents illiquid penny stock traps
    MIN_STOCK_PRICE: float = 100.0
    # ₹5,000 Cr min market cap = large-cap institutional coverage
    MIN_MARKET_CAP_CR: float = 5_000.0
    # ₹10 Cr daily traded value = sufficient liquidity for ₹5L portfolio.
    # This is the sole liquidity gate since 2026-07-29: the old 5-lakh-share
    # count test was redundant with it and penalized high-priced liquid
    # stocks (a ₹3,000 stock at 3L shares/day moves ₹90Cr but failed it) —
    # it alone removed ~160 of the Nifty 500 from the scan pond.
    MIN_DAILY_TRADED_VALUE_CR: float = 10.0
    # >50% promoter pledge = potential forced selling risk
    MAX_PROMOTER_PLEDGE_PCT: float = 50.0
    # D/E > 2 = overleveraged; standalone P&L more relevant for trading signals
    MAX_DEBT_EQUITY_RATIO: float = 2.0
    # ROE < 15% = poor capital allocation; Prasenjit Paul quality threshold.
    # DISABLED 2026-07-29: this gate was never part of the validated vcp_v2
    # ruleset (the PF 1.61 backtest applied no stock filters) and it removed
    # ~160 of the Nifty 500 from the live scan pond — live was trading a far
    # smaller universe than the one the edge was proven on. Re-enable only
    # after a backtest variant WITH this gate passes both the dev
    # (2021-2024) and untouched validation (2025-2026) windows.
    ENFORCE_MIN_ROE: bool = False
    MIN_ROE_PCT: float = 15.0
    # 252 trading days = 1 full year; less = insufficient pattern history
    MIN_LISTING_DAYS: int = 252
    # >3 circuit hits in 60 days = regulatory/liquidity risk
    MAX_CIRCUIT_HITS_60D: int = 3
    # >5 missing days in 90 = data quality problem
    MAX_MISSING_DAYS_90D: int = 5

    # ── Market regime ───────────────────────────────────────────────────────
    # 60% stocks above 200 EMA = broad bull market participation
    BULL_BREADTH_THRESHOLD: float = 0.60
    # <40% = deteriorating breadth; bear signal
    BEAR_BREADTH_THRESHOLD: float = 0.40
    # VIX 20 = transition from calm to cautious; empirically validated for Nifty
    VIX_CAUTION_LEVEL: float = 20.0
    # VIX 25 = halve position sizes; market is pricing in elevated uncertainty
    VIX_REDUCE_LEVEL: float = 25.0
    # VIX 35 = extreme fear; no new trades, begin staged exit
    VIX_HALT_LEVEL: float = 35.0
    # 5% band around 200 EMA = "cautious zone"
    NIFTY_EMA_NEAR_PCT: float = 0.05

    # ── Momentum strategy (simple EMA crossover, runs alongside VCP) ───────
    # 4 simple conditions vs 8 strict VCP conditions — generates weekly signals
    MOMENTUM_EMA_FAST: int = 21          # fast EMA period
    MOMENTUM_EMA_SLOW: int = 50          # slow EMA period
    MOMENTUM_VOLUME_MULTIPLIER: float = 1.5   # volume must be 1.5x 20d avg
    MOMENTUM_MAX_PCT_FROM_HIGH: float = 0.12  # within 12% of 20-day high
    MOMENTUM_RSI_MIN: float = 45.0       # not oversold
    MOMENTUM_RSI_MAX: float = 72.0       # not overbought
    MOMENTUM_STOP_PCT: float = 0.05      # 5% hard stop
    MOMENTUM_TARGET_PCT: float = 0.12    # 12% fixed profit target (2.4:1 R:R)
    MOMENTUM_TIME_STOP_DAYS: int = 12    # exit if no progress after 12 days
    MOMENTUM_POSITION_SIZE_PCT: float = 0.08  # 8% of portfolio per trade
    MOMENTUM_RISK_PCT: float = 0.01      # 1% portfolio risk per momentum trade
    MOMENTUM_MAX_POSITIONS: int = 3      # cap momentum slots within the shared MAX_POSITIONS pool

    # ── Momentum v2 (exit-geometry experiment — BACKTEST ONLY until validated) ─
    # 5-yr gated backtest of v1 (2026-07-14): PF 0.69, 52% of trades stopped
    # at the 5% stop, only 10% reached the 12% target. Hypothesis: the entries
    # are fine, the exit geometry is inverted (capped wins, uncapped loss
    # frequency). v2 keeps v1 entries and sizing, changes only the exits.
    MOMENTUM2_STOP_PCT: float = 0.10       # hard stop outside normal NSE daily noise
    MOMENTUM2_EXIT_EMA: int = 21           # trend-break exit: close below EMA21
    MOMENTUM2_TIME_STOP_DAYS: int = 25     # give trends room instead of 12-day churn

    # ── Momentum v3 (entry-quality experiment — BACKTEST ONLY) ─────────────
    # v2 dev verdict (2026-07-14): exits fixed the payoff (avg win +9.1% vs
    # +5.7%, best +62.9%) but win rate collapsed to 22% — the entries buy
    # volume surges at 20d highs that top out immediately. v3 keeps v2's
    # exits and adds an entry-quality gate: stage-2 trend structure and
    # relative strength vs Nifty, so it only buys leaders in real uptrends.
    MOMENTUM3_RS_LOOKBACK: int = 63        # ~3 months; stock must beat Nifty over this window
    MOMENTUM3_EMA200_RISE_DAYS: int = 21   # EMA200 must be higher than this many days ago

    # ── VCP v2 (two-stage pivot watch — BACKTEST ONLY until validated) ──────
    # 5-yr gated backtest of v1 (2026-07-14): ZERO trades. Structural
    # contradiction: the breakout candle expands the range and invalidates
    # the same-day tightness requirement, so setup + breakout can never
    # coincide. v2 splits the stages: a valid tight setup puts the symbol on
    # a pivot watchlist; entry happens on the first valid breakout THROUGH
    # the stored pivot within the watch window (Minervini's actual workflow).
    # Dev iteration 2026-07-14: at 14 days, 14 of 15 watches expired before a
    # valid breakout (1 trade / 3.5 yrs, +45.8% — machinery works, funnel
    # starved). A valid pivot stays actionable for weeks; widen the window.
    VCP2_PIVOT_WATCH_DAYS: int = 30        # calendar days a detected pivot stays watchable

    # ── Range breakout strategy (high-frequency, 1-2 signals/day on Nifty 200) ─
    # Triggered when price closes at a new N-day high with volume — far simpler
    # than VCP or momentum, deliberately designed for daily signal frequency.
    BREAKOUT_LOOKBACK_DAYS: int = 20        # new N-day high as the trigger
    BREAKOUT_VOLUME_MIN: float = 1.5        # volume must be 1.5x prior 20d avg
    BREAKOUT_EMA_PERIOD: int = 50           # price must be above 50 EMA (uptrend)
    BREAKOUT_RSI_MAX: float = 75.0          # not overbought
    BREAKOUT_RSI_MIN: float = 45.0          # has momentum
    BREAKOUT_STOP_PCT: float = 0.05         # 5% hard stop (tighter = quicker cut)
    BREAKOUT_TARGET_PCT: float = 0.10       # 10% fixed target (2:1 R:R at 5% stop)
    BREAKOUT_TIME_STOP_DAYS: int = 7        # cut if no 10% target after 7 days
    BREAKOUT_POSITION_SIZE_PCT: float = 0.10  # 10% of portfolio per trade
    BREAKOUT_MAX_POSITIONS: int = 3      # cap breakout slots within the shared MAX_POSITIONS pool

    # ── Mean reversion strategy (Connors RSI-2 variant) ────────────────────
    # Buys short-term pullbacks inside long-term uptrends. Anti-correlated
    # with breakout: makes money in chop, bleeds in strong trends.
    # Cost warning: 0.5% NSE round trip consumes 15-30% of each winning trade.
    MR_EMA_TREND: int = 200        # trend gate: close > EMA(200) required
    MR_EMA_EXIT: int = 5           # exit when close crosses above EMA(5)
    MR_RSI_PERIOD: int = 2         # ultra-short RSI period for sensitivity
    MR_RSI_ENTRY: float = 10.0     # entry: RSI(2) < 10 (deep short-term oversold)
    MR_RSI_EXIT: float = 70.0      # exit: RSI(2) > 70 (short-term momentum restored)
    MR_STOP_PCT: float = 0.05      # 5% hard stop (wider than expected avg loss — test carefully)
    MR_TIME_STOP_DAYS: int = 7     # cut if no exit signal after 7 trading days
    MR_POSITION_SIZE_PCT: float = 0.10  # 10% of portfolio per trade (same as breakout)
    MR_MAX_POSITIONS: int = 4      # cap MR slots; leave room for VCP/breakout in shared pool

    # ── Exit rules (VCP) ───────────────────────────────────────────────────
    # 10% first stop since 2026-10-11 (owner decision, was 7% = Minervini standard).
    # The 4-way grid (docs/TEST_RESULTS_2026_10.md, "Volume x stop grid") showed -10% better
    # than -7% at both 1.5x and 2.0x volume: full 2021-09..2026-08 PF 1.26 vs 1.04, max DD
    # -13.3% vs -15.7%, faster in 2021-24 and tied in 2025-26. It technically failed the
    # pre-registered "higher in both windows" rule (A1), so this is a deliberate override.
    # At 2% risk the 20% position cap still binds exactly (2% / 10% = 20%), so position size
    # is unchanged; a stopped trade now loses ~2% of capital instead of ~1.4%.
    # Oracle fixtures were captured at 7%; tests/test_fast_engine.py pins that value.
    HARD_STOP_PCT: float = 0.10
    # 15 days with <7% profit = capital is sitting idle; redeploy elsewhere
    TIME_STOP_DAYS: int = 15
    TIME_STOP_MIN_PROFIT_PCT: float = 0.07
    # Top 4% of day's range = strong institutional close; weak closes are warning signs
    STRONG_CLOSE_THRESHOLD: float = 0.96  # close >= high * 0.96

    # ── Trading hours (IST) ─────────────────────────────────────────────────
    # 9:30 start: first 15 min often fake moves driven by retail panic/FOMO
    ENTRY_WINDOW_START: str = "09:30"
    ENTRY_WINDOW_END: str = "10:30"
    # No entries after 3pm; insufficient time for position to work + illiquid close
    NO_ENTRY_AFTER: str = "15:00"
    MARKET_OPEN: str = "09:15"
    MARKET_CLOSE: str = "15:30"
    PRE_MARKET_START: str = "09:00"

    # ── Heartbeat / dead man's switch ───────────────────────────────────────
    # 30-min silence during market hours = something is wrong
    HEARTBEAT_INTERVAL_MINUTES: int = 30
    HEARTBEAT_TIMEOUT_MINUTES: int = 30

    # ── Telegram ─────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))

    # ── Manual positions blocklist ────────────────────────────────────────────
    # Managed via data/manual_blocklist.txt — one symbol per line.
    # The bot re-reads the file on every entry check; no restart needed.
    MANUAL_BLOCKLIST_PATH: str = "data/manual_blocklist.txt"
    # Sanctioned manual override: write a symbol into this file and the bot
    # exits the position properly (order + GTT cancel + ledger + alert)
    # within 5 minutes. Never sell bot positions from the Kite app directly.
    MANUAL_EXIT_PATH: str = "data/manual_exit.txt"

    # ── Data refresh schedules ──────────────────────────────────────────────
    # Fundamentals change quarterly; weekly refresh is more than sufficient
    FUNDAMENTALS_REFRESH_DAYS: int = 7
    # 500 days = ~2 years; enough for 200 EMA + Weinstein 30-week MA calculations
    HISTORICAL_DAYS: int = 500
    # Number of historical days to seed backtest data
    BACKTEST_HISTORY_DAYS: int = 2500   # ~10 years

    # ── Backtest data source (EXPLICIT — see docs/DATA_FOUNDATION_REVIEW.md) ──
    # "reconstructed"      data/reconstructed.db — ISIN-keyed, corporate-action
    #                      corrected, Historical Liquid-500 point-in-time universe,
    #                      regenerable from the hash-verified NSE bhavcopy archive.
    # "legacy_fabricated"  data/market_data.db + fundamentals.universe_snapshots.
    #                      SURVIVORSHIP-BIASED AND FABRICATED: its only pre-2026
    #                      universe snapshot is the July-2026 Nifty 500 backdated
    #                      (499/500 identical to today), and 36% of the real
    #                      historical candidate pool is missing entirely. Every
    #                      performance number produced from it is void. Selecting
    #                      it makes the loaders raise unless
    #                      ALLOW_FABRICATED_BACKTEST_DATA is also set true.
    # Overridable by env so a deliberate archaeological run is possible, but it
    # can never happen by accident or by default.
    BACKTEST_DATA_SOURCE: str = field(
        default_factory=lambda: os.environ.get("BACKTEST_DATA_SOURCE", "reconstructed")
    )
    ALLOW_FABRICATED_BACKTEST_DATA: bool = field(
        default_factory=lambda: os.environ.get(
            "ALLOW_FABRICATED_BACKTEST_DATA", "false").lower() == "true"
    )
    RECONSTRUCTED_DB: str = "reconstructed.db"

    # ── Paths ────────────────────────────────────────────────────────────────
    DB_PATH: str = "data"
    # Corporate-events DB filename. Live reads the default, populated daily by
    # refresh_corporate_events. Backtests point at a full historical snapshot
    # (scripts/backfill_events.py output) via --events-db so a multi-year
    # backfill never alters the pond the live filter sees (2026-08-07).
    CORPORATE_EVENTS_DB: str = "corporate_events.db"
    LOG_PATH: str = "logs"
    DATA_BACKUP_PATH: str = "data/backups"

    # ── Rate limiting ────────────────────────────────────────────────────────
    # Kite Connect limits: 10 orders/sec, 200 historical calls/min
    KITE_ORDER_RATE_LIMIT: int = 10       # per second
    KITE_HISTORICAL_RATE_LIMIT: int = 200 # per minute
    NSE_SCRAPE_DELAY_SECONDS: float = 2.0  # be respectful to NSE servers

    def validate(self) -> list[str]:
        """Return list of validation errors; empty list = config is valid."""
        errors: list[str] = []
        if not self.PAPER_TRADING_MODE:
            if not self.ZERODHA_API_KEY:
                errors.append("ZERODHA_API_KEY is required for live trading")
            if not self.ZERODHA_API_SECRET:
                errors.append("ZERODHA_API_SECRET is required for live trading")
            if not self.ZERODHA_USER_ID:
                errors.append("ZERODHA_USER_ID is required for live trading")
            if not self.TELEGRAM_BOT_TOKEN:
                errors.append("TELEGRAM_BOT_TOKEN is required for live trading")
            if not self.TELEGRAM_CHAT_ID:
                errors.append("TELEGRAM_CHAT_ID is required for live trading")
        if self.RISK_PER_TRADE_PCT > 0.03:
            errors.append("RISK_PER_TRADE_PCT > 3% is dangerously high")
        if self.MAX_DRAWDOWN_PCT > 0.30:
            errors.append("MAX_DRAWDOWN_PCT > 30% is dangerously high")
        return errors


config = TradingConfig()
