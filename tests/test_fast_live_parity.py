"""Live-parity rules in the fast engine (Parity, 2026-10-19).

Three kinds of check:
  1. The replayed pre-scan filters give the same verdict as the live functions in
     scanner/stock_filter.py, row by row on real data.
  2. The regime simulation applies live's VIX tiers, precedence, size multipliers
     and upgrade confirmation (synthetic series, so every transition is exercised).
  3. A full-window run with every rule on never breaks one of them: no entry above
     pivot + cap, no position held past the regime-exit day, no entry inside the
     drawdown pause or a loss limit, no trade in a stock live's pre-scan would drop.
The oracle fixtures (tests/test_fast_engine.py) pin the engine with Parity.off().
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).parent.parent
DB = REPO / "data" / "reconstructed.db"
VIX = REPO / "data" / "india_vix.csv"

needs_data = pytest.mark.skipif(not (DB.exists() and VIX.exists()),
                                reason="reconstructed.db / india_vix.csv not present here")


# ── 2. regime simulation (synthetic, no data files) ─────────────────────────

def _bull_nifty(days: pd.DatetimeIndex) -> pd.DataFrame:
    close = pd.Series(np.linspace(100.0, 160.0, len(days)), index=days)   # steady uptrend
    return pd.DataFrame({"close": close})


def test_regime_sim_vix_tiers_and_upgrade_confirmation():
    from backtest.regime_sim import build_regime_series
    from config.config import config

    days = pd.bdate_range("2020-01-01", periods=400)
    nifty = _bull_nifty(days)
    breadth = pd.Series(0.7, index=days)
    vix = pd.Series(15.0, index=days)
    t = days[300:]
    vix[t[0]] = 30.0              # HIGH_VOLATILITY
    vix[t[1]] = 40.0              # EXTREME_VOLATILITY
    vix[t[2]:] = 15.0             # calm again → upgrade needs confirmation
    trading = [d.date() for d in days[290:320]]
    out = build_regime_series({}, nifty, trading, breadth=breadth, vix=vix)

    def at(i):                    # regime on the trading day after t[i] (T uses T-1's close)
        return out[days[300 + i + 1].date()]

    assert out[days[300].date()] == ("BULL", 1.0)
    assert at(0) == ("HIGH_VOLATILITY", config.REGIME_SIZE_MULTIPLIER["HIGH_VOLATILITY"])
    assert at(1) == ("EXTREME_VOLATILITY", 0.0)            # downgrade: immediate
    # calm VIX from t[2]: upgrade to BULL is held one day, confirmed on the second
    assert at(2)[0] == "EXTREME_VOLATILITY"
    assert at(3) == ("BULL", 1.0)


def test_regime_sim_matches_live_classifier_without_hysteresis():
    """Every raw classification the sim makes is live's classify_regime on T-1 data."""
    from backtest.regime_sim import build_regime_series
    from regime.regime_detector import Regime, classify_regime

    days = pd.bdate_range("2020-01-01", periods=500)
    rng = np.random.default_rng(7)
    nifty = pd.DataFrame({"close": 100 * np.exp(np.cumsum(rng.normal(0, 0.012, len(days))))},
                         index=days)
    breadth = pd.Series(rng.uniform(0.3, 0.75, len(days)), index=days)
    vix = pd.Series(rng.uniform(10, 40, len(days)), index=days)
    out = build_regime_series({}, nifty, [d.date() for d in days[1:]], breadth=breadth, vix=vix)

    close = nifty["close"]
    ema = close.ewm(span=200, adjust=False, min_periods=200).mean()
    hi = close.rolling(20).max()
    trend = hi > hi.shift(20)
    rank = {"EXTREME_VOLATILITY": 1, "BEAR": 2, "HIGH_VOLATILITY": 3, "CAUTIOUS": 4, "BULL": 5}
    for i in range(201, len(days)):
        prev = days[i - 1]
        raw = classify_regime(close[prev], ema[prev], breadth[prev], vix[prev], bool(trend[prev]))
        got, before = out[days[i].date()][0], out[days[i - 1].date()][0]
        if rank[raw.value] <= rank[before]:
            assert got == raw.value               # downgrade or no change: same day
        else:
            assert got in (before, raw.value)     # upgrade: held, or confirmed
    assert Regime.EXTREME_VOLATILITY.value in {r for r, _ in out.values()}


def test_loss_limit_windows_and_entry_timing():
    """Live DrawdownMonitor semantics: realised P&L by exit date over the day, the last
    WEEKLY/MONTHLY_LOSS_WINDOW_DAYS (inclusive), as a share of equity. Today's exits
    count only if they filled at the open (before the 9:30 entry window)."""
    from backtest.fast.engine import FastBacktestEngine
    from config.config import config

    e = FastBacktestEngine(date(2024, 1, 1), date(2024, 12, 31), verbose=False)
    pv, today = 100_000.0, date(2024, 6, 14)
    day_lim = config.DAILY_LOSS_LIMIT_PCT * pv
    e._exit_log = [(today, -day_lim, False)]                 # intraday stop: after entries
    assert not e._loss_limit_hit(today, pv)
    e._exit_log = [(today, -day_lim, True)]                  # gap-through stop at the open
    assert e._loss_limit_hit(today, pv)
    wk = today - timedelta(days=config.WEEKLY_LOSS_WINDOW_DAYS)
    e._exit_log = [(wk, -config.WEEKLY_LOSS_LIMIT_PCT * pv, False)]   # window edge, inclusive
    assert e._loss_limit_hit(today, pv)
    e._exit_log = [(wk - timedelta(days=1), -config.WEEKLY_LOSS_LIMIT_PCT * pv, False)]
    assert not e._loss_limit_hit(today, pv)
    mo = today - timedelta(days=config.MONTHLY_LOSS_WINDOW_DAYS)
    e._exit_log = [(mo, -config.MONTHLY_LOSS_LIMIT_PCT * pv / 2, False),
                   (today - timedelta(days=1), -config.MONTHLY_LOSS_LIMIT_PCT * pv / 2, False)]
    assert e._loss_limit_hit(today, pv)


# ── shared real-data fixture ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def real(request):
    from config.config import config as cfg
    old = cfg.DB_PATH
    cfg.DB_PATH = str(REPO / "data")
    from backtest.fast.data import Universe, load_market_data
    from backtest.fast.signals import load_signals
    md = load_market_data()
    yield {"md": md, "sig": load_signals(md), "uni": Universe()}
    cfg.DB_PATH = old


# ── 1. filters: replay == live functions ────────────────────────────────────

@needs_data
def test_live_filters_match_live_functions_row_by_row(real):
    from backtest.fast.live_filters import LiveFilters
    from config.config import config
    from config.holidays import get_trading_days
    from scanner.stock_filter import (filter_circuit_breaker, filter_data_quality,
                                      filter_liquidity, filter_price)

    md = real["md"]
    rng = np.random.default_rng(11)
    tdays = np.array(get_trading_days(date(2020, 1, 1), date(2026, 12, 31)), dtype="datetime64[D]")
    tnum = tdays.astype(np.int64)
    lf = LiveFilters(md, market_days=tnum)     # live's calendar, so the comparison is 1:1
    seen = {}
    ks = rng.integers(0, len(md.isins), 1500)
    for k in ks:
        a, b = int(md.offsets[k]), int(md.offsets[k + 1])
        if b - a < 3:
            continue
        last = int(rng.integers(a, b - 1))
        nxt = np.searchsorted(tnum, md.dates[last], side="right")
        if nxt >= len(tnum):
            continue
        today = int(tnum[nxt])

        got = lf.check(a, last, today)

        # live's view: HISTORICAL_DAYS of candles up to yesterday, adjusted as of today
        sl = slice(a, last + 1)
        idx = pd.to_datetime(md.dates[sl].astype("datetime64[D]"))
        f = md.adj[last]
        df = pd.DataFrame({"open": md.open[sl] / f, "high": md.high[sl] / f, "low": md.low[sl] / f,
                           "close": md.close[sl] / f, "volume": md.volume[sl] * f}, index=idx)
        today_d = np.datetime64(today, "D").astype(date)
        df = df[df.index >= pd.Timestamp(today_d - timedelta(days=config.HISTORICAL_DAYS))]
        want = None
        for name, (ok, _) in (("price", filter_price("X", float(df["close"].iloc[-1]))),
                              ("liquidity", filter_liquidity("X", df)),
                              ("circuit_breaker", filter_circuit_breaker("X", df)),
                              ("data_quality", filter_data_quality("X", df, check_date=today_d))):
            if not ok:
                want = name
                break
        assert got == want, (md.isins[k], str(today_d), got, want)
        seen[want] = seen.get(want, 0) + 1
    # the sample must exercise every outcome, or the test proves nothing
    for name in (None, "price", "liquidity", "data_quality"):
        assert seen.get(name, 0) > 0, seen


# ── 3. invariants of a full parity-on run ───────────────────────────────────

@pytest.fixture(scope="module")
def parity_run(real):
    from backtest.fast.engine import FastBacktestEngine

    pivots: dict[tuple[int, int], float] = {}

    def record(engine, slot, p_row, ti, pend):
        pivots[(slot, ti)] = pend[0]
        return True

    e = FastBacktestEngine(start_date=date(2021, 9, 1), end_date=date(2026, 8, 31),
                           starting_capital=50_000.0, dd_halt=False, market=real["md"],
                           signals=real["sig"], universe=real["uni"], verbose=False,
                           entry_filter=record)
    e.run()
    return e, pivots


@needs_data
def test_data_quality_counts_exchange_days_not_the_holiday_calendar(real):
    """2022-01-26 (Republic Day) is a trading day in config/holidays.py (no 2022 list),
    but NSE was shut. The replay must not count it as a gap for every stock."""
    from backtest.fast.live_filters import LiveFilters
    md = real["md"]
    rd = np.datetime64("2022-01-26", "D").astype(np.int64)
    assert rd not in set(np.unique(md.dates).tolist())
    lf = LiveFilters(md)
    assert rd not in set(lf._tdays.tolist())


@needs_data
def test_parity_run_uses_every_rule(parity_run):
    e, _ = parity_run
    s = e._stats
    assert s["entered"] > 0
    assert s["regime_exits"] > 0
    # loss limits never bind on the full window at ₹50k (checked directly below)
    assert s["prefilter_price"] + s["prefilter_liquidity"] + s["prefilter_data_quality"] > 0
    assert any(r == "EXTREME_VOLATILITY" or r == "HIGH_VOLATILITY"
               for r, _ in e._regime_by_day.values()), "VIX tiers never fired"


@needs_data
def test_no_entry_above_pivot_cap(parity_run):
    from config.config import config
    e, pivots = parity_run
    days = sorted(e._regime_by_day)
    ti_of = {d: i for i, d in enumerate(days)}
    for t in e._closed_trades:
        slot = e._isins.index(t["isin"])
        pivot = pivots[(slot, ti_of[t["entry_date"]])]
        assert t["entry_price"] <= pivot * (1 + config.MAX_ENTRY_EXTENSION_PCT) + 1e-9, t


@needs_data
def test_no_position_held_past_regime_exit_day(parity_run):
    from config.config import config
    e, _ = parity_run
    days = sorted(e._regime_by_day)
    exit_days, run, prev = set(), 0, ""
    for d in days:
        r = e._regime_by_day[d][0]
        run = (run + 1 if r == prev else 1) if r in ("BEAR", "EXTREME_VOLATILITY") else 0
        prev = r
        if (r == "BEAR" and run >= config.REGIME_EXIT_BEAR_DAYS) or (
                r == "EXTREME_VOLATILITY" and run >= config.REGIME_EXIT_EXTREME_VOL_DAYS):
            exit_days.add(d)
    for t in e._closed_trades:
        held = [d for d in exit_days if t["entry_date"] <= d < t["exit_date"]]
        assert not held, (t["symbol"], t["entry_date"], t["exit_date"], held[:3])
        if t["exit_reason"].startswith("regime_exit"):
            assert t["exit_date"] in exit_days


@needs_data
def test_no_entry_in_drawdown_pause_or_loss_limit(parity_run):
    from config.config import config
    e, _ = parity_run
    eq = pd.Series(e._equity_curve).sort_index()
    dd = 1 - eq / eq.cummax()
    trades = e._closed_trades
    log = e._exit_log
    for t in trades:
        d = t["entry_date"]
        assert dd[d] < config.DD_HALT_ENTRIES_AT, (d, dd[d])
        pv = eq[d]
        def realised(since):
            return sum(p for x, p, o in log if since <= x <= d and (x < d or o))
        assert realised(d) / pv > -config.DAILY_LOSS_LIMIT_PCT
        assert realised(d - timedelta(days=config.WEEKLY_LOSS_WINDOW_DAYS)) / pv > -config.WEEKLY_LOSS_LIMIT_PCT
        assert realised(d - timedelta(days=config.MONTHLY_LOSS_WINDOW_DAYS)) / pv > -config.MONTHLY_LOSS_LIMIT_PCT


@needs_data
def test_no_trade_in_a_stock_the_prescan_drops(parity_run):
    e, _ = parity_run
    md = e._md
    for t in e._closed_trades:
        slot = e._isins.index(t["isin"])
        today = np.datetime64(t["entry_date"], "D").astype(np.int64)
        row = int(np.searchsorted(md.dates[e._lo[slot]:e._hi[slot]], today, side="left")) \
            + int(e._lo[slot]) - 1                       # the scan candle (yesterday)
        assert e._filters.check(int(e._a[slot]), row, today) is None, t["symbol"]
