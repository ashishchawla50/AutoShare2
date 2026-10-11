"""Event-driven backtest engine.

Simulates the complete trading system on historical data:
- Point-in-time universe (survivorship bias corrected)
- All 9 stock filters applied on each day
- Stage 2 + Trend Template + VCP + Breakout signal generation
- Position sizing with risk management
- Trailing stops, partial profit booking, time stops
- Realistic cost model

Usage:
    python backtest/backtest_engine.py --start 2023-01-01 --end 2024-01-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.cost_model import COST_PROFILES, TradeCosts, calculate_buy_cost, calculate_sell_cost, calculate_net_pnl
from backtest.data_loader import load_nifty_ohlcv, load_ohlcv
from backtest.performance_analyzer import BacktestMetrics, analyse, print_report
from backtest.point_in_time_universe import PointInTimeUniverse
from backtest.regime_sim import build_regime_series
from config.config import config
from config.holidays import get_trading_days, is_trading_day
from exit.profit_manager import PROFIT_TIERS, check_partial_profit, check_technical_breakdown, should_exit_on_time_stop
from exit.stop_loss_manager import calculate_hard_stop, calculate_trailing_stop, should_exit_on_stop
from risk.position_sizer import PositionSizeResult, calculate_position_size, calculate_stop_loss_price
from scanner.relative_strength import calculate_rs_universe
from scanner.stock_filter import apply_all_filters
from strategy.breakout_detector import get_avg_volume_50d, is_valid_breakout
from strategy.breakout_scanner import BreakoutScanResult, check_range_breakout
from strategy.mean_reversion import MeanReversionResult, check_mean_reversion, should_exit_mean_reversion
from strategy.momentum_strategy import MomentumResult, check_momentum
from strategy.multi_timeframe import MultiTimeframeResult, analyse_stock
from utils.logger import setup_logging

logger = logging.getLogger("backtest.engine")

# Valid strategy identifiers
VALID_STRATEGIES = {"vcp", "vcp_v2", "momentum", "momentum_v2", "momentum_v3", "momentum_v4", "breakout", "breakout_v2", "mean_reversion", "all"}


class BacktestEngine:
    """Event-driven backtest that replays the strategy on historical data.

    Args:
        start_date:       Backtest start date.
        end_date:         Backtest end date.
        starting_capital: Initial capital in rupees.
        strategy:         One of "vcp", "momentum", "breakout", "mean_reversion", "all".
                          "all" runs breakout + momentum + mean_reversion in a shared portfolio.
    """

    def __init__(
        self,
        start_date: date,
        end_date: date,
        starting_capital: float = 500_000.0,
        strategy: str = "vcp",
        events_filter: bool = False,
        dd_halt: bool = True,
        cost_profile: str = "legacy",
    ) -> None:
        # Absorbing 20% drawdown halt; see _simulate_day. Disable only for
        # diagnostic runs that need the full window measured.
        self._dd_halt = dd_halt
        self._dd_halted_on: Optional[date] = None
        self.start_date = start_date
        self.end_date = end_date
        self.capital = starting_capital       # current cash balance — changes with buys/sells
        self.starting_capital = starting_capital  # immutable reference for reporting
        self.strategy = strategy
        # Corporate-events pond exclusion — mirrors live filter_corporate_events
        # (results ±7 days, dividend/bonus/split ex-date within next 3 days).
        # Off by default: the original validation ran without it; the on/off
        # delta is what decides whether live keeps the filter.
        self._events_filter = events_filter
        self._events_by_symbol: dict[str, list[tuple[date, str]]] = {}
        self._pit_universe = PointInTimeUniverse()
        self._costs = COST_PROFILES[cost_profile]
        self._cost_profile = cost_profile
        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._open_positions: dict[str, dict] = {}   # symbol → position dict
        self._pending_pivots: dict[str, dict] = {}   # vcp_v2: symbol → pivot watch entry
        self._nifty_close: pd.Series = pd.Series(dtype=float)  # momentum_v3 RS benchmark
        self._closed_trades: list[dict] = []
        self._equity_curve: dict[date, float] = {}
        self._peak_value: float = starting_capital
        # Daily regime gate — mirrors the live bot: BEAR blocks entries,
        # CAUTIOUS halves sizes, upgrades need 2-day confirmation. Built in
        # _preload_data from historical Nifty + breadth (VIX-less; see
        # backtest/regime_sim.py for the documented differences vs live).
        self._regime_by_day: dict[date, tuple[str, float]] = {}
        # Cumulative scan counters — printed at end to diagnose 0-trade runs.
        self._stats = {
            "data_ok": 0, "analyse_called": 0,
            "regime_blocked_days": 0,   # days where BEAR regime blocked all entries
            # VCP path kill-rate by gate
            "fail_tt": 0,          # killed by Minervini Trend Template
            "fail_vcp": 0,         # killed by VCP pattern check
            "fail_tightness": 0,   # killed by tightness score < 50 (hard gate in multi_timeframe.py)
            "fail_breakout_confirm": 0,  # VCP setup valid but prior day was not a pivot breakout
            "fail_other": 0,       # killed by something else in analyse_stock()
            "pivot_watch_added": 0,   # vcp_v2: setups placed on the pivot watchlist
            "pivot_watch_expired": 0, # vcp_v2: watches that expired without a breakout
            "mom3_fail_stage2": 0,    # momentum_v3: trend structure gate failed
            "mom3_fail_rs": 0,        # momentum_v3: relative strength vs Nifty failed
            # Momentum path kill-rate by condition
            "mom_fail_ema": 0,     # price <= EMA21 or EMA21 <= EMA50
            "mom_fail_volume": 0,  # volume < 1.5x 20d avg
            "mom_fail_proximity": 0,  # price > 12% below 20d high
            "mom_fail_rsi": 0,     # RSI outside [45, 72]
            "mom_fail_other": 0,
            # Breakout strategy kill-rate
            "bo_fail_new_high": 0,    # price not at N-day high
            "bo_fail_volume": 0,      # volume too low
            "bo_fail_ema": 0,         # below 50 EMA
            "bo_fail_rsi": 0,         # RSI out of range
            "bo_fail_other": 0,
            # Mean reversion kill-rate
            "mr_fail_trend_gate": 0,  # close <= 200-day EMA
            "mr_fail_rsi_entry": 0,   # RSI(2) >= 10 (not oversold enough)
            "mr_fail_other": 0,
            "event_filtered": 0,   # symbol-days excluded by the corporate-events filter
            "dd_halted_days": 0,   # days at/over the 20% drawdown halt threshold
            "high_conf": 0, "gap_filtered": 0, "size_invalid": 0, "entered": 0,
        }

    def run(self) -> BacktestMetrics:
        """Run the full backtest and return performance metrics."""
        trading_days = get_trading_days(self.start_date, self.end_date)
        logger.info(
            "Backtest: %s → %s (%d trading days) | capital ₹%s | cost profile %s | "
            "dd_halt %s | floor ₹%.0f | events_filter %s",
            self.start_date, self.end_date, len(trading_days), f"{self.starting_capital:,.0f}",
            self._cost_profile, "on" if self._dd_halt else "OFF",
            config.MIN_POSITION_VALUE, "on" if self._events_filter else "off",
        )

        # Pre-load all needed OHLCV data
        self._preload_data(trading_days)
        if self._events_filter:
            self._preload_events()

        for sim_date in trading_days:
            self._simulate_day(sim_date)

        # Force-close any remaining positions at last available price
        self._close_all_positions(trading_days[-1])

        # ── Ledger invariant ────────────────────────────────────────────────
        # sum(realised_pnl) must equal capital - starting_capital.
        # Entry deducts (pos_value + buy_cost); exit credits (sell_value - sell_cost).
        # Net = sell_value - sell_cost - pos_value - buy_cost = net_pnl per trade.
        total_realised_pnl = sum(t["realised_pnl"] for t in self._closed_trades)
        capital_change = self.capital - self.starting_capital
        ledger_error = abs(total_realised_pnl - capital_change)
        logger.info(
            "Ledger invariant: sum_pnl=%.2f  capital_change=%.2f  error=%.2f",
            total_realised_pnl, capital_change, ledger_error,
        )
        assert ledger_error < 5.0, (
            f"LEDGER MISMATCH: sum_pnl={total_realised_pnl:.2f} "
            f"capital_change={capital_change:.2f} err={ledger_error:.2f} — "
            "check that entry deducts buy cost and exit credits sell_value - sell_cost"
        )

        equity_series = pd.Series(self._equity_curve)
        equity_series.index = pd.to_datetime(list(self._equity_curve.keys()))
        equity_series = equity_series.sort_index()

        metrics = analyse(
            trades=self._closed_trades,
            equity_curve=equity_series,
            starting_capital=self.starting_capital,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        print_report(metrics)

        self._log_kill_rates()

        return metrics

    def _log_kill_rates(self) -> None:
        """Log scan pipeline kill-rate totals for diagnostics."""
        s = self._stats
        if self.strategy in ("momentum", "momentum_v2", "momentum_v3", "momentum_v4"):
            logger.warning(
                "Scan pipeline totals  data_ok=%d  analysed=%d | "
                "momentum killed: ema=%d  volume=%d  proximity=%d  rsi=%d  other=%d | "
                "v3 gates killed: stage2=%d  rs=%d | "
                "high_conf=%d  gap_filtered=%d  size_invalid=%d  entered=%d",
                s["data_ok"], s["analyse_called"],
                s["mom_fail_ema"], s["mom_fail_volume"], s["mom_fail_proximity"],
                s["mom_fail_rsi"], s["mom_fail_other"],
                s["mom3_fail_stage2"], s["mom3_fail_rs"],
                s["high_conf"], s["gap_filtered"], s["size_invalid"], s["entered"],
            )
        elif self.strategy == "breakout":
            logger.warning(
                "Scan pipeline totals  data_ok=%d  analysed=%d | "
                "breakout killed: new_high=%d  volume=%d  ema=%d  rsi=%d  other=%d | "
                "high_conf=%d  gap_filtered=%d  size_invalid=%d  entered=%d",
                s["data_ok"], s["analyse_called"],
                s["bo_fail_new_high"], s["bo_fail_volume"], s["bo_fail_ema"],
                s["bo_fail_rsi"], s["bo_fail_other"],
                s["high_conf"], s["gap_filtered"], s["size_invalid"], s["entered"],
            )
        elif self.strategy == "mean_reversion":
            logger.warning(
                "Scan pipeline totals  data_ok=%d  analysed=%d | "
                "mr killed: trend_gate=%d  rsi_entry=%d  other=%d | "
                "high_conf=%d  gap_filtered=%d  size_invalid=%d  entered=%d",
                s["data_ok"], s["analyse_called"],
                s["mr_fail_trend_gate"], s["mr_fail_rsi_entry"], s["mr_fail_other"],
                s["high_conf"], s["gap_filtered"], s["size_invalid"], s["entered"],
            )
        elif self.strategy == "all":
            logger.warning(
                "Scan pipeline totals  data_ok=%d  analysed=%d | "
                "mr killed: trend_gate=%d  rsi_entry=%d  other=%d | "
                "momentum killed: ema=%d  volume=%d  proximity=%d  rsi=%d | "
                "breakout killed: new_high=%d  volume=%d  ema=%d  rsi=%d | "
                "high_conf=%d  gap_filtered=%d  size_invalid=%d  entered=%d",
                s["data_ok"], s["analyse_called"],
                s["mr_fail_trend_gate"], s["mr_fail_rsi_entry"], s["mr_fail_other"],
                s["mom_fail_ema"], s["mom_fail_volume"], s["mom_fail_proximity"], s["mom_fail_rsi"],
                s["bo_fail_new_high"], s["bo_fail_volume"], s["bo_fail_ema"], s["bo_fail_rsi"],
                s["high_conf"], s["gap_filtered"], s["size_invalid"], s["entered"],
            )
        else:
            logger.warning(
                "Scan pipeline totals  data_ok=%d  analysed=%d | "
                "vcp killed: tt=%d  vcp=%d  tightness=%d  breakout_confirm=%d  other=%d | "
                "pivot_watch: added=%d  expired=%d | "
                "event_filtered=%d  high_conf=%d  gap_filtered=%d  size_invalid=%d  entered=%d",
                s["data_ok"], s["analyse_called"],
                s["fail_tt"], s["fail_vcp"], s["fail_tightness"],
                s["fail_breakout_confirm"], s["fail_other"],
                s["pivot_watch_added"], s["pivot_watch_expired"],
                s["event_filtered"], s["high_conf"], s["gap_filtered"], s["size_invalid"], s["entered"],
            )
        if self._regime_by_day:
            logger.warning(
                "Regime gate: %d of %d days blocked (BEAR)",
                s["regime_blocked_days"], len(self._regime_by_day),
            )
        if s["dd_halted_days"]:
            logger.warning(
                "Drawdown halt: %d days at/over %.0f%% from peak, first on %s (halt %s). "
                "Regime-blocked count is understated when the halt is ON — it returns first.",
                s["dd_halted_days"], config.MAX_DRAWDOWN_PCT * 100,
                self._dd_halted_on, "ENFORCED" if self._dd_halt else "DISABLED (diagnostic)",
            )
        if s["entered"] == 0:
            logger.warning("DIAGNOSIS: %d high-confidence signals found but none entered"
                           " — gap filter or position sizer is the bottleneck.", s["high_conf"])

    def _simulate_day(self, sim_date: date) -> None:
        """Simulate one trading day: exits first, then entries."""
        # ── 1. Update existing positions ──────────────────────────────────
        self._process_exits(sim_date)

        # ── 2. Calculate current portfolio value ──────────────────────────
        portfolio_value = self._calc_portfolio_value(sim_date)
        self._equity_curve[sim_date] = portfolio_value
        self._peak_value = max(self._peak_value, portfolio_value)

        # ── 3. Check drawdown halt ─────────────────────────────────────────
        # NOTE: this halt is ABSORBING. It blocks new entries, so equity cannot
        # grow, so drawdown-from-peak never shrinks, so it never lifts — every
        # remaining day of the run is dead. The original ₹5L validation never
        # tripped it (max DD -19.0% dev / -4.4% OOS), but a ₹50k run trips it
        # in Feb 2022 and the remaining ~3 years produce no trades, which makes
        # the window untestable rather than merely unprofitable. Pass
        # dd_halt=False to measure the underlying edge across the full window;
        # the halt-on run then reports the operational outcome separately.
        drawdown = (self._peak_value - portfolio_value) / self._peak_value
        if drawdown >= config.MAX_DRAWDOWN_PCT:
            if not self._dd_halted_on:
                self._dd_halted_on = sim_date
                logger.warning(
                    "Backtest drawdown halt FIRST triggered on %s: %.1f%% "
                    "(halt is absorbing — no further entries this run)",
                    sim_date, drawdown * 100,
                )
            self._stats["dd_halted_days"] += 1
            if self._dd_halt:
                return

        # ── 4. Scan for new entries — gated by the day's regime ───────────
        regime, regime_mult = self._regime_by_day.get(sim_date, ("BULL", 1.0))
        if regime_mult <= 0.0:
            self._stats["regime_blocked_days"] += 1
            return
        if len(self._open_positions) < config.MAX_POSITIONS:
            self._scan_for_entries(sim_date, regime_mult, regime)

    def _process_exits(self, sim_date: date) -> None:
        """Check all open positions for exit conditions."""
        to_close: list[tuple[str, float, str]] = []

        for symbol, pos in list(self._open_positions.items()):
            df = self._ohlcv_cache.get(symbol)
            if df is None:
                continue

            sim_df = df[df.index.date <= sim_date]  # type: ignore[attr-defined]
            if sim_df.empty:
                continue

            today_row = sim_df.iloc[-1]
            current_price = float(today_row["close"])
            current_low = float(today_row["low"])
            strategy = pos.get("strategy", "vcp")
            target_price = pos.get("target_price", 0.0)

            # Hard stop: check if low touched the stop (intraday).
            # A gap through the stop fills at the open, not at the stop —
            # assuming the stop price on a gap-down is optimistic fiction.
            if current_low <= pos["stop_loss"]:
                today_open = float(today_row["open"])
                fill = min(pos["stop_loss"], today_open) if today_open > 0 else pos["stop_loss"]
                to_close.append((symbol, fill, "stop_loss"))
                continue

            # Profit target (breakout and momentum use fixed targets)
            if target_price > 0 and current_price >= target_price:
                to_close.append((symbol, target_price, "profit_target"))
                continue

            # Trailing stop update (VCP and breakout_v2 use trailing stop ratchet)
            if strategy in ("vcp", "vcp_v2", "breakout_v2"):
                new_stop, _ = calculate_trailing_stop(
                    entry_price=pos["entry_price"],
                    current_price=current_price,
                    current_stop=pos["stop_loss"],
                    daily_ohlcv=sim_df,
                )
                if new_stop > pos["stop_loss"]:
                    pos["stop_loss"] = new_stop

                # Technical breakdown exit
                should_exit, reason = check_technical_breakdown(
                    symbol=symbol,
                    daily_ohlcv=sim_df,
                    entry_price=pos["entry_price"],
                    vcp_consolidation_low=pos.get("vcp_low"),
                )
                if should_exit:
                    to_close.append((symbol, current_price, reason))
                    continue

            # Mean reversion: EMA5 / RSI(2) exits + 7-day time stop
            if strategy == "mean_reversion":
                from utils.indicator_utils import ema as _ema, rsi as _rsi
                close_series = sim_df["close"]
                e5 = float(_ema(close_series, config.MR_EMA_EXIT).iloc[-1])
                rsi2 = float(_rsi(close_series, config.MR_RSI_PERIOD).iloc[-1])
                mr_exit, mr_reason = should_exit_mean_reversion(current_price, e5, rsi2)
                if mr_exit:
                    to_close.append((symbol, current_price, mr_reason))
                    continue
                from config.holidays import trading_days_between
                days_held = trading_days_between(pos["entry_date"], sim_date)
                if days_held >= config.MR_TIME_STOP_DAYS:
                    to_close.append((symbol, current_price, "time_stop"))
                    continue
            # Momentum: 12-day unconditional time stop (fixed target drives wins, not time)
            elif strategy == "momentum":
                from config.holidays import trading_days_between
                days_held = trading_days_between(pos["entry_date"], sim_date)
                if days_held >= config.MOMENTUM_TIME_STOP_DAYS:
                    to_close.append((symbol, current_price, "time_stop"))
                    continue
            # Momentum v2/v3/v4: trend-break exit (close below EMA21) + 25-day time stop
            elif strategy in ("momentum_v2", "momentum_v3", "momentum_v4"):
                from utils.indicator_utils import ema as _ema
                ema_exit = float(_ema(sim_df["close"], config.MOMENTUM2_EXIT_EMA).iloc[-1])
                if current_price < ema_exit:
                    to_close.append((symbol, current_price, "trend_break"))
                    continue
                from config.holidays import trading_days_between
                days_held = trading_days_between(pos["entry_date"], sim_date)
                if days_held >= config.MOMENTUM2_TIME_STOP_DAYS:
                    to_close.append((symbol, current_price, "time_stop"))
                    continue
            # Breakout: 7-day time stop
            elif strategy == "breakout":
                from config.holidays import trading_days_between
                days_held = trading_days_between(pos["entry_date"], sim_date)
                if days_held >= config.BREAKOUT_TIME_STOP_DAYS:
                    to_close.append((symbol, current_price, "time_stop"))
                    continue
            # VCP: 15-day time stop with min-profit gate (original Minervini rule)
            else:
                exit_time, reason = should_exit_on_time_stop(
                    entry_date=pos["entry_date"],
                    current_date=sim_date,
                    entry_price=pos["entry_price"],
                    current_price=current_price,
                )
                if exit_time:
                    to_close.append((symbol, current_price, reason))
                    continue

        for symbol, exit_price, reason in to_close:
            self._close_position(symbol, exit_price, sim_date, reason)

    def _scan_for_entries(self, sim_date: date, regime_mult: float = 1.0, regime: str = "BULL") -> None:
        """Scan universe for new entry signals.

        Args:
            sim_date:    Trading day being simulated.
            regime_mult: Regime position-size multiplier (0.5 in CAUTIOUS, 1.0 in BULL).
            regime:      Regime name for strategies with per-regime entry gates.
        """
        # momentum_v4: trend-following entries only in a confirmed uptrend.
        # v3's residual bleed was concentrated in 2022's CAUTIOUS chop.
        if self.strategy == "momentum_v4" and regime != "BULL":
            return
        universe = self._pit_universe.get_universe(sim_date)
        available_cash = self._calc_available_cash()

        for symbol in universe:
            if symbol in self._open_positions:
                continue
            if len(self._open_positions) >= config.MAX_POSITIONS:
                break

            # Corporate-events pond exclusion (mirrors live: an excluded symbol
            # is neither scanned for a new pivot watch nor checked for a
            # breakout through an existing one; the watch itself stays alive)
            if self._events_filter and self._event_blocked(symbol, sim_date):
                self._stats["event_filtered"] += 1
                continue

            df = self._ohlcv_cache.get(symbol)
            if df is None or len(df) < 210:
                continue

            # Use data up to and including sim_date
            sim_df = df[df.index.date <= sim_date]  # type: ignore[attr-defined]
            if len(sim_df) < 210:
                continue
            self._stats["data_ok"] += 1

            # Scan using prior-day close data (matches live bot's evening scan).
            # Enter at today's open — the live bot places orders at market open
            # the morning after the scan, not at the previous night's close.
            prev_day_df = sim_df.iloc[:-1]
            today_row = sim_df.iloc[-1].to_dict()

            self._stats["analyse_called"] += 1

            # ── Strategy-specific entry signal ────────────────────────────
            # Sets: chosen_strategy, entry_price, stop_loss, target_price, vcp_low
            chosen_strategy = self.strategy  # overridden for "all" mode
            entry_price: float = 0.0
            stop_loss: float = 0.0
            target_price: float = 0.0
            vcp_low = None

            if self.strategy == "momentum":
                m = check_momentum(symbol, prev_day_df)
                if not m.passes:
                    self._record_mom_fail(m.failure_reason)
                    continue
                mom_open = sum(1 for p in self._open_positions.values() if p.get("strategy") == "momentum")
                if mom_open >= config.MOMENTUM_MAX_POSITIONS:
                    continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = entry_price * (1 - config.MOMENTUM_STOP_PCT)
                target_price = entry_price * (1 + config.MOMENTUM_TARGET_PCT)

            elif self.strategy == "momentum_v2":
                # v1 entries, v2 exit geometry: wide stop, no fixed target
                # (EMA21 trend-break exit handled in _process_exits)
                m = check_momentum(symbol, prev_day_df)
                if not m.passes:
                    self._record_mom_fail(m.failure_reason)
                    continue
                mom_open = sum(1 for p in self._open_positions.values() if p.get("strategy") == "momentum_v2")
                if mom_open >= config.MOMENTUM_MAX_POSITIONS:
                    continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = entry_price * (1 - config.MOMENTUM2_STOP_PCT)
                target_price = 0.0

            elif self.strategy in ("momentum_v3", "momentum_v4"):
                # v2 exits + entry-quality gate: stage-2 trend structure and
                # relative strength vs Nifty — only buy leaders in real uptrends
                m = check_momentum(symbol, prev_day_df)
                if not m.passes:
                    self._record_mom_fail(m.failure_reason)
                    continue
                from utils.indicator_utils import ema as _ema
                closes = prev_day_df["close"].astype(float)
                ema50 = _ema(closes, 50)
                ema200 = _ema(closes, 200)
                stage2 = (
                    float(closes.iloc[-1]) > float(ema200.iloc[-1])
                    and float(ema50.iloc[-1]) > float(ema200.iloc[-1])
                    and float(ema200.iloc[-1]) > float(ema200.iloc[-config.MOMENTUM3_EMA200_RISE_DAYS])
                )
                if not stage2:
                    self._stats["mom3_fail_stage2"] += 1
                    continue
                lb = config.MOMENTUM3_RS_LOOKBACK
                nifty_sim = self._nifty_close[self._nifty_close.index.date < sim_date]  # type: ignore[attr-defined]
                if len(closes) <= lb or len(nifty_sim) <= lb:
                    self._stats["mom3_fail_rs"] += 1
                    continue
                stock_ret = float(closes.iloc[-1]) / float(closes.iloc[-lb - 1]) - 1
                nifty_ret = float(nifty_sim.iloc[-1]) / float(nifty_sim.iloc[-lb - 1]) - 1
                if stock_ret <= nifty_ret:
                    self._stats["mom3_fail_rs"] += 1
                    continue
                mom_open = sum(1 for p in self._open_positions.values() if p.get("strategy") == self.strategy)
                if mom_open >= config.MOMENTUM_MAX_POSITIONS:
                    continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = entry_price * (1 - config.MOMENTUM2_STOP_PCT)
                target_price = 0.0

            elif self.strategy in ("breakout", "breakout_v2"):
                b = check_range_breakout(symbol, prev_day_df)
                if not b.passes:
                    self._record_bo_fail(b.failure_reason)
                    continue
                bo_open = sum(
                    1 for p in self._open_positions.values()
                    if p.get("strategy") in ("breakout", "breakout_v2")
                )
                if bo_open >= config.BREAKOUT_MAX_POSITIONS:
                    continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = entry_price * (1 - config.BREAKOUT_STOP_PCT)
                if self.strategy == "breakout_v2":
                    target_price = 0.0  # Trailing stop ratchet exit, no fixed target
                else:
                    target_price = entry_price * (1 + config.BREAKOUT_TARGET_PCT)

            elif self.strategy == "mean_reversion":
                mr = check_mean_reversion(symbol, prev_day_df)
                if not mr.passes:
                    self._record_mr_fail(mr.failure_reason)
                    continue
                mr_open = sum(
                    1 for p in self._open_positions.values()
                    if p.get("strategy") == "mean_reversion"
                )
                if mr_open >= config.MR_MAX_POSITIONS:
                    continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = entry_price * (1 - config.MR_STOP_PCT)
                target_price = 0.0  # MR uses EMA5/RSI2 dynamic exit

            elif self.strategy == "all":
                # Run all three non-VCP strategies; pick the highest-scoring passing signal.
                # Per-strategy slot caps are enforced independently so each strategy can
                # still fire even when another cap is full.
                candidates: list[dict] = []
                ep = float(today_row["open"])

                mr_open = sum(1 for p in self._open_positions.values() if p.get("strategy") == "mean_reversion")
                if mr_open < config.MR_MAX_POSITIONS:
                    mr = check_mean_reversion(symbol, prev_day_df)
                    if mr.passes:
                        candidates.append({
                            "score": mr.score,
                            "strategy": "mean_reversion",
                            "entry_price": ep,
                            "stop_loss": ep * (1 - config.MR_STOP_PCT),
                            "target_price": 0.0,
                        })
                    else:
                        self._record_mr_fail(mr.failure_reason)

                mom_open = sum(1 for p in self._open_positions.values() if p.get("strategy") == "momentum")
                if mom_open < config.MOMENTUM_MAX_POSITIONS:
                    m = check_momentum(symbol, prev_day_df)
                    if m.passes:
                        candidates.append({
                            "score": m.score,
                            "strategy": "momentum",
                            "entry_price": ep,
                            "stop_loss": ep * (1 - config.MOMENTUM_STOP_PCT),
                            "target_price": ep * (1 + config.MOMENTUM_TARGET_PCT),
                        })
                    else:
                        self._record_mom_fail(m.failure_reason)

                bo_open = sum(1 for p in self._open_positions.values() if p.get("strategy") == "breakout")
                if bo_open < config.BREAKOUT_MAX_POSITIONS:
                    b = check_range_breakout(symbol, prev_day_df)
                    if b.passes:
                        candidates.append({
                            "score": b.score,
                            "strategy": "breakout",
                            "entry_price": ep,
                            "stop_loss": ep * (1 - config.BREAKOUT_STOP_PCT),
                            "target_price": ep * (1 + config.BREAKOUT_TARGET_PCT),
                        })
                    else:
                        self._record_bo_fail(b.failure_reason)

                if not candidates:
                    continue

                best = max(candidates, key=lambda x: x["score"])
                self._stats["high_conf"] += 1
                chosen_strategy = best["strategy"]
                entry_price = best["entry_price"]
                stop_loss = best["stop_loss"]
                target_price = best["target_price"]

            elif self.strategy == "vcp_v2":
                # Two-stage VCP: a valid tight setup puts the symbol on a pivot
                # watchlist; entry happens on the first candle that breaks out
                # THROUGH the stored pivot within the watch window. Splitting
                # the stages fixes v1's contradiction where the breakout candle
                # itself invalidated the same-day tightness requirement.
                pend = self._pending_pivots.get(symbol)
                if pend is not None and sim_date > pend["expires"]:
                    del self._pending_pivots[symbol]
                    self._stats["pivot_watch_expired"] += 1
                    pend = None

                if pend is None:
                    result = analyse_stock(symbol, prev_day_df)  # scan mode
                    if (
                        result.is_high_confidence
                        and result.vcp_result
                        and result.vcp_result.pivot_price > 0
                    ):
                        self._pending_pivots[symbol] = {
                            "pivot": float(result.vcp_result.pivot_price),
                            "vcp_low": result.vcp_result.consolidation_low,
                            "expires": sim_date + timedelta(days=config.VCP2_PIVOT_WATCH_DAYS),
                        }
                        self._stats["pivot_watch_added"] += 1
                    else:
                        reason = result.failure_reason
                        if "Trend template" in reason:
                            self._stats["fail_tt"] += 1
                        elif "tightness" in reason.lower():
                            self._stats["fail_tightness"] += 1
                        elif "VCP" in reason:
                            self._stats["fail_vcp"] += 1
                        else:
                            self._stats["fail_other"] += 1
                    continue  # watch starts (or nothing found) — no entry today

                bo = is_valid_breakout(
                    today_ohlcv=prev_day_df.iloc[-1].to_dict(),
                    pivot_price=pend["pivot"],
                    avg_volume_50d=get_avg_volume_50d(prev_day_df),
                )
                if not bo.is_valid:
                    continue  # still watching
                del self._pending_pivots[symbol]
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = calculate_stop_loss_price(entry_price)
                vcp_low = pend.get("vcp_low")

            else:  # vcp
                result = analyse_stock(symbol, prev_day_df)  # scan mode, no EOD breakout
                if not result.is_high_confidence:
                    reason = result.failure_reason
                    if "Trend template" in reason:
                        self._stats["fail_tt"] += 1
                    elif "tightness" in reason.lower():
                        self._stats["fail_tightness"] += 1
                    elif "VCP" in reason:
                        self._stats["fail_vcp"] += 1
                    else:
                        self._stats["fail_other"] += 1
                    continue
                # Breakout confirmation — mirrors live: the scan evening's candle
                # must be an actual pivot breakout (close above pivot, strong
                # close, volume surge), not just a valid consolidation.
                pivot = result.vcp_result.pivot_price if result.vcp_result else 0.0
                if pivot <= 0:
                    self._stats["fail_breakout_confirm"] += 1
                    continue
                breakout_day = prev_day_df.iloc[-1].to_dict()
                bo = is_valid_breakout(
                    today_ohlcv=breakout_day,
                    pivot_price=pivot,
                    avg_volume_50d=get_avg_volume_50d(prev_day_df),
                )
                if not bo.is_valid:
                    self._stats["fail_breakout_confirm"] += 1
                    continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = (
                    result.suggested_stop_loss
                    if result.suggested_stop_loss is not None
                    else calculate_stop_loss_price(entry_price)
                )
                vcp_low = result.vcp_result.consolidation_low if result.vcp_result else None

            # Skip if today opens with a gap that destroys R/R
            prev_close = float(prev_day_df.iloc[-1]["close"])
            if prev_close > 0:
                gap_pct = (entry_price - prev_close) / prev_close
                if gap_pct > config.MAX_GAP_UP_PCT:
                    self._stats["gap_filtered"] += 1
                    continue
                if gap_pct < -0.03:
                    self._stats["gap_filtered"] += 1
                    continue
            portfolio_value = self._calc_portfolio_value(sim_date)

            # Position sizing: breakout/momentum/MR use fixed % of portfolio;
            # VCP uses risk-based sizing (1.5% risk per trade, capped at 15%).
            # The regime multiplier halves sizes in CAUTIOUS, matching live.
            # The minimum-position floor scales with it — otherwise CAUTIOUS
            # would fail every entry against a full-size floor.
            min_pos_value = config.MIN_POSITION_VALUE * regime_mult
            if chosen_strategy in ("breakout", "breakout_v2"):
                pos_value = portfolio_value * config.BREAKOUT_POSITION_SIZE_PCT * regime_mult
                shares = max(1, int(pos_value / entry_price))
                pos_value = shares * entry_price
                is_valid = pos_value >= min_pos_value and pos_value <= available_cash
            elif chosen_strategy == "mean_reversion":
                pos_value = portfolio_value * config.MR_POSITION_SIZE_PCT * regime_mult
                shares = max(1, int(pos_value / entry_price))
                pos_value = shares * entry_price
                is_valid = pos_value >= min_pos_value and pos_value <= available_cash
            elif chosen_strategy in ("momentum", "momentum_v2", "momentum_v3", "momentum_v4"):
                pos_value = portfolio_value * config.MOMENTUM_POSITION_SIZE_PCT * regime_mult
                shares = max(1, int(pos_value / entry_price))
                pos_value = shares * entry_price
                is_valid = pos_value >= min_pos_value and pos_value <= available_cash
            else:  # vcp: risk-based sizing
                size = calculate_position_size(
                    portfolio_value=portfolio_value,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss,
                    regime_multiplier=regime_mult,
                    available_cash=available_cash,
                )
                shares = size.shares
                pos_value = size.position_value if size.is_valid else 0.0
                # Dust guard: when nearly fully invested the cash cap shrinks
                # positions to 1-2 shares, where fixed costs eat double-digit
                # percentages. Floor set where CNC round-trip cost crosses
                # ~0.5% of position (DUST_FLOOR_DECISION.md).
                is_valid = size.is_valid and size.shares > 0 and pos_value >= min_pos_value

            if not is_valid or shares <= 0:
                self._stats["size_invalid"] += 1
                continue

            # Execute entry — use the real cost model, not an approximation
            actual_buy_cost = calculate_buy_cost(pos_value, self._costs)
            total_outflow = pos_value + actual_buy_cost
            if total_outflow > available_cash:
                self._stats["size_invalid"] += 1
                continue

            self._open_positions[symbol] = {
                "symbol": symbol,
                "entry_date": sim_date,
                "entry_price": entry_price,
                "shares": shares,
                "stop_loss": stop_loss,
                "initial_stop_loss": stop_loss,
                "vcp_low": vcp_low,
                "partial_sold": 0,
                "strategy": chosen_strategy,
                "target_price": target_price,
            }
            self.capital -= total_outflow   # deduct cash so next day's available_cash is correct
            available_cash -= total_outflow
            self._stats["entered"] += 1
            logger.info(
                "ENTRY: %s %d @ %.2f SL=%.2f on %s [%s]",
                symbol, shares, entry_price, stop_loss, sim_date, chosen_strategy,
            )

    # ── Kill-rate stat helpers ─────────────────────────────────────────────────

    def _record_mr_fail(self, reason: str) -> None:
        if "uptrend" in reason or "EMA200" in reason:
            self._stats["mr_fail_trend_gate"] += 1
        elif "RSI" in reason or "oversold" in reason:
            self._stats["mr_fail_rsi_entry"] += 1
        else:
            self._stats["mr_fail_other"] += 1

    def _record_mom_fail(self, reason: str) -> None:
        if "EMA" in reason:
            self._stats["mom_fail_ema"] += 1
        elif "olume" in reason or "vol" in reason.lower():
            self._stats["mom_fail_volume"] += 1
        elif "20d high" in reason or "below" in reason:
            self._stats["mom_fail_proximity"] += 1
        elif "RSI" in reason:
            self._stats["mom_fail_rsi"] += 1
        else:
            self._stats["mom_fail_other"] += 1

    def _record_bo_fail(self, reason: str) -> None:
        if "high" in reason.lower():
            self._stats["bo_fail_new_high"] += 1
        elif "olume" in reason or "vol" in reason.lower():
            self._stats["bo_fail_volume"] += 1
        elif "EMA" in reason:
            self._stats["bo_fail_ema"] += 1
        elif "RSI" in reason:
            self._stats["bo_fail_rsi"] += 1
        else:
            self._stats["bo_fail_other"] += 1

    # ── Position management ────────────────────────────────────────────────────

    def _close_position(self, symbol: str, exit_price: float, exit_date: date, reason: str) -> None:
        """Record position closure and update cash."""
        pos = self._open_positions.pop(symbol, None)
        if not pos:
            return

        net_pnl, pnl_pct = calculate_net_pnl(pos["entry_price"], exit_price, pos["shares"], self._costs)
        sell_value = exit_price * pos["shares"]
        sell_cost = calculate_sell_cost(sell_value, self._costs)
        self.capital += (sell_value - sell_cost)  # net cash received after sell-side costs

        trade = {
            "symbol": symbol,
            "entry_date": pos["entry_date"],
            "exit_date": exit_date,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "shares": pos["shares"],
            "realised_pnl": net_pnl,
            "realised_pnl_pct": pnl_pct * 100,
            "exit_reason": reason,
            "strategy": pos.get("strategy", "vcp"),
        }
        self._closed_trades.append(trade)
        logger.info("EXIT: %s @ %.2f PnL=%.1f%% reason=%s", symbol, exit_price, pnl_pct * 100, reason)

    def _close_all_positions(self, final_date: date) -> None:
        """Force-close all remaining positions at end of backtest."""
        for symbol in list(self._open_positions.keys()):
            df = self._ohlcv_cache.get(symbol)
            if df is None or df.empty:
                continue
            last_price = float(df["close"].iloc[-1])
            self._close_position(symbol, last_price, final_date, "backtest_end")

    def _calc_portfolio_value(self, sim_date: date) -> float:
        """Calculate total portfolio value (cash + open position market values)."""
        cash = self.capital
        positions_value = 0.0
        for symbol, pos in self._open_positions.items():
            df = self._ohlcv_cache.get(symbol)
            if df is None:
                positions_value += pos["entry_price"] * pos["shares"]
                continue
            sim_df = df[df.index.date <= sim_date]  # type: ignore[attr-defined]
            if sim_df.empty:
                positions_value += pos["entry_price"] * pos["shares"]
            else:
                positions_value += float(sim_df["close"].iloc[-1]) * pos["shares"]
        return cash + positions_value

    def _calc_available_cash(self) -> float:
        """Cash available after minimum reserve."""
        portfolio_value = self.capital
        for symbol, pos in self._open_positions.items():
            portfolio_value += pos["entry_price"] * pos["shares"]
        reserve = portfolio_value * config.MIN_CASH_RESERVE_PCT
        return max(0.0, self.capital - reserve)

    def _event_blocked(self, symbol: str, ref_date: date) -> bool:
        """Same rule as live's filter_corporate_events, evaluated in-memory."""
        from scanner.stock_filter import OTHER_EVENT_BUFFER_DAYS, RESULTS_BUFFER_DAYS
        for ex_date, event_type in self._events_by_symbol.get(symbol, ()):
            if event_type == "results":
                if abs((ex_date - ref_date).days) <= RESULTS_BUFFER_DAYS:
                    return True
            elif 0 <= (ex_date - ref_date).days <= OTHER_EVENT_BUFFER_DAYS:
                return True
        return False

    def _preload_events(self) -> None:
        """Load all corporate events once — per-day SQL lookups would add
        ~650k queries over a 5-year Nifty 500 run."""
        db_path = Path(config.DB_PATH) / config.CORPORATE_EVENTS_DB
        if not db_path.exists():
            logger.warning("--events-filter set but %s missing — filter is a NO-OP", db_path)
            return
        from sqlalchemy import create_engine
        from data_fetcher.event_fetcher import _safe_date
        df = pd.read_sql(
            "SELECT symbol, event_type, ex_date FROM corporate_events",
            create_engine(f"sqlite:///{db_path}"),
        )
        for _, row in df.iterrows():
            ex_date = _safe_date(row["ex_date"])
            if ex_date is None:
                continue
            self._events_by_symbol.setdefault(str(row["symbol"]).upper(), []).append(
                (ex_date, str(row["event_type"]))
            )
        all_dates = [d for evs in self._events_by_symbol.values() for d, _ in evs]
        if all_dates:
            logger.warning(
                "Events filter armed: %d events, %d symbols, ex-date coverage %s → %s "
                "(days outside coverage are silently unfiltered)",
                len(all_dates), len(self._events_by_symbol), min(all_dates), max(all_dates),
            )
        else:
            logger.warning("--events-filter set but corporate_events table is empty — NO-OP")

    def _preload_data(self, trading_days: list[date]) -> None:
        """Pre-load all OHLCV data needed for the backtest period."""
        # Get the broadest universe needed
        symbols = self._pit_universe.get_universe(self.start_date)
        data_start = self.start_date - timedelta(days=730)  # 2yr warmup captures full prior uptrend

        logger.info("Pre-loading OHLCV for %d symbols...", len(symbols))
        loaded = 0
        for symbol in symbols:
            df = load_ohlcv(symbol, data_start, self.end_date)
            if df is not None and len(df) >= 210:
                self._ohlcv_cache[symbol] = df
                loaded += 1
        logger.info("Loaded %d/%d symbols", loaded, len(symbols))

        # Build the daily regime gate from historical Nifty + breadth
        nifty_df = load_nifty_ohlcv(data_start, self.end_date)
        if nifty_df is None or len(nifty_df) < 220:
            logger.warning(
                "NIFTY50 history unavailable — regime gate DISABLED, every day "
                "treated as BULL. Results overstate live trade frequency."
            )
            return
        # The regime needs EMA200 warmup BEFORE the backtest starts; a cache
        # that begins mid-window silently classifies everything CAUTIOUS.
        nifty_first = nifty_df.index.min().date()
        warmup_deadline = self.start_date - timedelta(days=320)  # ~200 trading days
        if nifty_first > warmup_deadline:
            logger.warning(
                "NIFTY50 cache starts %s but regime EMA200 warmup needs data from "
                "~%s — regime series will default to CAUTIOUS until warmed up. "
                "Run scripts/backfill_data.py with a longer window.",
                nifty_first, warmup_deadline,
            )
        self._nifty_close = nifty_df["close"].astype(float)
        self._regime_by_day = build_regime_series(
            self._ohlcv_cache, nifty_df, trading_days
        )


if __name__ == "__main__":
    setup_logging()
    parser = argparse.ArgumentParser(description="Run strategy backtest")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=500_000.0)
    parser.add_argument("--strategy", default="vcp", choices=list(VALID_STRATEGIES))
    args = parser.parse_args()

    engine = BacktestEngine(
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        starting_capital=args.capital,
        strategy=args.strategy,
    )
    engine.run()
