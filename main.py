"""Main entry point for the automated swing trading bot.

Startup sequence:
  1. Load config, validate secrets
  2. Setup logging
  3. Initialize databases
  4. Connect to Kite (or paper mode)
  5. Start health check server
  6. Start APScheduler
  7. Enter monitoring loop

Usage:
    python main.py            # Paper trading mode (default)
    python main.py --paper    # Explicit paper mode
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Bootstrap path before local imports ──────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from utils.logger import setup_logging
setup_logging()

logger = logging.getLogger("trading.main")

from alerts.telegram_alert import send_system_error, send_text
from broker.gtt_monitor import GTTBackupMonitor
from backtest.performance_analyzer import print_report
from broker.auth_manager import AuthManager
from broker.kite_client import KiteClient
from broker.order_manager import OrderManager
from broker.portfolio_manager import PortfolioManager
from config.config import config
from config.holidays import is_trading_day
from data_fetcher.event_fetcher import fetch_corporate_events, save_events
from data_fetcher.ohlcv_fetcher import OHLCVFetcher
from data_fetcher.surveillance_fetcher import get_all_surveillance_symbols
from data_fetcher.universe_fetcher import fetch_universe_symbols, load_universe, save_universe_snapshot
from exit.profit_manager import check_partial_profit, check_technical_breakdown, should_exit_on_time_stop
from exit.stop_loss_manager import calculate_trailing_stop, should_exit_on_stop
from portfolio.portfolio_tracker import PortfolioTracker
from regime.regime_detector import Regime, RegimeDetector, RegimeState
from reports.daily_report import generate_daily_report, generate_eod_position_list
from risk.drawdown_monitor import DrawdownMonitor
from risk.position_sizer import PositionSizeResult, calculate_position_size, calculate_stop_loss_price
from risk.risk_manager import RiskManager
from scanner.opportunity_ranker import TradeSetup, rank_setups, select_top_setups
from scanner.relative_strength import calculate_rs_universe
from scanner.stock_filter import apply_all_filters, run_batch_filters
from strategy.multi_timeframe import MultiTimeframeResult, analyse_stock
from utils.backup import backup_databases
from utils.health_check import (
    is_emergency_halt_active,
    record_heartbeat,
    start_http_health_server,
)
from utils.time_utils import is_entry_window, is_market_open, today_ist


def _load_manual_blocklist() -> set[str]:
    """Read data/manual_blocklist.txt and return a set of blocked symbols.

    Called fresh on every entry check so changes take effect without restart.
    Lines starting with # are comments. Case-insensitive.
    """
    path = Path(config.MANUAL_BLOCKLIST_PATH)
    if not path.exists():
        return set()
    blocked: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            blocked.add(line.upper())
    return blocked


class TradingBot:
    """Main trading bot orchestrator.

    Coordinates all modules and implements the full daily workflow.
    """

    def __init__(self) -> None:
        self._kite = KiteClient()
        self._auth = AuthManager()
        self._ohlcv_fetcher: OHLCVFetcher = OHLCVFetcher()
        self._portfolio_manager: PortfolioManager = PortfolioManager()
        self._portfolio_tracker: PortfolioTracker = PortfolioTracker(self._portfolio_manager)
        self._drawdown_monitor: DrawdownMonitor = DrawdownMonitor(self._portfolio_manager)
        self._risk_manager: RiskManager = RiskManager(self._portfolio_manager, self._drawdown_monitor)
        self._gtt_monitor: GTTBackupMonitor = GTTBackupMonitor(self._kite)
        self._universe_ohlcv: dict = {}
        self._nifty_ohlcv = None
        self._regime_state: RegimeState = None  # type: ignore[assignment]
        self._watchlist: list[TradeSetup] = []
        self._instrument_map: dict[str, dict] = {}   # symbol → instrument details
        self._days_in_bear: int = 0  # for staged exit tracking
        self._trades_today: int = 0

    # ── Daily workflow ────────────────────────────────────────────────────────

    def startup(self) -> None:
        """One-time startup: connect to broker, validate config."""
        logger.info("Trading bot starting up (paper=%s)", config.PAPER_TRADING_MODE)

        errors = config.validate()
        if errors:
            for err in errors:
                logger.error("Config error: %s", err)
            if not config.PAPER_TRADING_MODE:
                raise RuntimeError("Config validation failed for live mode")

        # Connect to Kite for market data in both live and paper mode.
        # Paper mode only blocks real order placement — data fetching always needs Kite.
        token = self._auth.get_access_token()
        if token:
            self._kite.connect(token)
            self._ohlcv_fetcher = OHLCVFetcher(self._kite.raw)
            if not config.PAPER_TRADING_MODE:
                self._portfolio_manager = PortfolioManager(self._kite)
                self._reconcile_holdings()
            logger.info("Kite connected successfully (paper=%s)", config.PAPER_TRADING_MODE)
        else:
            if config.PAPER_TRADING_MODE:
                logger.info("Paper mode — no token yet; will connect after 8 AM TOTP login")
            else:
                logger.warning("No access token — operating in monitoring-only mode")

    def send_login_url(self) -> None:
        """Daily auth refresh at 8:00 AM — TOTP auto-login if opted in, else send URL.

        Runs in paper mode too — Kite connection is needed for live market data
        even when orders are not placed.

        TOTP automation uses Zerodha's internal web endpoints, not the official
        Kite Connect OAuth flow.  Set TOTP_AUTO_LOGIN_ENABLED=True in .env only
        after explicitly accepting the account-revocation risk this implies.
        """
        if config.TOTP_AUTO_LOGIN_ENABLED and config.ZERODHA_TOTP_SECRET:
            logger.warning(
                "TOTP auto-login is ENABLED — uses internal Zerodha endpoints "
                "(not official Kite Connect OAuth). Account revocation risk accepted."
            )
            token = self._auth.login_with_totp()
            if token:
                self._kite.connect(token)
                self._ohlcv_fetcher = OHLCVFetcher(self._kite.raw)
                self._portfolio_manager = PortfolioManager(self._kite)
                send_text("Auto-login successful — session ready")
                logger.info("TOTP auto-login successful")
                return
            logger.warning("TOTP auto-login failed — falling back to manual URL")
            send_text("TOTP auto-login failed — sending manual login URL")
        from alerts.telegram_alert import send_login_url as _send_url
        url = self._auth.get_login_url()
        _send_url(url, self._portfolio_manager.get_portfolio_value())
        logger.info("Login URL sent to Telegram")

    def refresh_fundamentals(self) -> None:
        """Refresh fundamental data for stale symbols."""
        from data_fetcher.fundamental_fetcher import refresh_all_fundamentals
        symbols = load_universe()
        refresh_all_fundamentals(symbols[:20])  # batch 20 at a time

    def refresh_surveillance(self) -> None:
        """Refresh ASM/ESM surveillance lists."""
        get_all_surveillance_symbols(use_cached=False)

    def refresh_corporate_events(self) -> None:
        """Refresh upcoming corporate events."""
        events = fetch_corporate_events(days_ahead=10)
        save_events(events)

    def refresh_universe_ohlcv(self) -> None:
        """Fetch/update OHLCV for all universe stocks."""
        symbols = load_universe()
        self._load_instrument_map()

        symbol_token_map = {
            sym: self._instrument_map.get(sym, {}).get("instrument_token", 0)
            for sym in symbols
            if self._instrument_map.get(sym, {}).get("instrument_token", 0) != 0
        }

        if symbol_token_map and self._kite.is_connected:
            self._universe_ohlcv = self._ohlcv_fetcher.get_bulk_ohlcv(symbol_token_map)
        else:
            # No Kite connection yet — load from cache
            for sym in symbols:
                df = self._ohlcv_fetcher._load_from_cache(sym)
                if df is not None:
                    self._universe_ohlcv[sym] = df

        logger.info("Universe OHLCV refreshed: %d stocks", len(self._universe_ohlcv))

    def run_pre_scan(self) -> None:
        """Run all 9 stock filters on universe, build candidate list."""
        surveillance = get_all_surveillance_symbols(use_cached=True)
        universe_data = {}

        for symbol, df in self._universe_ohlcv.items():
            from data_fetcher.fundamental_fetcher import get_fundamentals
            fundamentals = get_fundamentals(symbol) or {}
            inst_data = self._instrument_map.get(symbol, {})

            universe_data[symbol] = {
                "ohlcv": df,
                "close": float(df["close"].iloc[-1]) if not df.empty else 0.0,
                "market_cap_cr": fundamentals.get("market_cap_cr"),
                "promoter_pledge_pct": fundamentals.get("promoter_pledge_pct"),
                "debt_equity_ratio": fundamentals.get("debt_equity_ratio"),
                "roe_pct": fundamentals.get("roe_pct"),
                "listing_date": inst_data.get("listing_date"),
                "category": inst_data.get("segment", "EQ"),
            }

        passing_symbols, _ = run_batch_filters(universe_data, surveillance)
        # Keep only passing symbols in the OHLCV map
        self._universe_ohlcv = {s: self._universe_ohlcv[s] for s in passing_symbols if s in self._universe_ohlcv}
        logger.info("Pre-scan complete: %d stocks pass all filters", len(self._universe_ohlcv))

    def run_strategy_scan(self) -> None:
        """Two-stage VCP scan (validated vcp_v2 config, 2026-07-16).

        Stage 1: a valid tight consolidation stores its pivot on a persistent
        watchlist — it does NOT become an entry candidate directly.
        Stage 2: symbols on the watchlist become candidates only when
        yesterday's candle was a valid breakout THROUGH the stored pivot.
        Mirrors backtest_engine's vcp_v2 path; keep the two in sync.
        """
        from scanner.relative_strength import calculate_rs_universe
        from strategy.breakout_detector import get_avg_volume_50d, is_valid_breakout
        from strategy.pivot_watch import add_symbol, load_watch, purge_expired, save_watch

        nifty_df = self._universe_ohlcv.get("NIFTY50") or self._get_nifty_ohlcv()
        if nifty_df is None:
            logger.warning("No Nifty data — RS scores unavailable")
            rs_scores = {}
        else:
            rs_scores = calculate_rs_universe(self._universe_ohlcv, nifty_df)

        watch = load_watch()
        expired = purge_expired(watch, today_ist())
        watch_added = 0
        breakouts = 0

        candidates: list[TradeSetup] = []
        fail_tt = 0
        fail_vcp = 0
        fail_rs = 0

        # Belt and suspenders: the entry gate in check_entries also enforces
        # the blocklist, but blocked symbols shouldn't even be watched
        blocked = _load_manual_blocklist()

        for symbol, df in self._universe_ohlcv.items():
            if symbol.upper() in blocked:
                watch.pop(symbol.upper(), None)
                continue
            # ── Stage 2: watched pivot → enter only on a confirmed breakout ──
            pend = watch.get(symbol.upper())
            if pend is not None:
                last_candle = df.iloc[-1].to_dict()
                bo = is_valid_breakout(
                    today_ohlcv=last_candle,
                    pivot_price=pend["pivot"],
                    avg_volume_50d=get_avg_volume_50d(df),
                )
                if not bo.is_valid:
                    continue  # still watching
                del watch[symbol.upper()]
                breakouts += 1
                candidates.append(TradeSetup(
                    symbol=symbol,
                    rs_percentile=rs_scores.get(symbol, pend.get("rs_percentile", 50.0)),
                    vcp_tightness_score=pend.get("tightness", 0.0),
                    trend_template_score=100.0,
                    volume_multiplier=bo.volume_multiplier,
                    pivot_price=pend["pivot"],
                    stop_loss_price=pend.get("vcp_low", 0.0),
                    entry_price=float(df["close"].iloc[-1]),
                    sector="Unknown",
                ))
                continue

            # ── Stage 1: fresh setups go on the pivot watch, not into entry ──
            result = analyse_stock(symbol, df)
            if not result.is_high_confidence:
                if result.trend_template_result and not result.trend_template_result.passes:
                    fail_tt += 1
                elif result.vcp_result is not None and not result.vcp_result.is_valid:
                    fail_vcp += 1
                elif result.vcp_result is not None:
                    fail_vcp += 1  # tightness < 50
                continue

            if not result.vcp_result or result.vcp_result.pivot_price <= 0:
                fail_vcp += 1
                continue

            rs_pct = rs_scores.get(symbol, 50.0)
            if add_symbol(
                watch, symbol,
                pivot=result.vcp_result.pivot_price,
                vcp_low=result.vcp_result.consolidation_low or 0.0,
                rs_percentile=rs_pct,
                tightness=result.vcp_result.tightness_score,
                today=today_ist(),
            ):
                watch_added += 1
            continue

        save_watch(watch)

        total = len(self._universe_ohlcv)
        logger.info(
            "Strategy scan: %d breakout candidates / %d watched pivots (+%d new, -%d expired) "
            "of %d stocks (trend_template_fail=%d, vcp_fail=%d, rs_fail=%d)",
            breakouts, len(watch), watch_added, expired, total, fail_tt, fail_vcp, fail_rs,
        )
        self._watchlist = rank_setups(candidates)
        logger.info("Strategy scan: %d setups in watchlist", len(self._watchlist))

    def run_momentum_scan(self) -> None:
        """Scan filtered universe for simple EMA-crossover momentum setups."""
        from strategy.momentum_strategy import check_momentum
        from data_fetcher.fundamental_fetcher import get_fundamentals

        passed = 0
        failed = 0
        for symbol, df in self._universe_ohlcv.items():
            result = check_momentum(symbol, df)
            if not result.passes:
                failed += 1
                continue

            fundamentals = get_fundamentals(symbol) or {}
            sector = fundamentals.get("sector", "Unknown")

            setup = TradeSetup(
                symbol=symbol,
                rs_percentile=50.0,         # RS not used for momentum ranking
                vcp_tightness_score=0.0,
                trend_template_score=0.0,
                volume_multiplier=result.volume_ratio,
                pivot_price=result.entry_price,
                stop_loss_price=result.stop_loss,
                target_price=result.target_price,
                entry_price=result.entry_price,
                sector=sector,
                strategy="momentum",
                composite_score=result.score,
            )
            self._watchlist.append(setup)
            passed += 1

        logger.info(
            "Momentum scan: %d setups found, %d failed (%d total checked)",
            passed, failed, passed + failed,
        )

    def run_mean_reversion_scan(self) -> None:
        """Scan filtered universe for Connors RSI-2 mean-reversion setups."""
        from strategy.mean_reversion import check_mean_reversion
        from data_fetcher.fundamental_fetcher import get_fundamentals

        passed = 0
        failed = 0
        for symbol, df in self._universe_ohlcv.items():
            result = check_mean_reversion(symbol, df)
            if not result.passes:
                failed += 1
                continue

            fundamentals = get_fundamentals(symbol) or {}
            sector = fundamentals.get("sector", "Unknown")

            setup = TradeSetup(
                symbol=symbol,
                rs_percentile=50.0,
                vcp_tightness_score=0.0,
                trend_template_score=0.0,
                volume_multiplier=1.0,
                pivot_price=result.entry_price,
                stop_loss_price=result.stop_loss,
                target_price=0.0,      # MR exits on EMA5/RSI2, not a fixed target
                entry_price=result.entry_price,
                sector=sector,
                strategy="mean_reversion",
                composite_score=result.score,
            )
            self._watchlist.append(setup)
            passed += 1

        logger.info(
            "Mean reversion scan: %d setups found, %d failed (%d total checked)",
            passed, failed, passed + failed,
        )

    def run_breakout_scan(self) -> None:
        """Scan filtered universe for 20-day range breakout setups."""
        from strategy.breakout_scanner import check_range_breakout
        from data_fetcher.fundamental_fetcher import get_fundamentals

        passed = 0
        failed = 0
        for symbol, df in self._universe_ohlcv.items():
            result = check_range_breakout(symbol, df)
            if not result.passes:
                failed += 1
                continue

            fundamentals = get_fundamentals(symbol) or {}
            sector = fundamentals.get("sector", "Unknown")

            stop_price = round(result.entry_price * (1 - config.BREAKOUT_STOP_PCT), 2)
            setup = TradeSetup(
                symbol=symbol,
                rs_percentile=50.0,
                vcp_tightness_score=0.0,
                trend_template_score=0.0,
                volume_multiplier=result.volume_ratio,
                pivot_price=result.entry_price,
                stop_loss_price=stop_price,
                target_price=result.target_price,
                entry_price=result.entry_price,
                sector=sector,
                strategy="breakout",
                composite_score=result.score,
            )
            self._watchlist.append(setup)
            passed += 1

        logger.info(
            "Breakout scan: %d setups found, %d failed (%d total checked)",
            passed, failed, passed + failed,
        )

    def _prefetch_vix_from_kite(self) -> None:
        """Try to get India VIX from Kite quotes and update the vix_monitor cache.

        Falls back silently — NSE API remains the primary source.
        """
        if not self._kite.is_connected:
            return
        try:
            quotes = self._kite.raw.quote(["NSE:INDIA VIX"])
            vix = quotes.get("NSE:INDIA VIX", {}).get("last_price")
            if vix:
                from regime.vix_monitor import update_vix_from_kite
                update_vix_from_kite(float(vix))
        except Exception as exc:
            logger.debug("Kite VIX prefetch failed (non-critical): %s", exc)

    def check_regime(self) -> None:
        """Update market regime state."""
        if not self._universe_ohlcv:
            return

        self._prefetch_vix_from_kite()
        nifty_df = self._get_nifty_ohlcv()
        if nifty_df is None:
            return

        detector = RegimeDetector(self._universe_ohlcv, nifty_df)
        old_regime = self._regime_state.regime if self._regime_state else None
        self._regime_state = detector.detect()
        logger.info(
            "Regime: %s | Nifty=%.0f EMA200=%.0f (%.1f%%) | Breadth=%.1f%% | VIX=%s",
            self._regime_state.regime.value,
            self._regime_state.nifty_close,
            self._regime_state.nifty_ema200,
            (self._regime_state.nifty_close / self._regime_state.nifty_ema200 - 1) * 100,
            self._regime_state.breadth_pct * 100,
            f"{self._regime_state.india_vix:.1f}" if self._regime_state.india_vix else "N/A",
        )

        if old_regime and old_regime != self._regime_state.regime:
            from alerts.telegram_alert import send_regime_change
            action = "no_new_trades" if not self._regime_state.allow_new_trades else "trading_normal"
            send_regime_change(
                str(old_regime),
                str(self._regime_state.regime.value),
                action,
                self._portfolio_manager.get_portfolio_value(),
            )
            if self._regime_state.regime == Regime.BEAR:
                self._days_in_bear = 1
            elif self._regime_state.regime == Regime.EXTREME_VOLATILITY:
                self._days_in_bear = 1
        elif old_regime == self._regime_state.regime and self._regime_state.regime in (Regime.BEAR, Regime.EXTREME_VOLATILITY):
            self._days_in_bear += 1
        else:
            self._days_in_bear = 0

    def rank_opportunities(self) -> None:
        """Final ranking and watchlist preparation before market open."""
        if self._regime_state and not self._regime_state.allow_new_trades:
            logger.info("Regime %s — no new entries today", self._regime_state.regime.value if self._regime_state else "UNKNOWN")
            self._watchlist = []
            return

        # Strategy bench: drop setups from strategies with a rejected edge
        if config.DISABLED_STRATEGIES:
            before = len(self._watchlist)
            self._watchlist = [s for s in self._watchlist if s.strategy not in config.DISABLED_STRATEGIES]
            if before - len(self._watchlist):
                logger.info(
                    "Strategy bench: dropped %d setups from benched strategies %s",
                    before - len(self._watchlist), config.DISABLED_STRATEGIES,
                )

        open_positions = self._portfolio_manager.get_open_positions()
        open_count = len(open_positions)
        open_symbols = {p["symbol"].upper() for p in open_positions}

        # VCP: no dedicated cap — may use any free slot in the shared pool
        vcp_open = sum(1 for p in open_positions if p.get("strategy", "vcp") == "vcp")
        vcp_slots = max(0, config.MAX_POSITIONS - vcp_open)
        vcp_setups = [s for s in self._watchlist if s.strategy == "vcp" and s.symbol.upper() not in open_symbols]
        vcp_top = select_top_setups(vcp_setups, 0, vcp_slots)

        # Momentum: capped at MOMENTUM_MAX_POSITIONS concurrent slots
        mom_open = sum(1 for p in open_positions if p.get("strategy") == "momentum")
        mom_slots = max(0, config.MOMENTUM_MAX_POSITIONS - mom_open)
        momentum_setups = [
            s for s in self._watchlist
            if s.strategy == "momentum" and s.symbol.upper() not in open_symbols
        ]
        momentum_top = sorted(momentum_setups, key=lambda s: s.composite_score, reverse=True)[:mom_slots]

        # Breakout: capped at BREAKOUT_MAX_POSITIONS concurrent slots
        bo_open = sum(1 for p in open_positions if p.get("strategy") == "breakout")
        bo_slots = max(0, config.BREAKOUT_MAX_POSITIONS - bo_open)
        breakout_setups = [
            s for s in self._watchlist
            if s.strategy == "breakout" and s.symbol.upper() not in open_symbols
        ]
        breakout_top = sorted(breakout_setups, key=lambda s: s.composite_score, reverse=True)[:bo_slots]

        # Mean reversion: cap at MR_MAX_POSITIONS; rank by score (lower RSI2 = higher score)
        mr_open_count = sum(1 for p in open_positions if p.get("strategy") == "mean_reversion")
        mr_slots = max(0, config.MR_MAX_POSITIONS - mr_open_count)
        mr_setups = [
            s for s in self._watchlist
            if s.strategy == "mean_reversion" and s.symbol.upper() not in open_symbols
        ]
        mr_top = sorted(mr_setups, key=lambda s: s.composite_score, reverse=True)[:mr_slots]

        # Shared pool cap: per-strategy slots can sum past MAX_POSITIONS, so the
        # combined list is truncated to the free slots left in the total pool.
        # Order encodes entry priority: VCP > momentum > breakout > mean reversion.
        # A symbol can signal in several scans at once — keep only its
        # highest-priority setup so duplicates don't consume pool slots.
        total_slots = max(0, config.MAX_POSITIONS - open_count)
        seen_symbols: set[str] = set()
        combined = []
        for s in vcp_top + momentum_top + breakout_top + mr_top:
            if s.symbol.upper() not in seen_symbols:
                seen_symbols.add(s.symbol.upper())
                combined.append(s)
        self._watchlist = combined[:total_slots]
        logger.info(
            "Watchlist ready: %d VCP + %d momentum + %d breakout + %d mean_reversion setups"
            " → %d kept (%d open, %d free slots of %d)",
            len(vcp_top), len(momentum_top), len(breakout_top), len(mr_top),
            len(self._watchlist), open_count, total_slots, config.MAX_POSITIONS,
        )

    def verify_gtt_orders(self) -> None:
        """Verify all open positions have active GTT stop-loss orders."""
        if config.PAPER_TRADING_MODE:
            return
        positions = self._portfolio_manager.get_open_positions()
        unprotected = [p["symbol"] for p in positions if not p.get("gtt_order_id")]
        if unprotected:
            # A tracked position without a GTT ID means stop protection was
            # never established — the exact failure this check exists to catch
            logger.critical("Open positions with NO GTT stop-loss: %s", unprotected)
            from alerts.telegram_alert import send_system_error
            send_system_error(
                "GTT verify",
                f"Positions without GTT stop protection: {', '.join(unprotected)}. "
                f"Place stops manually in Kite or exit via data/manual_exit.txt.",
            )
        expected = {p["symbol"]: p.get("gtt_order_id", "") for p in positions if p.get("gtt_order_id")}
        order_mgr = OrderManager(self._kite)
        order_mgr.sync_gtt_orders(expected)

    def check_entries(self) -> None:
        """Entry window: evaluate watchlist and place orders."""
        if not is_entry_window():
            return
        if is_emergency_halt_active():
            return
        if not self._regime_state or not self._regime_state.allow_new_trades:
            return

        can_trade, reason = self._drawdown_monitor.check_all_limits()
        if not can_trade:
            logger.info("Entry blocked: %s", reason)
            return

        from strategy.breakout_detector import evaluate_gap, GapDecision
        order_mgr = OrderManager(self._kite)
        portfolio_value = self._portfolio_manager.get_portfolio_value()
        available_cash = self._portfolio_manager.get_available_cash()

        blocked = _load_manual_blocklist()
        for setup in list(self._watchlist):
            if setup.strategy in config.DISABLED_STRATEGIES:
                self._watchlist.remove(setup)
                continue
            if setup.symbol.upper() in blocked:
                logger.info("Skipping %s — in manual_blocklist.txt (manually held position)", setup.symbol)
                self._watchlist.remove(setup)
                continue

            df = self._universe_ohlcv.get(setup.symbol)
            if df is None or df.empty:
                continue

            # Evaluate today's open vs pivot
            ltp_data = self._kite.get_ltp([f"NSE:{setup.symbol}"]) if not config.PAPER_TRADING_MODE else {}
            today_open = ltp_data.get(f"NSE:{setup.symbol}", float(df["close"].iloc[-1]))
            prev_close = float(df["close"].iloc[-1])

            gap_decision, gap_reason = evaluate_gap(prev_close, today_open, setup.pivot_price)
            if gap_decision == GapDecision.SKIP:
                logger.info("Skipping %s: %s", setup.symbol, gap_reason)
                self._watchlist.remove(setup)
                continue

            entry_price = today_open
            strategy = setup.strategy  # "vcp", "momentum", "breakout", or "mean_reversion"

            if strategy == "momentum":
                # Momentum: fixed 8% position size, 5% stop, 12% target
                stop_loss = round(entry_price * (1 - config.MOMENTUM_STOP_PCT), 2)
                target_price = round(entry_price * (1 + config.MOMENTUM_TARGET_PCT), 2)
                size_pct = config.MOMENTUM_POSITION_SIZE_PCT
            elif strategy == "breakout":
                # Breakout: fixed 10% position size, 5% stop, 10% target
                stop_loss = round(entry_price * (1 - config.BREAKOUT_STOP_PCT), 2)
                target_price = round(entry_price * (1 + config.BREAKOUT_TARGET_PCT), 2)
                size_pct = config.BREAKOUT_POSITION_SIZE_PCT
            elif strategy == "mean_reversion":
                # Mean reversion: fixed 10% position size, 5% stop, no fixed target (EMA5/RSI2 exit)
                stop_loss = round(entry_price * (1 - config.MR_STOP_PCT), 2)
                target_price = 0.0
                size_pct = config.MR_POSITION_SIZE_PCT
            else:
                # VCP: risk-based position sizing, trailing stop, no fixed target
                stop_loss = calculate_stop_loss_price(entry_price)
                target_price = 0.0
                size_pct = None

            if size_pct is not None:
                target_value = portfolio_value * size_pct
                target_value *= self._regime_state.position_size_multiplier
                shares = int(target_value / entry_price)
                if shares == 0:
                    # A single share may still be acceptable, but only while the
                    # overshoot vs the intended position value stays tolerable
                    if entry_price <= target_value * config.MAX_SINGLE_SHARE_OVERSHOOT:
                        shares = 1
                    else:
                        logger.info(
                            "Entry skipped for %s — 1 share (₹%.0f) exceeds %.1fx the ₹%.0f size target",
                            setup.symbol, entry_price,
                            config.MAX_SINGLE_SHARE_OVERSHOOT, target_value,
                        )
                        self._watchlist.remove(setup)
                        continue
                if available_cash < shares * entry_price:
                    logger.info("%s entry skipped for %s — insufficient cash", strategy, setup.symbol)
                    continue
                size = PositionSizeResult(
                    shares=shares,
                    position_value=shares * entry_price,
                    actual_risk_pct=(shares * (entry_price - stop_loss)) / portfolio_value,
                    actual_position_pct=(shares * entry_price) / portfolio_value,
                    capped_by="fixed_pct",
                    is_valid=True,
                    reason="",
                )
            else:
                size = calculate_position_size(
                    portfolio_value=portfolio_value,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss,
                    regime_multiplier=self._regime_state.position_size_multiplier,
                    available_cash=available_cash,
                )
                shares = size.shares

            if not size.is_valid or shares <= 0:
                logger.info("Entry skipped for %s — invalid size", setup.symbol)
                self._watchlist.remove(setup)
                continue

            # Dust floor — same constant and regime scaling as backtest_engine
            # (DUST_FLOOR_DECISION.md 2026-08-07).
            min_pos_value = config.MIN_POSITION_VALUE * self._regime_state.position_size_multiplier
            if size.position_value < min_pos_value:
                logger.info(
                    "Entry skipped for %s — ₹%.0f position below the ₹%.0f dust floor",
                    setup.symbol, size.position_value, min_pos_value,
                )
                # Transient condition (cash-capped near full investment) —
                # keep the pivot alive, same as a risk-gate block
                if setup.strategy == "vcp":
                    self._restore_pivot_watch(setup)
                self._watchlist.remove(setup)
                continue

            # Risk gate for every strategy: MAX_POSITIONS pool cap, duplicate
            # holding, cash reserve, and the 30% sector exposure cap
            from data_fetcher.fundamental_fetcher import get_fundamentals
            fundamentals = get_fundamentals(setup.symbol) or {}
            sector = fundamentals.get("sector", "Unknown")
            risk_check = self._risk_manager.approve_entry(
                symbol=setup.symbol,
                sector=sector,
                size_result=size,
                regime_state=self._regime_state,
            )
            if not risk_check.approved:
                logger.info("Entry blocked for %s: %s", setup.symbol, risk_check.reason)
                # Risk blocks (cash, position pool, sector cap) are transient
                # conditions — put the consumed pivot back on watch so a later
                # breakout can still fire (2026-07-31: SONACOMS's pivot was
                # silently lost this way on the first live signal)
                if setup.strategy == "vcp":
                    self._restore_pivot_watch(setup)
                self._watchlist.remove(setup)
                continue

            # Place order
            if strategy == "breakout":
                tag_prefix = "bo"
            elif strategy == "momentum":
                tag_prefix = "mom"
            elif strategy == "mean_reversion":
                tag_prefix = "mr"
            else:
                tag_prefix = "bot"
            order_id = order_mgr.buy_cnc(
                symbol=setup.symbol,
                quantity=shares,
                order_type="MARKET",
                tag=f"{tag_prefix}_{setup.symbol[:6]}",
            )

            if order_id:
                # Ledger records the broker's actual average fill, not the
                # intended price — the pilot's fidelity measurement depends on
                # real slippage being visible (falls back to intended in paper)
                entry_fill = order_mgr.get_fill_price(order_id) or entry_price
                gtt_id = order_mgr.place_gtt_stop_loss(setup.symbol, stop_loss, shares)
                if gtt_id is None:
                    logger.warning("GTT placement failed for %s — retrying once", setup.symbol)
                    gtt_id = order_mgr.place_gtt_stop_loss(setup.symbol, stop_loss, shares)
                self._portfolio_manager.open_position(
                    symbol=setup.symbol,
                    entry_price=entry_fill,
                    shares=shares,
                    stop_loss=stop_loss,
                    gtt_order_id=gtt_id,
                    rs_score=setup.rs_percentile,
                    vcp_tightness=setup.vcp_tightness_score,
                    regime=self._regime_state.regime.value,
                    pivot_price=setup.pivot_price,
                    strategy=setup.strategy,
                    target_price=target_price,
                )
                self._watchlist.remove(setup)
                self._trades_today += 1

                if gtt_id is None and not config.PAPER_TRADING_MODE:
                    # Emergency: real shares held with no stop protection and
                    # both GTT attempts failed — flatten immediately rather
                    # than carry an unprotected overnight position
                    logger.critical(
                        "GTT placement failed twice for %s — flattening unprotected position",
                        setup.symbol,
                    )
                    from alerts.telegram_alert import send_system_error
                    send_system_error(
                        "Entry",
                        f"GTT stop-loss could not be placed for {setup.symbol} after 2 attempts. "
                        f"Flattening the position at market. If the flatten sell also fails, "
                        f"EXIT MANUALLY via Kite and clear data/manual_exit.txt.",
                    )
                    self._execute_exit(setup.symbol, entry_fill, "no_stop_protection")
                    continue

                position_value = shares * entry_fill
                from alerts.telegram_alert import send_trade_entry
                send_trade_entry(
                    symbol=setup.symbol,
                    entry_price=entry_fill,
                    stop_loss=stop_loss,
                    shares=shares,
                    position_value=position_value,
                    rs_score=setup.rs_percentile,
                    regime=self._regime_state.regime.value,
                    vcp_tightness=setup.vcp_tightness_score,
                    portfolio_value=portfolio_value,
                    strategy=strategy,
                )
                available_cash -= position_value

    def _restore_pivot_watch(self, setup: TradeSetup) -> None:
        """Put a consumed pivot back on the watchlist after a blocked entry.

        run_strategy_scan deletes a pivot from the watch the moment its
        breakout becomes a candidate; if the entry is then refused by a
        transient risk gate, the setup would be lost forever. Restoring it
        keeps the pivot actionable for a later valid breakout within a fresh
        watch window.
        """
        from strategy.pivot_watch import add_symbol, load_watch, save_watch
        watch = load_watch()
        if add_symbol(
            watch,
            symbol=setup.symbol,
            pivot=setup.pivot_price,
            vcp_low=setup.stop_loss_price,
            rs_percentile=setup.rs_percentile,
            tightness=setup.vcp_tightness_score,
            today=today_ist(),
        ):
            save_watch(watch)
            logger.info("Pivot for %s restored to watch after blocked entry", setup.symbol)

    def _process_manual_exits(self) -> None:
        """Exit positions listed in data/manual_exit.txt — the sanctioned
        manual override. Write one symbol per line (from any SSH/file editor);
        within 5 minutes the bot exits it properly: sell order, GTT cancel,
        ledger entry, Telegram alert. The file is cleared after processing so
        a symbol is acted on exactly once. Never sell bot positions from the
        Kite app directly — that desyncs the ledger and dangles the GTT.
        """
        path = Path(config.MANUAL_EXIT_PATH)
        if not path.exists():
            return
        requested = [
            line.split("#")[0].strip().upper()
            for line in path.read_text().splitlines()
        ]
        requested = [s for s in requested if s]
        if not requested:
            return

        from alerts.telegram_alert import send_text
        processed: list[str] = []
        skipped: list[str] = []
        for sym in dict.fromkeys(requested):  # dedupe, keep order
            pos = self._portfolio_manager.get_open_position(sym)
            if not pos:
                skipped.append(sym)
                continue
            df = self._universe_ohlcv.get(sym)
            price = (
                float(df["close"].iloc[-1])
                if df is not None and not df.empty
                else float(pos.get("current_price") or pos["entry_price"])
            )
            self._execute_exit(sym, price, "manual")
            processed.append(sym)

        path.write_text("")  # clear — each request is acted on exactly once
        summary = []
        if processed:
            summary.append(f"exited: {', '.join(processed)}")
        if skipped:
            summary.append(f"ignored (no open position): {', '.join(skipped)}")
        logger.info("Manual exit file processed — %s", "; ".join(summary))
        send_text(f"Manual exit request — {'; '.join(summary)}")

    def check_exits(self) -> None:
        """Check all open positions for exit conditions every 5 minutes.

        Prices come from the cached daily OHLCV (refreshed pre-market), so
        trailing stops, targets, and time stops evaluate on daily closes by
        design — matching the cadence the vcp_v2 backtest was validated on.
        Intraday hard-stop protection is the server-side GTT plus the
        GTTBackupMonitor, both of which act on live prices.
        """
        self._process_manual_exits()
        positions = self._portfolio_manager.get_open_positions()
        if not positions:
            return

        price_updates: dict[str, float] = {}
        for pos in positions:
            symbol = pos["symbol"]
            df = self._universe_ohlcv.get(symbol)
            if df is None or df.empty:
                continue

            current_price = float(df["close"].iloc[-1])
            price_updates[symbol] = current_price
            entry_price = float(pos["entry_price"])
            stop_loss = float(pos["stop_loss"])
            strategy = pos.get("strategy", "vcp")

            # ── Hard stop (both strategies) ──────────────────────────────
            if should_exit_on_stop(current_price, stop_loss):
                self._execute_exit(symbol, stop_loss, "stop_loss")
                continue

            # Normalise entry_date to a date object (may be stored as str in DB)
            entry_date = pos["entry_date"]
            if isinstance(entry_date, str):
                from datetime import date as _date
                entry_date = _date.fromisoformat(entry_date[:10])

            # ── Momentum-specific exits ───────────────────────────────────
            if strategy == "momentum":
                target = float(pos.get("target_price") or 0)
                if target > 0 and current_price >= target:
                    self._execute_exit(symbol, current_price, "profit_target")
                    continue
                holding_days = (today_ist() - entry_date).days
                if holding_days >= config.MOMENTUM_TIME_STOP_DAYS:
                    self._execute_exit(symbol, current_price, "time_stop_momentum")
                    continue
                # No trailing stop / partial profit for momentum — fixed target only
                continue

            # ── Breakout-specific exits ───────────────────────────────────
            if strategy == "breakout":
                target = float(pos.get("target_price") or 0)
                if target > 0 and current_price >= target:
                    self._execute_exit(symbol, current_price, "profit_target")
                    continue
                from config.holidays import trading_days_between
                days_held = trading_days_between(entry_date, today_ist())
                if days_held >= config.BREAKOUT_TIME_STOP_DAYS:
                    self._execute_exit(symbol, current_price, "time_stop_breakout")
                    continue
                continue

            # ── Mean reversion exits ──────────────────────────────────────
            if strategy == "mean_reversion":
                from strategy.mean_reversion import should_exit_mean_reversion
                from utils.indicator_utils import ema as _ema, rsi as _rsi
                close_series = df["close"]
                e5 = float(_ema(close_series, config.MR_EMA_EXIT).iloc[-1])
                rsi2 = float(_rsi(close_series, config.MR_RSI_PERIOD).iloc[-1])
                mr_exit, mr_reason = should_exit_mean_reversion(current_price, e5, rsi2)
                if mr_exit:
                    self._execute_exit(symbol, current_price, mr_reason)
                    continue
                from config.holidays import trading_days_between
                days_held = trading_days_between(entry_date, today_ist())
                if days_held >= config.MR_TIME_STOP_DAYS:
                    self._execute_exit(symbol, current_price, "time_stop_mr")
                    continue
                continue

            # ── VCP-specific exits ────────────────────────────────────────
            should_exit, reason = check_technical_breakdown(
                symbol, df, entry_price,
                vcp_consolidation_low=pos.get("vcp_low"),
            )
            if should_exit:
                self._execute_exit(symbol, current_price, reason)
                continue

            exit_time, reason = should_exit_on_time_stop(
                pos["entry_date"], today_ist(), entry_price, current_price
            )
            if exit_time:
                self._execute_exit(symbol, current_price, reason)
                continue

            if self._regime_state:
                from exit.time_stop import check_regime_exit
                should_regime_exit, reason = check_regime_exit(
                    symbol, self._regime_state, pos["entry_date"],
                    days_since_bear_start=self._days_in_bear,
                )
                if should_regime_exit:
                    self._execute_exit(symbol, current_price, reason)
                    continue

            if config.ENABLE_PARTIAL_PROFIT:
                total_shares = int(pos["shares"])
                partial_sold = int(pos.get("partial_sold_shares", 0))
                partial = check_partial_profit(entry_price, current_price, total_shares, partial_sold)
                if partial:
                    shares_to_sell, description = partial
                    self._execute_partial_exit(symbol, current_price, shares_to_sell, description)

            new_stop, reason = calculate_trailing_stop(
                entry_price=entry_price,
                current_price=current_price,
                current_stop=stop_loss,
                daily_ohlcv=df,
            )
            if new_stop > stop_loss and reason != "ratchet_hold":
                order_mgr = OrderManager(self._kite)
                old_gtt = pos.get("gtt_order_id")
                new_gtt = None
                if old_gtt and not config.PAPER_TRADING_MODE:
                    new_gtt = order_mgr.update_gtt_stop_loss(old_gtt, symbol, new_stop, total_shares - partial_sold)
                self._portfolio_manager.update_stop_loss(symbol, new_stop, new_gtt)
                from alerts.telegram_alert import send_trailing_stop_updated
                send_trailing_stop_updated(
                    symbol, stop_loss, new_stop,
                    (current_price - entry_price) / entry_price * 100,
                )

        self._portfolio_manager.update_current_prices(price_updates)

    def _execute_exit(self, symbol: str, price: float, reason: str) -> None:
        """Place sell order and record position closure."""
        pos = self._portfolio_manager.get_open_position(symbol)
        if not pos:
            return

        total_shares = int(pos["shares"]) - int(pos.get("partial_sold_shares", 0))
        order_mgr = OrderManager(self._kite)
        order_type = "MARKET" if "stop" in reason else "LIMIT"
        order_id = order_mgr.sell_cnc(
            symbol, total_shares,
            order_type=order_type,
            limit_price=price if order_type == "LIMIT" else 0.0,
        )

        if order_id is None:
            # Sell failed — the shares are still held. Keep the ledger open and
            # the GTT in place so the position stays tracked and protected.
            logger.critical(
                "Exit SELL failed for %s (%s) — position and GTT left unchanged",
                symbol, reason,
            )
            from alerts.telegram_alert import send_system_error
            send_system_error(
                "Exit",
                f"SELL order failed for {symbol} (reason: {reason}). Position remains "
                f"open and GTT-protected; bot will retry on the next exit check.",
            )
            return

        exit_fill = order_mgr.get_fill_price(order_id) or price
        trade = self._portfolio_manager.close_position(symbol, exit_fill, reason)
        if trade:
            # Full exit — cancel the now-dangling GTT stop-loss
            gtt_id = pos.get("gtt_order_id")
            if gtt_id:
                self._kite.delete_gtt(str(gtt_id))
            self._drawdown_monitor.record_trade_result(trade["realised_pnl"])
            portfolio_value = self._portfolio_manager.get_portfolio_value()
            from alerts.telegram_alert import send_trade_exit
            send_trade_exit(
                symbol=symbol,
                exit_price=exit_fill,
                entry_price=float(pos["entry_price"]),
                pnl_rupees=trade["realised_pnl"],
                pnl_pct=trade["realised_pnl_pct"],
                holding_days=trade.get("holding_days", 0),
                exit_reason=reason,
                portfolio_value=portfolio_value,
            )

    def _execute_partial_exit(self, symbol: str, price: float, shares: int, description: str) -> None:
        """Book partial profit on a position."""
        pos = self._portfolio_manager.get_open_position(symbol)
        if not pos:
            return
        order_mgr = OrderManager(self._kite)
        order_id = order_mgr.sell_cnc(symbol, shares, order_type="LIMIT",
                                      limit_price=price * 0.999)
        if order_id is None:
            # Sell failed — shares still held; leave the position untouched so
            # the booking is retried on the next exit check
            logger.critical(
                "Partial-profit SELL failed for %s — position left unchanged", symbol,
            )
            from alerts.telegram_alert import send_system_error
            send_system_error(
                "Exit",
                f"Partial-profit SELL failed for {symbol} ({shares} shares). "
                f"Position unchanged; will retry next cycle.",
            )
            return
        exit_fill = order_mgr.get_fill_price(order_id) or price
        trade = self._portfolio_manager.close_position(symbol, exit_fill, "partial_profit", shares_to_close=shares)
        if trade:
            from alerts.telegram_alert import send_partial_profit
            remaining = int(pos["shares"]) - int(pos.get("partial_sold_shares", 0)) - shares
            send_partial_profit(symbol, shares, exit_fill * shares, trade["realised_pnl_pct"], remaining)

    def send_heartbeat(self) -> None:
        """Record heartbeat; alert if dead man's switch condition detected."""
        record_heartbeat()

    def market_close(self) -> None:
        """End-of-day cleanup: cancel pending orders."""
        if config.PAPER_TRADING_MODE:
            return
        orders = self._kite.get_orders()
        for order in orders:
            if order.get("status") in ("OPEN", "TRIGGER PENDING"):
                self._kite.cancel_order(order["order_id"])
        logger.info("Market close: pending orders cancelled")

    def generate_eod_report(self) -> None:
        """Generate and send end-of-day report."""
        snap = self._portfolio_tracker.snapshot()
        regime_str = self._regime_state.regime.value if self._regime_state else "UNKNOWN"
        generate_daily_report(
            portfolio_value=snap["portfolio_value"],
            day_pnl=snap.get("unrealised_pnl", 0),
            day_pnl_pct=snap.get("unrealised_pnl", 0) / snap["portfolio_value"] * 100,
            open_positions=snap["open_positions_count"],
            cash_pct=snap["cash_pct"] * 100,
            regime=regime_str,
            trades_today=self._trades_today,
        )
        self._trades_today = 0
        self._portfolio_tracker.record_daily_metrics(regime=regime_str)
        self._check_inactivity_warning()

    def _check_inactivity_warning(self) -> None:
        """Warn via Telegram if account is approaching 30-day inactivity threshold.

        Zerodha transfers unused cash monthly if no trade placed in 30 days (SEBI rule).
        Alert at day 25 so user has 5 days to decide: re-deposit plan or place a trade.
        Only fires when there are no open positions (otherwise account is clearly active).
        """
        open_positions = self._portfolio_manager.get_open_positions()
        if open_positions:
            return  # live positions = account is active, no risk of sweep

        days = self._portfolio_manager.get_days_since_last_trade()
        if days >= 25:
            cash = self._portfolio_manager.get_available_cash()
            from alerts.telegram_alert import send_inactivity_warning
            send_inactivity_warning(days_since_trade=days, cash_in_zerodha=cash)
            logger.warning("Inactivity warning sent: %d days since last trade", days)

    def generate_weekly_report(self) -> None:
        from reports.weekly_report import generate_weekly_report
        generate_weekly_report(self._portfolio_tracker)

    def backup_databases(self) -> None:
        backup_databases()

    def refresh_universe_snapshot(self) -> None:
        """Fetch current Nifty 200 constituents from NSE and save to DB."""
        from data_fetcher.universe_fetcher import fetch_universe_symbols, save_universe_snapshot
        symbols = fetch_universe_symbols()
        save_universe_snapshot(symbols)
        logger.info("Universe snapshot refreshed: %d symbols", len(symbols))

    def reconcile_holdings(self) -> None:
        """Daily-scheduled wrapper: reconcile DB positions against Kite holdings."""
        if config.PAPER_TRADING_MODE:
            return
        self._reconcile_holdings()

    def _reconcile_holdings(self) -> None:
        """Close/adjust DB positions that diverge from Kite holdings.

        Zerodha holds the authoritative truth, local DB is secondary. Covers:
        GTT fired offline, full manual sells (position gone), and partial
        manual sells (Kite quantity below DB shares). Unsettled T+1 shares
        count as held — Kite reports them in t1_quantity, not quantity.
        Dangling GTTs are cancelled on full closes and resized on partials.
        """
        from alerts.telegram_alert import send_text

        kite_holdings = self._kite.get_holdings()
        kite_qty = {
            h["tradingsymbol"].upper(): int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
            for h in kite_holdings
        }
        local_positions = self._portfolio_manager.get_open_positions()
        order_mgr = OrderManager(self._kite)

        for pos in local_positions:
            sym = pos["symbol"].upper()
            db_remaining = int(pos["shares"]) - int(pos.get("partial_sold_shares", 0))
            held = kite_qty.get(sym, 0)
            if held >= db_remaining:
                continue  # holdings cover the position — nothing to do

            exit_price = float(pos.get("current_price") or pos["entry_price"])
            gtt_id = pos.get("gtt_order_id")

            if held <= 0:
                logger.warning(
                    "Reconcile: %s in local DB but not in Kite holdings — "
                    "closed offline (GTT or manual). Marking closed.", sym,
                )
                self._portfolio_manager.close_position(sym, exit_price, "closed_offline")
                if gtt_id:
                    self._kite.delete_gtt(str(gtt_id))
                send_text(
                    f"Reconcile: {sym} was closed offline (GTT or manual sell). "
                    f"Position removed from bot tracking; GTT cancelled."
                )
            else:
                sold = db_remaining - held
                logger.warning(
                    "Reconcile: %s partially sold offline — DB %d vs Kite %d. "
                    "Booking %d shares as manual close.", sym, db_remaining, held, sold,
                )
                self._portfolio_manager.close_position(
                    sym, exit_price, "closed_offline_partial", shares_to_close=sold
                )
                if gtt_id:
                    new_gtt = order_mgr.update_gtt_stop_loss(
                        str(gtt_id), sym, float(pos["stop_loss"]), held
                    )
                    if new_gtt:
                        self._portfolio_manager.update_stop_loss(sym, float(pos["stop_loss"]), new_gtt)
                send_text(
                    f"Reconcile: {sym} partially sold offline ({sold} of {db_remaining} shares). "
                    f"Bot now tracks {held} shares; GTT resized."
                )

        # Reverse direction: a failed exit that still closed the ledger leaves
        # real shares held with no DB record and no GTT. The forward loop above
        # only iterates open DB positions, so it can never see these orphans.
        # Personal holdings are never bot-traded, so any recently-closed bot
        # symbol still in Kite holdings is a genuine anomaly. Alert-only —
        # selling is a human decision here.
        open_syms = {p["symbol"].upper() for p in local_positions}
        recent = self._portfolio_manager.get_recently_closed_trades(days=7)
        orphans = sorted({
            t["symbol"].upper() for t in recent
            if t["symbol"].upper() not in open_syms
            and kite_qty.get(t["symbol"].upper(), 0) > 0
        })
        if orphans:
            logger.critical(
                "Reconcile: closed in bot ledger but still held at Kite: %s", orphans,
            )
            send_text(
                f"⚠️ Reconcile: {', '.join(orphans)} recently closed in the bot ledger "
                f"but still held at Zerodha — an exit sell may have failed. "
                f"Check Kite and sell manually if needed."
            )

        if not local_positions:
            logger.info("Reconcile: no open positions in DB — nothing to check")
        else:
            logger.info(
                "Reconcile complete: %d DB position(s) checked against %d Kite holding(s)",
                len(local_positions), len(kite_qty),
            )

    def _load_instrument_map(self) -> None:
        """Load NSE instrument master into memory."""
        if self._kite.is_connected:
            instruments = self._kite.get_instruments("NSE")
            self._instrument_map = {
                inst["tradingsymbol"]: inst
                for inst in instruments
                if inst.get("instrument_type") == "EQ"
            }
            # Also capture NIFTY 50 index token for RS / regime calculations
            for inst in instruments:
                if inst.get("tradingsymbol") == "NIFTY 50":
                    self._instrument_map["NIFTY 50"] = inst
                    break
            logger.info("Loaded %d NSE instruments", len(self._instrument_map))

    def _get_nifty_ohlcv(self):
        """Return Nifty 50 OHLCV for regime and RS calculations.

        Uses Kite as primary source (nsepy is unreliable for indices).
        Falls back to local cache if Kite is unavailable.
        """
        if self._nifty_ohlcv is not None:
            return self._nifty_ohlcv

        # Primary: fetch from Kite using the NIFTY 50 index instrument token
        if self._kite.is_connected:
            nifty_inst = self._instrument_map.get("NIFTY 50")
            token = nifty_inst.get("instrument_token") if nifty_inst else 256265
            df = self._ohlcv_fetcher.get_ohlcv("NIFTY50", token, days=config.HISTORICAL_DAYS)
            if df is not None:
                self._nifty_ohlcv = df
                return df

        # Fallback: SQLite cache (populated from prior runs)
        from backtest.data_loader import load_nifty_ohlcv
        from datetime import date, timedelta
        df = load_nifty_ohlcv(date.today() - timedelta(days=config.HISTORICAL_DAYS), date.today())
        if df is not None:
            self._nifty_ohlcv = df
        return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated Swing Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Force paper trading mode")
    args = parser.parse_args()

    if args.paper:
        config.PAPER_TRADING_MODE = True

    mode = "PAPER" if config.PAPER_TRADING_MODE else "LIVE"
    logger.info("=" * 60)
    logger.info("Starting trading bot in %s mode", mode)
    logger.info("=" * 60)

    if config.PAPER_TRADING_MODE:
        send_text(f"[PAPER] Trading bot started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    else:
        send_text(f"Trading bot started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    bot = TradingBot()
    bot.startup()

    # Start health check HTTP server
    start_http_health_server(port=8081)

    # Setup APScheduler
    from scheduler import build_scheduler
    scheduler = build_scheduler(bot)
    scheduler.start()
    logger.info("Scheduler started")

    # Graceful shutdown handler
    def handle_shutdown(signum, frame):
        logger.info("Shutdown signal received")
        scheduler.shutdown(wait=False)
        send_text(f"{'[PAPER] ' if config.PAPER_TRADING_MODE else ''}Trading bot shutdown")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Main monitoring loop
    logger.info("Bot running. Press Ctrl+C to stop.")
    while True:
        try:
            record_heartbeat()
            time.sleep(60)
        except KeyboardInterrupt:
            handle_shutdown(None, None)
        except Exception as exc:
            logger.exception("Unhandled exception in main loop: %s", exc)
            send_system_error("main", str(exc)[:200])
            time.sleep(30)


if __name__ == "__main__":
    main()
