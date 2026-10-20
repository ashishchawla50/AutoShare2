"""Fast vcp_v2 backtest over precomputed arrays.

Same day loop, ordering, sizing, cost and exit rules as backtest_engine's vcp_v2
path; the only change is where numbers come from. Per symbol-day the reference
slices a DataFrame and re-runs every indicator; here each lookup is an array index
into data.py / signals.py output, and Python only touches the few universe members
where something can happen that day (a pending pivot or a fresh setup). Every other
member's outcome is a precomputed rejection, counted in bulk. A 5-year Liquid-500
run takes a few seconds.

Reference-engine behaviour reproduced deliberately (do not "fix" here without
fixing it there too — parity with the frozen oracle is the correctness proof):
  - a symbol with no candle on sim_date uses its latest earlier candle as "today"
  - the scan reads the prior candle (prev_day_df), entry fills at today's open
  - exits are collected first, then closed in open-position insertion order
  - the stop ratchet updates after the stop check, so it acts from the next day
  - the scan stops at the first member after the book fills (MAX_POSITIONS)
  - the drawdown halt is absorbing when enabled

Live-parity rules (Parity, 2026-10-19), all ON by default and all OFF for the oracle
fixtures, which predate them: live's open <= pivot + 5% gap rule, the regime exit
(3rd BEAR day / 2nd EXTREME_VOLATILITY day, all positions sold at the open), India
VIX regime tiers, the 15% drawdown entry pause, the daily/weekly/monthly realised-loss
limits, and live's bhavcopy-derivable pre-scan filters (backtest/fast/live_filters.py).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from backtest.cost_model import COST_PROFILES, calculate_buy_cost, calculate_net_pnl, calculate_sell_cost
from backtest.data_loader import load_india_vix, load_nifty_ohlcv
from backtest.fast.data import MarketData, Universe, load_market_data
from backtest.fast.live_filters import LiveFilters
from backtest.fast.signals import FAIL_TIGHTNESS, FAIL_TT, FAIL_VCP, HIGH_CONF, Signals, load_signals
from backtest.performance_analyzer import BacktestMetrics, analyse, print_report
from backtest.regime_sim import build_regime_series
from config.config import config
from config.holidays import get_trading_days
from exit.profit_manager import check_partial_profit
from exit.stop_loss_manager import TRAILING_RULES
from risk.position_sizer import calculate_position_size, calculate_stop_loss_price

logger = logging.getLogger("backtest.fast.engine")


class MissingBenchmarkError(RuntimeError):
    """NIFTY50 regime benchmark absent or too short — never silently treat every day as BULL."""


_STATS = ("data_ok", "analyse_called", "fail_tt", "fail_vcp", "fail_tightness",
          "fail_breakout_confirm", "fail_other", "pivot_watch_added", "pivot_watch_expired",
          "event_filtered", "high_conf", "gap_filtered", "size_invalid", "entered",
          "regime_blocked_days", "dd_halted_days",
          # live-parity rules (Parity)
          "open_cap_filtered", "regime_exits", "dd_paused_days", "loss_limit_days",
          "prefilter_price", "prefilter_liquidity", "prefilter_circuit_breaker",
          "prefilter_data_quality")

_BEAR, _EXTREME = "BEAR", "EXTREME_VOLATILITY"


@dataclass(frozen=True)
class Parity:
    """Live rules replayed in the backtest. Default = all on (the live bot's behaviour).

    open_cap      skip the entry if the open is above pivot x (1 + MAX_ENTRY_EXTENSION_PCT)
                  (live breakout_detector.evaluate_gap's second rule)
    regime_exit   sell every position at the open on the REGIME_EXIT_BEAR_DAYS-th straight
                  BEAR day, or the REGIME_EXIT_EXTREME_VOL_DAYS-th EXTREME_VOLATILITY day
                  (live exit.time_stop.check_regime_exit, counted in trading days)
    vix           India VIX regime tiers (backtest/regime_sim.py; needs data/india_vix.csv)
    dd_pause      no entries while drawdown from peak >= DD_HALT_ENTRIES_AT (lifts on
                  recovery; live DrawdownMonitor._check_drawdown)
    loss_limits   no entries while realised P&L over the day / WEEKLY_LOSS_WINDOW_DAYS /
                  MONTHLY_LOSS_WINDOW_DAYS is at or below -DAILY/WEEKLY/MONTHLY_LOSS_LIMIT_PCT
                  of equity (live DrawdownMonitor; today's exits count only if filled at
                  the open, i.e. before the entry window)
    live_filters  live pre-scan filters that bhavcopy can reproduce (live_filters.py)
    """
    open_cap: bool = True
    regime_exit: bool = True
    vix: bool = True
    dd_pause: bool = True
    loss_limits: bool = True
    live_filters: bool = True

    @classmethod
    def off(cls) -> "Parity":
        """The pre-2026-10-19 engine, which the frozen oracle fixtures pin."""
        return cls(**{f.name: False for f in fields(cls)})

    @classmethod
    def without(cls, names) -> "Parity":
        """All on except the named rules."""
        known = {f.name for f in fields(cls)}
        bad = set(names) - known
        if bad:
            raise ValueError(f"unknown parity rules {sorted(bad)}; known: {sorted(known)}")
        return cls(**{n: n not in names for n in known})


@dataclass
class _Pos:
    slot: int
    entry_date: date
    entry_ti: int             # index of entry_date in the trading calendar
    entry_price: float
    shares: int
    stop_loss: float
    vcp_low: Optional[float]
    strategy: str = "vcp_v2"
    target_price: float = 0.0    # fixed profit target; 0 = none
    # partial sales (exit_mode "my_strategy"); untouched by the oracle path
    armed: bool = False          # +10% reached: half sold, stop at entry, trailing on
    orig_shares: int = 0         # shares bought, set on the first partial sale
    sold_shares: int = 0
    sold_value: float = 0.0
    realised: float = 0.0        # net P&L already booked by partial sales


EXIT_MODES = ("vcp", "my_strategy", "vcp_partials")


class FastBacktestEngine:
    """Drop-in for BacktestEngine(strategy="vcp_v2") on the reconstructed dataset.

    Pass market/signals/universe to reuse loaded arrays across many runs (sweeps).
    """

    def __init__(
        self,
        start_date: date,
        end_date: date,
        starting_capital: float = config.STARTING_CAPITAL,
        cost_profile: str = "cnc",
        dd_halt: bool = True,
        market: Optional[MarketData] = None,
        signals: Optional[Signals] = None,
        universe: Optional[Universe] = None,
        verbose: bool = True,
        ignore_regime: bool = False,
        exit_mode: str = "vcp",
        slippage: float = 0.0,
        caution_full_size: bool = False,
        entry_filter=None,
        take_profit: float = 0.0,
        parity: Optional[Parity] = None,
    ) -> None:
        """Research options (all off by default, so the oracle path is untouched):
        exit_mode     "my_strategy" / "vcp_partials" (see _my_exit / _live_partials)
        slippage      fraction per side: buys fill this much higher, sells this much lower
        caution_full_size  trade CAUTIOUS days at full size instead of the regime's half
        entry_filter  callable(engine, slot, p_row, ti, pend) -> bool, asked for every
                      confirmed breakout; False drops it (its pivot is consumed)
        take_profit   sell everything when the day's high reaches entry x (1 + take_profit),
                      filled at that price (or the open, if it gaps above); 0 = off
        parity        live rules to replay (Parity); None = all on. Parity.off() = oracle engine
        """
        if exit_mode not in EXIT_MODES:
            raise ValueError(f"exit_mode must be one of {EXIT_MODES}")
        self._exit_mode = exit_mode
        self._slip = slippage
        self._caution_full = caution_full_size
        self._entry_filter = entry_filter
        self._tp = take_profit
        self.parity = Parity() if parity is None else parity
        self.start_date = start_date
        self.end_date = end_date
        self.starting_capital = starting_capital
        self.capital = starting_capital
        self.strategy = "vcp_v2"
        self._costs = COST_PROFILES[cost_profile]
        self._cost_profile = cost_profile
        self._dd_halt = dd_halt
        # Research switch: trade every day at full size, whatever the regime
        # (BEAR no longer blocks, CAUTIOUS no longer halves). Never used by the
        # oracle fixtures — results with it on are not comparable to validation runs.
        self._ignore_regime = ignore_regime
        self._regime_name = "BULL"
        self._dd_halted_on: Optional[date] = None
        self._md = market
        self._sig = signals
        self._uni = universe
        self._verbose = verbose
        self._stats = {k: 0 for k in _STATS}
        self._stats["filtered"] = 0
        self._closed_trades: list[dict] = []
        self._exit_log: list[tuple[date, float, bool]] = []   # (exit_date, net P&L, filled at open)
        self._filters: Optional[LiveFilters] = None
        self._equity_curve: dict[date, float] = {}
        self._regime_by_day: dict[date, tuple[str, float]] = {}
        self.timings: dict[str, float] = {}

    # ── setup ────────────────────────────────────────────────────────────────

    def _prepare(self, trading_days: list[date]) -> None:
        t0 = time.perf_counter()
        md = self._md = self._md or load_market_data()
        self._sig = self._sig or load_signals(md)
        self._uni = self._uni or Universe()
        self.timings["load"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        data_start = self.start_date - timedelta(days=config.BACKTEST_WARMUP_CALENDAR_DAYS)
        lo_day = np.datetime64(data_start, "D").astype(np.int64)
        hi_day = np.datetime64(self.end_date, "D").astype(np.int64)
        day_nums = np.array(trading_days, dtype="datetime64[D]").astype(np.int64)
        members = [self._uni.get_universe(d.isoformat()) for d in trading_days]

        # Slots = union of every monthly universe in the window, keeping securities
        # with >= TT_MIN_HISTORY_DAYS candles in [data_start, end_date] — the set the reference
        # preloads, which also feeds regime breadth.
        isins: list[str] = []
        slot_of: dict[str, int] = {}
        a_l, lo_l, hi_l = [], [], []
        seen: set[str] = set()
        for day in members:
            for isin in day:
                if isin in seen:
                    continue
                seen.add(isin)
                k = md.index.get(isin)
                if k is None:
                    continue
                a, b = int(md.offsets[k]), int(md.offsets[k + 1])
                d = md.dates[a:b]
                lo = a + int(np.searchsorted(d, lo_day, side="left"))
                hi = a + int(np.searchsorted(d, hi_day, side="right"))
                if hi - lo >= config.TT_MIN_HISTORY_DAYS:
                    slot_of[isin] = len(isins)
                    isins.append(isin)
                    a_l.append(a); lo_l.append(lo); hi_l.append(hi)
        self._isins = isins
        self._a = np.array(a_l, dtype=np.int64)
        self._lo = np.array(lo_l, dtype=np.int64)
        self._hi = np.array(hi_l, dtype=np.int64)
        K, T = len(isins), len(trading_days)

        # n[ti, k] = candles of slot k dated <= trading day ti (the reference's len(sim_df))
        self._n = np.zeros((T, K), dtype=np.int32)
        for k in range(K):
            self._n[:, k] = np.searchsorted(md.dates[self._lo[k]:self._hi[k]], day_nums, side="right")
        self._day_slots = [np.array([slot_of[i] for i in day if i in slot_of], dtype=np.int64)
                           for day in members]
        self.timings["universe"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        nifty = load_nifty_ohlcv(data_start, self.end_date)
        if nifty is None or len(nifty) < config.BACKTEST_MIN_BENCHMARK_ROWS:
            raise MissingBenchmarkError(
                f"NIFTY50 benchmark missing or too short "
                f"({0 if nifty is None else len(nifty)} rows) for {data_start}→{self.end_date}")
        self._breadth_s = self._breadth(nifty)
        self._regime_by_day = build_regime_series(
            {}, nifty, trading_days, breadth=self._breadth_s,
            vix=load_india_vix() if self.parity.vix else None)
        if self.parity.live_filters:
            self._filters = LiveFilters(md)
        self.timings["regime"] = time.perf_counter() - t0

    def _breadth(self, nifty: pd.DataFrame) -> pd.Series:
        """Share of loaded securities above their own EMA200 on each Nifty date —
        regime_sim's DataFrame-of-flags mean, computed by counting."""
        md = self._md
        nd = nifty.index.values.astype("datetime64[D]").astype(np.int64)
        num = np.zeros(len(nd))
        den = np.zeros(len(nd))
        for k in range(len(self._isins)):
            lo, hi = self._lo[k], self._hi[k]
            c = pd.Series(md.close[lo:hi])
            e = c.ewm(span=config.REGIME_EMA_DAYS, adjust=False,
                      min_periods=config.REGIME_EMA_DAYS).mean().to_numpy()
            with np.errstate(invalid="ignore"):
                flag = md.close[lo:hi] > e          # NaN warm-up compares False, as in pandas
            pos = np.searchsorted(nd, md.dates[lo:hi])
            pos_c = np.minimum(pos, len(nd) - 1)
            hit = nd[pos_c] == md.dates[lo:hi]
            np.add.at(num, pos_c[hit], flag[hit])
            np.add.at(den, pos_c[hit], 1.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            b = np.where(den > 0, num / den, config.REGIME_BREADTH_NEUTRAL)
        return pd.Series(b, index=nifty.index)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _row(self, slot: int, ti: int) -> int:
        """Global row of the latest candle <= trading day ti, or -1 if none."""
        n = int(self._n[ti, slot])
        return int(self._lo[slot]) + n - 1 if n > 0 else -1

    def _portfolio_value(self, ti: int) -> float:
        v = self.capital
        close = self._md.close
        for p in self._open.values():
            r = self._row(p.slot, ti)
            v += (p.entry_price if r < 0 else float(close[r])) * p.shares
        return v

    def _available_cash(self) -> float:
        pv = self.capital
        for p in self._open.values():
            pv += p.entry_price * p.shares
        return max(0.0, self.capital - pv * config.MIN_CASH_RESERVE_PCT)

    def _close(self, slot: int, exit_price: float, exit_date: date, reason: str,
               at_open: bool = False) -> None:
        p = self._open.pop(slot)
        if self._slip:
            exit_price *= 1 - self._slip
        shares = p.shares
        if p.sold_shares:                       # partial sale earlier: one record for the whole trade
            net_pnl = p.realised + self._leg_net(p, p.shares, exit_price)
            shares = p.orig_shares
            pnl_pct = net_pnl / (p.entry_price * shares)
            sell_value = exit_price * p.shares
            exit_price = (p.sold_value + sell_value) / shares     # average sell price
            reason = f"{reason} (after partial sale)"
        else:
            net_pnl, pnl_pct = calculate_net_pnl(p.entry_price, exit_price, p.shares, self._costs)
            sell_value = exit_price * p.shares
        self.capital += sell_value - calculate_sell_cost(sell_value, self._costs)
        self._exit_log.append((exit_date, net_pnl, at_open))
        isin = self._isins[slot]
        self._closed_trades.append({
            "isin": isin,
            "symbol": self._uni.symbol_at(isin, p.entry_date.isoformat()),
            "entry_date": p.entry_date, "exit_date": exit_date,
            "entry_price": p.entry_price, "exit_price": exit_price, "shares": shares,
            "realised_pnl": net_pnl, "realised_pnl_pct": pnl_pct * 100,
            "exit_reason": reason, "strategy": p.strategy,
        })

    def _leg_net(self, p: _Pos, n: int, px: float) -> float:
        """Net P&L of selling n shares at px, with the buy cost shared pro rata — so the
        legs of a split trade sum to exactly what the entry outflow and sells moved."""
        buy_cost = calculate_buy_cost(p.entry_price * p.orig_shares, self._costs) * n / p.orig_shares
        return n * (px - p.entry_price) - buy_cost - calculate_sell_cost(n * px, self._costs)

    def _sell_part(self, p: _Pos, n: int, px: float) -> None:
        if not p.orig_shares:
            p.orig_shares = p.shares
        if self._slip:
            px *= 1 - self._slip
        sell_value = n * px
        self.capital += sell_value - calculate_sell_cost(sell_value, self._costs)
        p.realised += self._leg_net(p, n, px)
        p.sold_shares += n
        p.sold_value += sell_value
        p.shares -= n

    # ── day loop ─────────────────────────────────────────────────────────────

    def _process_exits(self, sim_date: date, ti: int) -> None:
        md = self._md
        to_close: list[tuple[int, float, str, bool]] = []
        for slot, p in self._open.items():
            r = self._row(slot, ti)
            if r < 0:
                continue
            if float(md.low[r]) <= p.stop_loss:
                o = float(md.open[r])
                to_close.append((slot, min(p.stop_loss, o) if o > 0 else p.stop_loss, "stop_loss",
                                 0 < o <= p.stop_loss))
                continue
            hit = self._exit_signal(slot, p, r, ti)
            if hit is not None:
                to_close.append((slot, hit[0], hit[1], False))

        for slot, px, reason, at_open in to_close:
            self._close(slot, px, sim_date, reason, at_open=at_open)

    def _regime_exit(self, sim_date: date, ti: int, reason: str) -> None:
        """Sell every open position at today's open (live check_regime_exit). A position
        with no candle today is sold on its next candle."""
        today = np.datetime64(sim_date, "D").astype(np.int64)
        for slot in list(self._open):
            r = self._row(slot, ti)
            if r < 0 or int(self._md.dates[r]) != today or float(self._md.open[r]) <= 0:
                continue
            self._close(slot, float(self._md.open[r]), sim_date, reason, at_open=True)
            self._stats["regime_exits"] += 1

    def _loss_limit_hit(self, sim_date: date, pv: float) -> bool:
        """Live DrawdownMonitor's realised-loss limits, as seen at the entry window."""
        if pv <= 0:
            return False
        day = week = month = 0.0
        w0 = sim_date - timedelta(days=config.WEEKLY_LOSS_WINDOW_DAYS)
        m0 = sim_date - timedelta(days=config.MONTHLY_LOSS_WINDOW_DAYS)
        for d, pnl, at_open in reversed(self._exit_log):
            if d < m0:
                break
            if d == sim_date and not at_open:
                continue                      # happens after the entry window
            month += pnl
            if d >= w0:
                week += pnl
            if d == sim_date:
                day += pnl
        return (day / pv <= -config.DAILY_LOSS_LIMIT_PCT
                or week / pv <= -config.WEEKLY_LOSS_LIMIT_PCT
                or month / pv <= -config.MONTHLY_LOSS_LIMIT_PCT)

    def _exit_signal(self, slot: int, p: _Pos, r: int, ti: int) -> Optional[tuple[float, str]]:
        """Non-stop exit for an open position on row r: (fill, reason) or None."""
        if self._exit_mode == "my_strategy":
            return self._my_exit(slot, p, r, ti)
        if self._tp:
            target = p.entry_price * (1 + self._tp)
            if float(self._md.high[r]) >= target:
                return max(target, float(self._md.open[r])), "take_profit"
        hit = self._vcp_exit(slot, p, r, ti)
        if hit is None and self._exit_mode == "vcp_partials":
            self._live_partials(p, r)
        return hit

    def _live_partials(self, p: _Pos, r: int) -> None:
        """Live's partial booking, via the live function itself: on the day's close, after
        the breakdown and time-stop checks, at most one tier per day (main.check_exits)."""
        total = p.orig_shares or p.shares
        hit = check_partial_profit(p.entry_price, float(self._md.close[r]), total, p.sold_shares)
        if hit:
            n = min(hit[0], p.shares)
            if n > 0:
                self._sell_part(p, n, float(self._md.close[r]))

    def _my_exit(self, slot: int, p: _Pos, r: int, ti: int) -> Optional[tuple[float, str]]:
        """my_strategy (docs/WINNING_STRATEGY.md): when the high reaches +10%, sell half at
        +10% (or the open, if it gaps above) and lift the stop to entry; from then on the stop
        follows 10-day EMA x 0.97. No target, no time stop, no breakdown exit — the remaining
        half leaves only through the stop. Stop changes act from the next day."""
        md = self._md
        if not p.armed:
            target = p.entry_price * (1 + config.MY_STRATEGY_PARTIAL_AT)
            if float(md.high[r]) >= target:
                half = int(p.shares * config.MY_STRATEGY_PARTIAL_FRACTION)
                if half:
                    self._sell_part(p, half, max(target, float(md.open[r])))
                p.armed = True
                p.stop_loss = max(p.stop_loss, p.entry_price)
        if p.armed and r - int(self._a[slot]) + 1 >= config.STOP_EMA_FLOOR_DAYS:
            floor = float(self._sig.ema10[r]) * config.STOP_EMA_FLOOR_MULT
            if floor > p.stop_loss:
                p.stop_loss = floor
        return None

    def _vcp_exit(self, slot: int, p: _Pos, r: int, ti: int) -> Optional[tuple[float, str]]:
        """vcp_v2 exit geometry: trailing ratchet, technical breakdown, 15-day time stop."""
        md, sig = self._md, self._sig
        close = float(md.close[r])
        # Trailing ratchet (calculate_trailing_stop)
        profit = (close - p.entry_price) / p.entry_price
        rule_stop = p.entry_price * (1 - config.HARD_STOP_PCT)
        for rule in reversed(TRAILING_RULES):
            if profit >= rule.profit_pct:
                rs = p.entry_price * (1 + rule.trail_to)
                if rs > rule_stop:
                    rule_stop = rs
                break
        hist = r - int(self._a[slot]) + 1     # history length behind today's candle
        if hist >= config.STOP_EMA_FLOOR_DAYS:
            floor = float(sig.ema10[r]) * config.STOP_EMA_FLOOR_MULT
            if floor > rule_stop:
                rule_stop = floor
        if rule_stop > p.stop_loss:
            p.stop_loss = rule_stop

        # Technical breakdown (check_technical_breakdown)
        if hist >= config.BREAKDOWN_MIN_HISTORY:
            if p.vcp_low and close < p.vcp_low:
                return close, (f"Close {close:.2f} below VCP low "
                               f"{p.vcp_low:.2f} — breakout failed")
            e50, vol, avg = float(sig.ema50[r]), float(md.volume[r]), float(sig.volsma_bd[r])
            if close < e50 and vol > avg * config.BREAKDOWN_VOLUME_MULT:
                return close, (f"Close {close:.2f} below {config.BREAKDOWN_EMA_DAYS}-day EMA "
                               f"{e50:.2f} on {vol / avg:.1f}x volume")

        # Time stop (should_exit_on_time_stop)
        held = ti - p.entry_ti
        if held >= config.TIME_STOP_DAYS and profit < config.TIME_STOP_MIN_PROFIT_PCT:
            return close, (f"Time stop: {held} days held, profit {profit:.1%} "
                           f"< {config.TIME_STOP_MIN_PROFIT_PCT:.0%} threshold")
        return None

    def _scan(self, sim_date: date, ti: int, regime_mult: float) -> None:
        md, sig, st = self._md, self._sig, self._stats
        U = self._day_slots[ti]
        if len(U) == 0:
            return
        n = self._n[ti, U]
        valid = n >= config.TT_MIN_HISTORY_DAYS
        if self._open:
            valid &= ~np.isin(U, np.fromiter(self._open, np.int64))
        prev = self._lo[U] + n.astype(np.int64) - 2        # scan candle (prev_day_df[-1])
        verdict = np.where(valid, sig.verdict[np.where(valid, prev, 0)], 0)
        event = valid & (verdict == HIGH_CONF)
        if self._pending:
            event |= valid & np.isin(U, np.fromiter(self._pending, np.int64))

        available_cash = self._available_cash()
        watch_days = timedelta(days=config.VCP2_PIVOT_WATCH_DAYS)
        today = np.datetime64(sim_date, "D").astype(np.int64)
        cutoff = len(U)          # members at index >= cutoff are never reached (book full)
        for j in np.nonzero(event)[0]:
            slot = int(U[j])
            p_row = int(prev[j])
            pend = self._pending.get(slot)
            if pend is not None and sim_date > pend[2]:
                del self._pending[slot]
                st["pivot_watch_expired"] += 1
                pend = None

            if self._filters is not None:
                failed = self._filters.check(int(self._a[slot]), p_row, today)
                if failed:
                    st["prefilter_" + failed] += 1
                    continue                  # live drops it from today's scan

            if pend is None:
                if verdict[j] == HIGH_CONF and sig.pivot[p_row] > 0:
                    self._pending[slot] = (float(sig.pivot[p_row]), float(sig.vcp_low[p_row]),
                                           sim_date + watch_days)
                    st["pivot_watch_added"] += 1
                else:
                    self._count_verdict(int(verdict[j]))
                continue

            if self._try_entry(slot, pend, p_row, sim_date, ti, regime_mult, available_cash):
                available_cash = self._available_cash_after
                if len(self._open) >= config.MAX_POSITIONS:
                    cutoff = int(j) + 1
                    break

        reached = valid.copy()
        reached[cutoff:] = False
        st["data_ok"] += int(reached.sum())
        st["analyse_called"] += int(reached.sum())
        quiet = reached & ~event
        if quiet.any():
            counts = np.bincount(verdict[quiet], minlength=5)
            st["fail_tt"] += int(counts[FAIL_TT])
            st["fail_vcp"] += int(counts[FAIL_VCP])
            st["fail_tightness"] += int(counts[FAIL_TIGHTNESS])
            st["fail_other"] += int(counts[0])

    def _count_verdict(self, v: int) -> None:
        st = self._stats
        if v == FAIL_TT:
            st["fail_tt"] += 1
        elif v == FAIL_TIGHTNESS:
            st["fail_tightness"] += 1
        elif v == FAIL_VCP:
            st["fail_vcp"] += 1
        else:
            st["fail_other"] += 1

    def _try_entry(self, slot: int, pend: tuple, p_row: int, sim_date: date, ti: int,
                   regime_mult: float, available_cash: float) -> bool:
        """Breakout through the stored pivot on the scan candle → enter at today's open."""
        md, sig, st = self._md, self._sig, self._stats
        pivot = pend[0]
        o, h, l, c, vol = (float(md.open[p_row]), float(md.high[p_row]), float(md.low[p_row]),
                           float(md.close[p_row]), float(md.volume[p_row]))
        avg_vol = float(sig.volsma50[p_row])
        if h <= l or h <= 0 or avg_vol <= 0:
            return False
        if not (c > pivot
                and c >= h * config.STRONG_CLOSE_THRESHOLD
                and c > o
                and vol >= avg_vol * config.MIN_BREAKOUT_VOLUME_MULTIPLIER
                and (h - l) / l > config.PIVOT_BREAKOUT_MIN_DAY_RANGE_PCT
                and c <= pivot * (1 + config.MAX_ENTRY_EXTENSION_PCT)):
            return False
        del self._pending[slot]
        st["high_conf"] += 1
        if self._entry_filter is not None and not self._entry_filter(self, slot, p_row, ti, pend):
            st["filtered"] += 1
            return False
        entry_price = float(md.open[p_row + 1])
        stop_loss = calculate_stop_loss_price(entry_price)

        if c > 0:
            gap = (entry_price - c) / c
            if gap > config.MAX_GAP_UP_PCT or gap < -config.MAX_GAP_DOWN_PCT:
                st["gap_filtered"] += 1
                return False
        if self.parity.open_cap and entry_price > pivot * (1 + config.MAX_ENTRY_EXTENSION_PCT):
            st["open_cap_filtered"] += 1
            return False
        if self._slip:
            entry_price *= 1 + self._slip
            stop_loss = calculate_stop_loss_price(entry_price)
        size = calculate_position_size(
            portfolio_value=self._portfolio_value(ti), entry_price=entry_price,
            stop_loss_price=stop_loss, regime_multiplier=regime_mult,
            available_cash=available_cash)
        pos_value = size.position_value if size.is_valid else 0.0
        if not (size.is_valid and size.shares > 0
                and pos_value >= config.MIN_POSITION_VALUE * regime_mult):
            st["size_invalid"] += 1
            return False
        outflow = pos_value + calculate_buy_cost(pos_value, self._costs)
        if outflow > available_cash:
            st["size_invalid"] += 1
            return False
        self._open[slot] = _Pos(slot=slot, entry_date=sim_date, entry_ti=ti,
                                entry_price=entry_price, shares=size.shares,
                                stop_loss=stop_loss, vcp_low=pend[1])
        self.capital -= outflow
        self._available_cash_after = available_cash - outflow
        st["entered"] += 1
        return True

    # ── run ──────────────────────────────────────────────────────────────────

    def run(self) -> BacktestMetrics:
        t_start = time.perf_counter()
        trading_days = get_trading_days(self.start_date, self.end_date)
        self._prepare(trading_days)
        self._open: dict[int, _Pos] = {}
        self._pending: dict[int, tuple[float, float, date]] = {}
        peak = self.starting_capital
        prev_regime, regime_days = "", 0

        t0 = time.perf_counter()
        for ti, sim_date in enumerate(trading_days):
            self._sim_date = sim_date
            if self.parity.regime_exit and not self._ignore_regime:
                reg = self._regime_by_day.get(sim_date, ("BULL", 1.0))[0]
                if reg in (_BEAR, _EXTREME):
                    regime_days = regime_days + 1 if reg == prev_regime else 1
                else:
                    regime_days = 0
                prev_regime = reg
                if self._open and ((reg == _BEAR and regime_days >= config.REGIME_EXIT_BEAR_DAYS)
                                   or (reg == _EXTREME
                                       and regime_days >= config.REGIME_EXIT_EXTREME_VOL_DAYS)):
                    self._regime_exit(sim_date, ti, f"regime_exit_{reg.lower()}")
            self._process_exits(sim_date, ti)
            pv = self._portfolio_value(ti)
            self._equity_curve[sim_date] = pv
            peak = max(peak, pv)
            if (peak - pv) / peak >= config.MAX_DRAWDOWN_PCT:
                if not self._dd_halted_on:
                    self._dd_halted_on = sim_date
                self._stats["dd_halted_days"] += 1
                if self._dd_halt:
                    continue
            self._regime_name, mult = self._regime_by_day.get(sim_date, ("BULL", 1.0))
            if self._ignore_regime or (self._caution_full and mult > 0.0):
                mult = 1.0
            if mult <= 0.0:
                self._stats["regime_blocked_days"] += 1
                continue
            if self.parity.dd_pause and (peak - pv) / peak >= config.DD_HALT_ENTRIES_AT:
                self._stats["dd_paused_days"] += 1
                continue
            if self.parity.loss_limits and self._loss_limit_hit(sim_date, pv):
                self._stats["loss_limit_days"] += 1
                continue
            if len(self._open) < config.MAX_POSITIONS:
                self._scan(sim_date, ti, mult)

        # Force-close at each security's last candle on or before end_date
        for slot in list(self._open):
            self._close(slot, float(self._md.close[self._hi[slot] - 1]), trading_days[-1],
                        "backtest_end")
        self.timings["simulate"] = time.perf_counter() - t0

        ledger_error = abs(sum(t["realised_pnl"] for t in self._closed_trades)
                           - (self.capital - self.starting_capital))
        assert ledger_error < config.LEDGER_TOLERANCE_RUPEES, f"LEDGER MISMATCH err={ledger_error:.2f}"

        eq = pd.Series(self._equity_curve)
        eq.index = pd.to_datetime(list(self._equity_curve.keys()))
        metrics = analyse(trades=self._closed_trades, equity_curve=eq.sort_index(),
                          starting_capital=self.starting_capital,
                          start_date=self.start_date, end_date=self.end_date)
        self.timings["total"] = time.perf_counter() - t_start
        if self._verbose:
            print_report(metrics)
        return metrics
