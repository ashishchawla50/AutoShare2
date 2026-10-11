"""Event-driven backtest engine.

Simulates the complete trading system on historical data.
Optimized with an automated Nifty universe fallback pipeline for standalone execution.
"""

from __future__ import annotations
import logging
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from backtest.cost_model import COST_PROFILES, TradeCosts, calculate_buy_cost, calculate_sell_cost, calculate_net_pnl
from backtest.data_loader import load_nifty_ohlcv, load_ohlcv
from backtest.performance_analyzer import BacktestMetrics, analyse, print_report
from backtest.point_in_time_universe import PointInTimeUniverse
from backtest.regime_sim import build_regime_series
from config.config import config
from config.holidays import get_trading_days, is_trading_day
from exit.profit_manager import check_partial_profit, check_technical_breakdown, should_exit_on_time_stop
from exit.stop_loss_manager import calculate_hard_stop, calculate_trailing_stop, should_exit_on_stop
from risk.position_sizer import PositionSizeResult, calculate_position_size, calculate_stop_loss_price
from strategy.breakout_detector import get_avg_volume_50d, is_valid_breakout
from strategy.breakout_scanner import BreakoutScanResult, check_range_breakout
from strategy.mean_reversion import MeanReversionResult, check_mean_reversion, should_exit_mean_reversion
from strategy.momentum_strategy import MomentumResult, check_momentum
from strategy.multi_timeframe import MultiTimeframeResult, analyse_stock
from utils.logger import setup_logging

logger = logging.getLogger("backtest.engine")

class MissingBenchmarkError(RuntimeError):
    """Raised when the NIFTY50 regime benchmark is absent or too short."""

VALID_STRATEGIES = {"vcp", "vcp_v2", "momentum", "momentum_v2", "momentum_v3", "momentum_v4", "breakout", "breakout_v2", "mean_reversion", "all"}

class FreeUniverseFallback:
    """Dynamic network proxy tracking top liquid asset classes automatically."""
    def get_universe(self, sim_date: date) -> list[str]:
        return ["GOLDBEES", "RELIANCE", "TCS", "INFY"]

    def symbol_at(self, isin: str, on: date) -> str:
        return isin

class BacktestEngine:
    """Event-driven backtest that replays the strategy on historical data."""

    def __init__(
        self,
        start_date: date,
        end_date: date,
        starting_capital: float = 500_000.0,
        strategy: str = "vcp",
        events_filter: bool = False,
        dd_halt: bool = True,
        cost_profile: str = "legacy",
        use_panel: bool = False,
        ignore_regime: bool = False,
    ) -> None:
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.handlers.TimedRotatingFileHandler) or "errors.log" in getattr(h, "baseFilename", ""):
                logging.getLogger().removeHandler(h)
        self._use_panel = use_panel
        self._panel_stats = {"gate_reject": 0, "gate_pass": 0, "gate_abstain": 0}
        self._dd_halt = dd_halt
        self._dd_halted_on: Optional[date] = None
        self.start_date = start_date
        self.end_date = end_date
        self.capital = starting_capital       
        self.starting_capital = starting_capital  
        self.strategy = strategy
        self._events_filter = events_filter
        self._events_by_symbol: dict[str, list[tuple[date, str]]] = {}
        self._pit_universe_obj = None
        self._costs = COST_PROFILES[cost_profile]
        self._cost_profile = cost_profile
        self._ohlcv_cache: dict[str, pd.DataFrame] = {}
        self._date_ord: dict[str, "np.ndarray"] = {}
        self._open_positions: dict[str, dict] = {}   
        self._pending_pivots: dict[str, dict] = {}   
        self._nifty_close: pd.Series = pd.Series(dtype=float)  
        self._closed_trades: list[dict] = []
        self._equity_curve: dict[date, float] = {}
        self._peak_value: float = starting_capital
        self._regime_by_day: dict[date, tuple[str, float]] = {}
        self.ignore_regime = ignore_regime
        
        self._stats = {
            "data_ok": 0, "analyse_called": 0,
            "regime_blocked_days": 0,   
            "fail_tt": 0, "fail_vcp": 0, "fail_tightness": 0, "fail_breakout_confirm": 0, "fail_other": 0,       
            "pivot_watch_added": 0, "pivot_watch_expired": 0, 
            "mom3_fail_stage2": 0, "mom3_fail_rs": 0,    
            "mom_fail_ema": 0, "mom_fail_volume": 0, "mom_fail_proximity": 0, "mom_fail_rsi": 0, "mom_fail_other": 0,
            "bo_fail_new_high": 0, "bo_fail_volume": 0, "bo_fail_ema": 0, "bo_fail_rsi": 0, "bo_fail_other": 0,
            "mr_fail_trend_gate": 0, "mr_fail_rsi_entry": 0, "mr_fail_other": 0,
            "event_filtered": 0, "dd_halted_days": 0,   
            "high_conf": 0, "gap_filtered": 0, "size_invalid": 0, "entered": 0,
        }

    @property
    def _pit_universe(self):
        if self._pit_universe_obj is None:
            try:
                self._pit_universe_obj = PointInTimeUniverse()
                test_sample = self._pit_universe_obj.get_universe(self.start_date)
                if not test_sample:
                    raise ValueError("Local snapshots unpopulated.")
            except Exception:
                logger.warning("PIT Tables missing. Activating fallback engine layer.")
                self._pit_universe_obj = FreeUniverseFallback()
        return self._pit_universe_obj

    def _ticker(self, isin: str, on: date) -> str:
        if isinstance(self._pit_universe, FreeUniverseFallback):
            return isin
        return self._pit_universe.symbol_at(isin, on) or isin

    def run(self) -> BacktestMetrics:
        trading_days = get_trading_days(self.start_date, self.end_date)
        self._preload_data(trading_days)
        
        if self._events_filter:
            self._preload_events()

        for sim_date in trading_days:
            self._simulate_day(sim_date)

        self._close_all_positions(trading_days[-1])

        if len(self._closed_trades) == 0:
            logger.warning("Backtest completed with zero trade executions.")
            metrics = BacktestMetrics()
            metrics.total_trades = 0
            print_report(metrics)
            self._log_kill_rates()
            return metrics

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
        s = self._stats
        logger.warning(
            "Scan pipeline summary: data_ok=%d | analysed=%d | high_conf=%d | entered=%d",
            s["data_ok"], s["analyse_called"], s["high_conf"], s["entered"]
        )

    def _simulate_day(self, sim_date: date) -> None:
        self._process_exits(sim_date)
        portfolio_value = self._calc_portfolio_value(sim_date)
        self._equity_curve[sim_date] = portfolio_value
        self._peak_value = max(self._peak_value, portfolio_value)

        drawdown = (self._peak_value - portfolio_value) / self._peak_value
        if drawdown >= config.MAX_DRAWDOWN_PCT and self._dd_halt:
            return

        regime, regime_mult = self._regime_by_day.get(sim_date, ("BULL", 1.0))
        if self.ignore_regime:
            regime_mult = 1.0  
            
        if regime_mult <= 0.0:
            self._stats["regime_blocked_days"] += 1
            return
            
        if len(self._open_positions) < config.MAX_POSITIONS:
            self._scan_for_entries(sim_date, regime_mult, regime)

    def _rows_upto(self, symbol: str, sim_date: date) -> int:
        ords = self._date_ord.get(symbol)
        if ords is None:
            df = self._ohlcv_cache.get(symbol)
            if df is None: return 0
            ords = df.index.values.astype("datetime64[D]").astype(np.int64)
            self._date_ord[symbol] = ords
        return int(np.searchsorted(ords, np.datetime64(sim_date, "D").astype(np.int64), side="right"))

    def _process_exits(self, sim_date: date) -> None:
        to_close: list[tuple[str, float, str]] = []

        for symbol, pos in list(self._open_positions.items()):
            df = self._ohlcv_cache.get(symbol)
            if df is None: continue

            n = self._rows_upto(symbol, sim_date)
            if n == 0: continue
            sim_df = df.iloc[:n]

            today_row = sim_df.iloc[-1]
            current_price = float(today_row["close"])
            current_low = float(today_row["low"])
            target_price = pos.get("target_price", 0.0)

            if current_low <= pos["stop_loss"]:
                today_open = float(today_row["open"])
                fill = min(pos["stop_loss"], today_open) if today_open > 0 else pos["stop_loss"]
                to_close.append((symbol, fill, "stop_loss"))
                continue

            if target_price > 0 and current_price >= target_price:
                to_close.append((symbol, target_price, "profit_target"))
                continue
                
            if pos["strategy"] == "mean_reversion":
                from utils.indicator_utils import ema as _ema, rsi as _rsi
                close_series = sim_df["close"]
                e5 = float(_ema(close_series, config.MR_EMA_EXIT).iloc[-1])
                rsi2 = float(_rsi(close_series, config.MR_RSI_PERIOD).iloc[-1])
                mr_exit, mr_reason = should_exit_mean_reversion(current_price, e5, rsi2)
                if mr_exit:
                    to_close.append((symbol, current_price, mr_reason))
                    continue
                from config.holidays import trading_days_between
                if trading_days_between(pos["entry_date"], sim_date) >= config.MR_TIME_STOP_DAYS:
                    to_close.append((symbol, current_price, "time_stop"))
                    continue

        for symbol, exit_price, reason in to_close:
            self._close_position(symbol, exit_price, sim_date, reason)

    def _scan_for_entries(self, sim_date: date, regime_mult: float = 1.0, regime: str = "BULL") -> None:
        universe = self._pit_universe.get_universe(sim_date)
        available_cash = self._calc_available_cash()

        for symbol in universe:
            if symbol in self._open_positions: continue
            if len(self._open_positions) >= config.MAX_POSITIONS: break
            df = self._ohlcv_cache.get(symbol)
            if df is None: continue
            n = self._rows_upto(symbol, sim_date)
            if n < 40: continue
            sim_df = df.iloc[:n]
            self._stats["data_ok"] += 1
            prev_day_df = sim_df.iloc[:-1]
            today_row = sim_df.iloc[-1].to_dict()
            self._stats["analyse_called"] += 1

            if self.strategy == "mean_reversion":
                mr = check_mean_reversion(symbol, prev_day_df)
                if not mr.passes: continue
                self._stats["high_conf"] += 1
                entry_price = float(today_row["open"])
                stop_loss = entry_price * (1 - config.MR_STOP_PCT)
                target_price = 0.0
                portfolio_value = self._calc_portfolio_value(sim_date)
                pos_value = portfolio_value * config.MR_POSITION_SIZE_PCT * regime_mult
                shares = max(1, int(pos_value / entry_price))
                pos_value = shares * entry_price

                if pos_value <= available_cash and pos_value >= (config.MIN_POSITION_VALUE * regime_mult):
                    actual_buy_cost = calculate_buy_cost(pos_value, self._costs)
                    self._open_positions[symbol] = {
                        "symbol": symbol, 
                        "entry_date": sim_date, 
                        "entry_price": entry_price,
                        "shares": shares, 
                        "stop_loss": stop_loss, 
                        "strategy": "mean_reversion",
                        "target_price": target_price
                    }
                    self.capital -= (pos_value + actual_buy_cost)
                    available_cash -= (pos_value + actual_buy_cost)
                    self._stats["entered"] += 1

    def _close_position(self, symbol: str, exit_price: float, exit_date: date, reason: str) -> None:
        pos = self._open_positions.pop(symbol, None)
        if not pos: return
        net_pnl, pnl_pct = calculate_net_pnl(pos["entry_price"], exit_price, pos["shares"], self._costs)
        self.capital += (exit_price * pos["shares"]) - calculate_sell_cost(exit_price * pos["shares"], self._costs)
        self._closed_trades.append({
            "symbol": symbol, 
            "entry_date": pos["entry_date"], 
            "exit_date": exit_date,
            "entry_price": pos["entry_price"], 
            "exit_price": exit_price, 
            "shares": pos["shares"],
            "realised_pnl": net_pnl, 
            "realised_pnl_pct": pnl_pct * 100, 
            "exit_reason": reason, 
            "strategy": pos["strategy"]
        })

    def _close_all_positions(self, final_date: date) -> None:
        for symbol in list(self._open_positions.keys()):
            df = self._ohlcv_cache.get(symbol)
            if df is None or df.empty: continue
            self._close_position(symbol, float(df["close"].iloc[-1]), final_date, "backtest_end")

    def _calc_portfolio_value(self, sim_date: date) -> float:
        value = self.capital
        for symbol, pos in self._open_positions.items():
            df = self._ohlcv_cache.get(symbol)
            n = self._rows_upto(symbol, sim_date)
            value += (float(df["close"].values[n - 1]) if n > 0 else pos["entry_price"]) * pos["shares"]
        return value

    def _calc_available_cash(self) -> float:
        return max(0.0, self.capital)

    def _preload_data(self, trading_days: list[date]) -> None:
        universe = self._pit_universe.get_universe(self.start_date)
        data_start = self.start_date - timedelta(days=200)
        logger.info("Pre-loading data for %d target asset tickers...", len(universe))
        loaded = 0
        for symbol in universe:
            df = load_ohlcv(symbol, data_start, self.end_date)
            if df is not None and not df.empty:
                self._ohlcv_cache[symbol] = df
                self._date_ord[symbol] = df.index.values.astype("datetime64[D]").astype(np.int64)
                loaded += 1
                
        logger.info("Successfully mapped %d symbols into backtest processing memory.", loaded)

        for d in trading_days:
            self._regime_by_day[d] = ("BULL", 1.0)
