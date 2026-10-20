"""Fast backtest for the rule-based strategies (mean reversion, momentum, breakout).

Same day loop as engine.FastBacktestEngine; entries and exits follow
backtest_engine's branches for these strategies:
  - scan reads the prior candle, entry fills at today's open, gap filter as vcp_v2
  - per-strategy slot caps inside the MAX_POSITIONS pool
  - fixed-%-of-portfolio sizing (× regime multiplier), MIN_POSITION_VALUE floor
  - exits: stop (gap-through fills at the open) → fixed target → strategy exit
    (MR: close > EMA5 / RSI(2) > 70 / 7-day time stop; momentum: 12-day time stop;
    momentum_v2-4: close < EMA21 / 25-day time stop; breakout: 7-day time stop;
    breakout_v2: vcp_v2's trailing ratchet, breakdown and 15-day time stop)

Strategy "all" runs mean_reversion + momentum + breakout in one book and takes the
best-scoring passing signal per stock, like the reference.

The entry verdicts come from rules.py; tests/test_fast_rules.py checks them
against the check_* functions row by row. Unlike vcp_v2, there is no frozen oracle
for the portfolio loop of these strategies.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import numpy as np

from backtest.cost_model import calculate_buy_cost
from backtest.data_loader import load_nifty_ohlcv
from backtest.fast.engine import FastBacktestEngine, Parity, _Pos
from backtest.fast.rules import PASS, RuleSignals, bo_score, load_rule_signals, mom_score, mr_score
from config.config import config

RULE_STRATEGIES = ("mean_reversion", "momentum", "momentum_v2", "momentum_v3", "momentum_v4",
                   "breakout", "breakout_v2", "all")

# verdict code → funnel counter, per verdict family
_FAIL_KEYS = {
    "mr": {0: "mr_fail_other", 2: "mr_fail_trend_gate", 3: "mr_fail_rsi_entry"},
    "mom": {0: "mom_fail_other", 2: "mom_fail_ema", 3: "mom_fail_volume",
            4: "mom_fail_proximity", 5: "mom_fail_rsi"},
    "bo": {0: "bo_fail_other", 2: "bo_fail_new_high", 3: "bo_fail_volume",
           4: "bo_fail_ema", 5: "bo_fail_rsi"},
}
_RULE_STATS = tuple(k for d in _FAIL_KEYS.values() for k in d.values()) + (
    "mom3_fail_stage2", "mom3_fail_rs", "bull_only_skipped_days")


def _family(strategy: str) -> str:
    if strategy == "mean_reversion":
        return "mr"
    return "mom" if strategy.startswith("momentum") else "bo"


def _cap(strategy: str) -> int:
    return {"mr": config.MR_MAX_POSITIONS, "mom": config.MOMENTUM_MAX_POSITIONS,
            "bo": config.BREAKOUT_MAX_POSITIONS}[_family(strategy)]


def _stop_pct(strategy: str) -> float:
    if strategy == "mean_reversion":
        return config.MR_STOP_PCT
    if strategy == "momentum":
        return config.MOMENTUM_STOP_PCT
    if strategy.startswith("momentum"):
        return config.MOMENTUM2_STOP_PCT
    return config.BREAKOUT_STOP_PCT


def _target_pct(strategy: str) -> float:
    return {"momentum": config.MOMENTUM_TARGET_PCT,
            "breakout": config.BREAKOUT_TARGET_PCT}.get(strategy, 0.0)


def _size_pct(strategy: str) -> float:
    return {"mr": config.MR_POSITION_SIZE_PCT, "mom": config.MOMENTUM_POSITION_SIZE_PCT,
            "bo": config.BREAKOUT_POSITION_SIZE_PCT}[_family(strategy)]


class FastRuleEngine(FastBacktestEngine):
    """FastBacktestEngine for mean_reversion / momentum* / breakout* / all."""

    def __init__(self, *args, strategy: str = "mean_reversion",
                 rules: Optional[RuleSignals] = None, **kwargs) -> None:
        if strategy not in RULE_STRATEGIES:
            raise ValueError(f"unknown rule strategy {strategy!r}; one of {RULE_STRATEGIES}")
        # The benched rule strategies keep the pre-parity engine unless asked: their
        # published results were produced without the live-parity rules.
        kwargs.setdefault("parity", Parity.off())
        super().__init__(*args, **kwargs)
        self.strategy = strategy
        self._rules = rules
        self._members = (("mean_reversion", "momentum", "breakout") if strategy == "all"
                         else (strategy,))
        self._caps = {s: _cap(s) for s in self._members}
        self._fail_keys = {s: _FAIL_KEYS[_family(s)] for s in self._members}
        self._n_open: dict[str, int] = {}
        for k in _RULE_STATS:
            self._stats[k] = 0

    def _prepare(self, trading_days: list[date]) -> None:
        super()._prepare(trading_days)
        self._rules = self._rules or load_rule_signals(self._md)
        self._nifty_ret = None
        if self.strategy in ("momentum_v3", "momentum_v4"):
            # Nifty return over the RS lookback, using closes dated before sim_date
            lb = config.MOMENTUM3_RS_LOOKBACK
            nifty = load_nifty_ohlcv(
                trading_days[0] - timedelta(days=config.BACKTEST_WARMUP_CALENDAR_DAYS), self.end_date)
            nd = nifty.index.values.astype("datetime64[D]")
            nc = nifty["close"].astype(float).to_numpy()
            k = np.searchsorted(nd, np.array(trading_days, dtype="datetime64[D]"), side="left")
            self._nifty_ret = np.where(
                k > lb, nc[np.maximum(k - 1, 0)] / nc[np.maximum(k - lb - 1, 0)] - 1, np.nan)

    # ── exits ────────────────────────────────────────────────────────────────

    def _exit_signal(self, slot: int, p: _Pos, r: int, ti: int) -> Optional[tuple[float, str]]:
        close = float(self._md.close[r])
        if p.target_price > 0 and close >= p.target_price:
            return p.target_price, "profit_target"
        s, rs = p.strategy, self._rules
        held = ti - p.entry_ti
        if s == "breakout_v2":
            return self._vcp_exit(slot, p, r, ti)
        if s == "mean_reversion":
            if close > float(rs.ema5[r]):
                return close, "mr_ema5_exit"
            if float(rs.rsi2[r]) > config.MR_RSI_EXIT:
                return close, "mr_rsi_exit"
            if held >= config.MR_TIME_STOP_DAYS:
                return close, "time_stop"
        elif s == "momentum":
            if held >= config.MOMENTUM_TIME_STOP_DAYS:
                return close, "time_stop"
        elif s.startswith("momentum"):
            if close < float(rs.ema21[r]):
                return close, "trend_break"
            if held >= config.MOMENTUM2_TIME_STOP_DAYS:
                return close, "time_stop"
        elif s == "breakout":
            if held >= config.BREAKOUT_TIME_STOP_DAYS:
                return close, "time_stop"
        return None

    # ── entries ──────────────────────────────────────────────────────────────

    def _verdicts(self, strategy: str, rows: np.ndarray, n: np.ndarray) -> np.ndarray:
        rs = self._rules
        v = getattr(rs, _family(strategy))[rows].copy()
        if strategy == "mean_reversion":
            # check_mean_reversion needs MR_EMA_TREND + 10 bars in prev_day_df
            v[(n - 1) < config.MR_EMA_TREND + config.MR_MIN_EXTRA_BARS] = 0
        return v

    def _open_count(self, strategy: str) -> int:
        if self.strategy != "all":
            return len(self._open)          # single strategy: every open position counts
        return self._n_open.get(strategy, 0)

    def _scan(self, sim_date: date, ti: int, regime_mult: float) -> None:
        # momentum_v4 trades only in a confirmed BULL regime — part of its rules,
        # so it applies even with ignore_regime.
        if self.strategy == "momentum_v4" and self._regime_name != "BULL":
            self._stats["bull_only_skipped_days"] += 1
            return
        md, st = self._md, self._stats
        U = self._day_slots[ti]
        if len(U) == 0:
            return
        n = self._n[ti, U].astype(np.int64)
        valid = n >= config.TT_MIN_HISTORY_DAYS
        if self._open:
            valid &= ~np.isin(U, np.fromiter(self._open, np.int64))
        prev = np.where(valid, self._lo[U] + n - 2, 0)        # scan candle (prev_day_df[-1])
        verdicts = {s: self._verdicts(s, prev, n) for s in self._members}
        passing = np.zeros(len(U), dtype=bool)
        for v in verdicts.values():
            passing |= v == PASS
        passing &= valid
        quiet = valid & ~passing     # no strategy passes: fails counted in bulk below
        if self.strategy == "all":
            self._n_open = {}
            for p in self._open.values():
                self._n_open[p.strategy] = self._n_open.get(p.strategy, 0) + 1

        available_cash = self._available_cash()
        # Slot caps change only on an entry, so quiet members are counted per
        # stretch between entries, under the cap state that held for that stretch.
        last = 0
        cutoff = len(U)
        for j in np.nonzero(passing)[0]:
            j = int(j)
            slot, g = int(U[j]), int(prev[j])
            chosen = self._choose(verdicts, j, g, slot, ti)
            if chosen is None:
                continue
            if self._enter(chosen, slot, g, sim_date, ti, regime_mult, available_cash):
                available_cash = self._available_cash_after
                self._count_fails(verdicts, quiet, last, j)
                last = j + 1
                self._n_open[chosen] = self._n_open.get(chosen, 0) + 1
                if len(self._open) >= config.MAX_POSITIONS:
                    cutoff = j + 1
                    break
        self._count_fails(verdicts, quiet, last, cutoff)
        reached = int(valid[:cutoff].sum())
        st["data_ok"] += reached
        st["analyse_called"] += reached

    def _count_fails(self, verdicts: dict, quiet: np.ndarray, a: int, b: int) -> None:
        if b <= a:
            return
        st = self._stats
        for s, v in verdicts.items():
            # "all" skips a strategy's check when its slots are full (reference order)
            if self.strategy == "all" and self._open_count(s) >= self._caps[s]:
                continue
            seg = v[a:b][quiet[a:b]]
            if len(seg):
                counts = np.bincount(seg, minlength=6)
                for code, key in self._fail_keys[s].items():
                    st[key] += int(counts[code])

    def _choose(self, verdicts: dict, j: int, g: int, slot: int, ti: int) -> Optional[str]:
        """Strategy to enter for this stock today, or None."""
        st = self._stats
        if self.strategy != "all":
            s = self.strategy
            if verdicts[s][j] != PASS:
                return None
            if s in ("momentum_v3", "momentum_v4"):
                if not self._rules.stage2[g]:
                    st["mom3_fail_stage2"] += 1
                    return None
                lb = config.MOMENTUM3_RS_LOOKBACK
                stock_ret = (float(self._md.close[g]) / float(self._md.close[g - lb]) - 1
                             if g - lb >= int(self._a[slot]) else np.nan)
                nifty_ret = self._nifty_ret[ti]
                if not (stock_ret > nifty_ret):         # NaN (too little history) fails too
                    st["mom3_fail_rs"] += 1
                    return None
            if self._open_count(s) >= self._caps[s]:
                return None
            st["high_conf"] += 1
            return s

        best, best_score = None, -np.inf
        first = int(self._a[slot])
        for s in self._members:                     # MR, momentum, breakout — reference order
            if self._open_count(s) >= self._caps[s]:
                continue
            v = verdicts[s][j]
            if v != PASS:
                st[self._fail_keys[s][int(v)]] += 1
                continue
            score = (mr_score(self._rules, self._md, g, first) if s == "mean_reversion"
                     else mom_score(self._rules, g) if s == "momentum"
                     else bo_score(self._rules, self._md, g))
            if best is None or score > best_score:
                best, best_score = s, score
        if best is not None:
            st["high_conf"] += 1
        return best

    def _enter(self, strategy: str, slot: int, g: int, sim_date: date, ti: int,
               regime_mult: float, available_cash: float) -> bool:
        md, st = self._md, self._stats
        entry_price = float(md.open[g + 1])
        prev_close = float(md.close[g])
        if prev_close > 0:
            gap = (entry_price - prev_close) / prev_close
            if gap > config.MAX_GAP_UP_PCT or gap < -config.MAX_GAP_DOWN_PCT:
                st["gap_filtered"] += 1
                return False
        if available_cash < config.MIN_POSITION_VALUE * regime_mult:
            st["size_invalid"] += 1          # floor <= pos_value <= cash is impossible
            return False
        pos_value = self._portfolio_value(ti) * _size_pct(strategy) * regime_mult
        shares = max(1, int(pos_value / entry_price))
        pos_value = shares * entry_price
        if not (config.MIN_POSITION_VALUE * regime_mult <= pos_value <= available_cash):
            st["size_invalid"] += 1
            return False
        outflow = pos_value + calculate_buy_cost(pos_value, self._costs)
        if outflow > available_cash:
            st["size_invalid"] += 1
            return False
        tp = _target_pct(strategy)
        self._open[slot] = _Pos(slot=slot, entry_date=sim_date, entry_ti=ti,
                                entry_price=entry_price, shares=shares,
                                stop_loss=entry_price * (1 - _stop_pct(strategy)),
                                vcp_low=None, strategy=strategy,
                                target_price=entry_price * (1 + tp) if tp else 0.0)
        self.capital -= outflow
        self._available_cash_after = available_cash - outflow
        st["entered"] += 1
        return True
