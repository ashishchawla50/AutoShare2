# CURRENT_SYSTEM_AUDIT.md

**Date:** 2026-08-11
**Scope:** Phase 1 — full read of the repository, both pipelines traced end to end.
**Status:** Audit only. No strategy code was modified.
**Method:** Code is the source of truth. Documentation claims were checked against
the code that implements them, and discrepancies are recorded as findings.

---

## 1. What the system actually is

One validated engine, `vcp_v2`, in two independent implementations:

| | Live | Backtest |
|---|---|---|
| Orchestrator | `main.py` (`TradingBot`) | `backtest/backtest_engine.py` (`BacktestEngine`) |
| Signal | `main.py:231 run_strategy_scan` | `backtest_engine.py:634` (`vcp_v2` branch of `_scan_for_entries`) |
| Exits | `main.py:891 check_exits` | `backtest_engine.py:310 _process_exits` |

These are **two separate implementations of the same intended rules**, not one
engine with two executors. Every rule below was checked in both. They differ in
eight places (Section 4 and `docs/LIVE_BACKTEST_PARITY.md`).

### Live path
`refresh_universe_ohlcv` (main.py:183) → `run_pre_scan` (:205, applies 9 filters via
`run_batch_filters`) → `run_strategy_scan` (:231, two-stage pivot watch) →
`rank_setups` (`scanner/opportunity_ranker.py:91`) → `rank_opportunities` (main.py:505,
slot caps) → `check_entries` (:599, gap → size → risk gate → order → GTT) →
`check_exits` (:891) → `portfolio_manager` ledger.

### Backtest path
`_preload_data` (:938) → per day `_process_exits` (:310) → `_calc_portfolio_value` (:873)
→ drawdown halt (:289) → regime gate (:303) → `_scan_for_entries` (:421) →
`calculate_position_size` → in-memory position dict → `_close_position` (:838) →
`analyse` (`backtest/performance_analyzer.py:69`).

### Verified working
- 163/163 tests pass (`pytest -q`, run 2026-08-11).
- No look-ahead in `swing_highs`/`swing_lows` (`utils/indicator_utils.py:95,115`): the
  loop is `range(left, len(high) - right)`, so a swing is only ever marked once
  `right` bars have confirmed it. Correct.
- No look-ahead in the regime series (`backtest/regime_sim.py:97`): `raw.shift(1)`
  assigns day T the classification from T−1's close.
- No look-ahead in signal timing: `_scan_for_entries` splits `prev_day_df =
  sim_df.iloc[:-1]` for the signal and enters at `today_row["open"]`. Correct
  next-open logic.
- Gap-through-stop is modelled honestly (`_process_exits:332`): fill is
  `min(stop, today_open)`, not the fiction of getting the stop price on a gap.
- The ledger invariant assertion (`run():172`) is a genuine and valuable guard.

---

## 2. Findings by severity

Numbering is used throughout the other docs.

### BLOCKER-1 — The point-in-time universe is fabricated

`backtest/point_in_time_universe.py:46 get_universe()` reads the
`universe_snapshots` table. The **only two writers** of that table are:

- `data_fetcher/universe_fetcher.py:281` (`__main__` block)
- `main.py:1167` (`refresh_universe_snapshot`)

Both call `save_universe_snapshot(fetch_universe_symbols(), <today>)` —
i.e. they persist the **current live NSE composition**. There is no historical
index-membership source anywhere in the repository, and no code path that could
produce one. Any row dated 2019 or 2021 was written by backdating today's list.

Compounding it, `get_universe()` lines 86–112: for any date before the earliest
snapshot, it loads that earliest snapshot **once** and reuses it for every such
day. So a single backdated snapshot supplies the universe for the entire
backtest window.

This produces two biases at once, and the second is the more damaging:

1. **Survivorship** — names that were delisted, or that fell out of the index
   after collapsing, are absent. Their losses never enter the P&L.
2. **Inclusion / look-ahead selection** — the list is composed of stocks that had
   *already earned* their Nifty 500 membership by July 2026. For a strategy that
   buys uptrend continuation, this is close to being told the answer: the
   candidate pool was filtered on the very property the strategy tries to
   predict.

**Consequence: every published performance number in this repository is void** —
PF 1.11 dev, PF 1.61 OOS, PF 1.75 / 1.84 / 1.77 in `VALIDATION_2026_08.md`, the
sizing study, and all four rejection verdicts (momentum, breakout, mean
reversion). The rejections are void in a specific and useful direction: they were
measured on a pool biased *in their favour*, so a rejection under bias is
stronger evidence of failure than a pass under bias is of success. The vcp_v2
pass is the number that cannot be trusted.

This must be fixed before any other measurement is worth taking.

### BLOCKER-2 — No corporate-action adjustment discipline in the price cache

`data_fetcher/ohlcv_fetcher.py:101 _fetch_from_kite` requests
`start_date = today - days` (default 500) and `_save_to_cache:171` writes with
`INSERT OR REPLACE`. Rows older than the refetch window are never revisited.

There is no adjustment logic in the codebase, no verification that vendor data
is adjusted, and no test covering a split or bonus. Two concrete consequences:

- If the vendor re-serves history on a new adjustment basis after a split, the
  cache ends up **mixed-basis**, with an artificial price discontinuity frozen at
  the refetch boundary. Nothing detects this.
- An unadjusted 1:5 split is a −80% single-bar move. That is a fabricated stop-out,
  a fabricated "prior uptrend ≥30%", and a fabricated volatility contraction.

`utils/data_utils.flag_data_anomalies` and `validate_ohlcv` check volume spikes
and OHLC sanity — neither would catch this, because a split-adjusted bar is
internally consistent.

Severity depends on how many splits/bonuses occurred in the traded names over
2021–2026, which is measurable and not yet measured.

### BLOCKER-3 — Point-in-time fundamentals do not exist

The `fundamentals` table is keyed by symbol with **no as-of date** (verified
against `data/fundamentals.db` schema; `get_fundamentals`,
`data_fetcher/fundamental_fetcher.py:157`, returns one current row per symbol).

The backtest does not currently read fundamentals, so **there is no live leak
today**. This is recorded as a blocker because it constrains Phase 3: "make the
backtest match live" cannot be implemented by porting live's market-cap, D/E,
pledge or ROE filters into the backtest — doing so would inject a look-ahead bias
worse than the divergence it closes. The same applies to `filter_surveillance`,
which reads the current ASM/ESM list.

### MAJOR-4 — The backtest applies no stock filters at all

`backtest_engine.py:39` imports `apply_all_filters`. It is **never called**
(verified by grep across the repo). Live runs all 9 via `run_batch_filters`
(main.py:226), which is what reduces the Nifty 500 to the ~432-stock live pond.

The dead import makes the file read as though filtering happens. It does not.

### MAJOR-5 — The backtest does not rank; live does

`_scan_for_entries:436` is `for symbol in universe:` — it takes the **first**
signals it encounters in universe list order (effectively alphabetical) until
slots or cash run out. Live scores every candidate
(`opportunity_ranker.score_setup`) and takes the highest composite first.

On any day with more signals than free slots — precisely the high-opportunity
days that matter most for compounding — the two systems buy **different stocks**.

### MAJOR-6 — Relative strength exists only in live

`calculate_rs_universe` is imported at `backtest_engine.py:38` and never called.
In live, RS carries **35% of the composite score**, the single largest weight, so
it materially determines which setup is taken. The backtest has no RS concept
anywhere.

(Note: the RS ≥ 70 percentile *hard gate* was removed on 2026-08-10 —
`opportunity_ranker.py:15-20`. `CLAUDE.md` and `DIVERGENCE_INVENTORY.md` still
list divergence A1 as OPEN. That is stale documentation; the gate is gone. The
35%-weight ranking divergence documented here is what remains.)

### MAJOR-7 — Live force-exits on regime; the backtest never does

`main.py:989-997` calls `check_regime_exit` (`exit/time_stop.py`) for every VCP
position. `_process_exits` has no equivalent. In a 2022-style deterioration live
liquidates positions the backtest rides through. This changes drawdown, holding
period and P&L, all in the same direction, and it is exactly the mechanism that
would matter most in the worst historical period.

### MAJOR-8 — The gap filter differs

`evaluate_gap` (`strategy/breakout_detector.py:134`) applies **three** rules:
gap > +5% → SKIP; **`today_open > pivot × 1.05` → SKIP**; gap < −3% → SKIP.

The backtest (`_scan_for_entries:722-731`) implements only the first and third.
The backtest therefore accepts entries that open more than 5% past the pivot —
chase entries live refuses. This biases the backtest toward extended entries with
worse R/R.

### MAJOR-9 — Paper mode models neither gaps nor intraday stops

Two lines in `check_entries` (main.py:633-635):

```python
ltp_data = self._kite.get_ltp([...]) if not config.PAPER_TRADING_MODE else {}
today_open = ltp_data.get(f"NSE:{setup.symbol}", float(df["close"].iloc[-1]))
prev_close  = float(df["close"].iloc[-1])
```

In paper mode `today_open` falls back to `prev_close`, so `gap_pct` is
**always exactly 0**. The gap filter can never fire, and paper entries fill at
yesterday's close instead of today's open — overnight gap risk is entirely absent
from paper results.

Separately, paper has no GTT. `check_exits` tests `should_exit_on_stop(close,
stop)` against the **daily close** (main.py:912,919), whereas live is protected
intraday by the GTT and `GTTBackupMonitor`, and the backtest triggers on
`current_low` (`_process_exits:332`). A position that trades through its stop
intraday and closes above it is stopped out in live and in the backtest, but
survives in paper.

Both errors push the same way: **paper results are optimistically biased against
both live and backtest.** Since the plan is to paper-trade before committing
money, this needs fixing before paper trading is used as evidence.

### MAJOR-10 — `MAX_POSITIONS = 8` is dead config; the real ceiling is 4

With `HARD_STOP_PCT = 7%` and `RISK_PER_TRADE_PCT = 2%`, the risk-based share
count implies 2/7 ≈ 28.6% of portfolio, so `MAX_POSITION_SIZE_PCT = 20%`
**always binds** for vcp_v2 (`position_sizer.py:88`). With
`MIN_CASH_RESERVE_PCT = 20%`, available cash runs out after four 20% positions:

| Position | Size | Cash after | Available (cash − 20% reserve) |
|---|---|---|---|
| 1 | 20% | 80% | 60% |
| 2 | 20% | 60% | 40% |
| 3 | 20% | 40% | 20% |
| 4 | 20% | 20% | **0%** |
| 5 | — | — | blocked |

So the live portfolio is structurally **4 concentrated positions at 20% each,
80% invested, 20% permanently idle** — not the 8 the config and documentation
describe. This is identical in both engines, so it is not a parity break (correction 2026-10-11: in **live** mode it was
not identical. `get_available_cash` read the raw Zerodha balance, including the owner's personal cash, so
the ceiling did not bind live until the audit-H1 fix); it is a
strategy-design fact that no document currently states, and it bears directly on
capital utilization, concentration and correlated-loss risk.

### MAJOR-11 — Execution realism is a single constant

`portfolio/pnl_calculator.py:40` — `slippage_pct = 0.001` (0.1% per side), applied
as a fee inside the cost model. There is no bid-ask spread model, no market-impact
term that scales with order size relative to volume, no partial or failed fills,
and no ADV-based cap on position size.

At ₹50k this is defensible. The Phase 11 scale test (₹2L → ₹10L) asks a question
this model **cannot answer**, because the one variable that should degrade with
size is held constant. Any scaling conclusion drawn from the current engine would
be an artifact.

The fee components themselves were checked and are correct for CNC delivery:
`CNC_DELIVERY_COSTS` (:53) zeroes brokerage, sets exchange charges to the
post-Oct-2024 0.00297%, and adds the ₹13.5 + GST DP charge per sell. STT at 0.1%
both sides is right for delivery. This part is sound.

### MODERATE-12 — The absorbing drawdown halt exists in live too

`backtest_engine.py:289` documents the halt as absorbing: it blocks entries →
equity cannot grow → drawdown-from-peak never shrinks → it never lifts.
`CLAUDE.md` note 10 states the same trap exists in live. Given Phase 6 explicitly
contemplates higher risk settings, a halt that is permanent once tripped is a
latent single point of failure, not a safety net.

### MODERATE-13 — The metrics the objective needs are not computed

`BacktestMetrics` (`performance_analyzer.py:22`) provides total return, CAGR, PF,
win rate, Sharpe, Sortino, max DD + duration, Calmar, `avg_holding_days`, and
`exposure_time_pct`.

Absent, and all required by the stated objective: **median** holding period,
capital utilization, capital turnover, return per capital-day, trades/year,
expectancy, recovery time, worst month/year, Monte Carlo survival, walk-forward,
cost/slippage/parameter sensitivity, correlated-loss and sector-concentration
analysis.

Phases 5, 6, 9, 10 and 11 cannot currently be executed. This is missing
instrumentation rather than a defect.

### MINOR-14 — Documentation drift

- `CLAUDE.md` / `DIVERGENCE_INVENTORY.md` list A1 (RS gate) as OPEN — closed
  2026-08-10 (`opportunity_ranker.py:15-20`).
- Same for B1 (partial profits) — closed by `ENABLE_PARTIAL_PROFIT = False`
  (`config.py:80`); `check_partial_profit` is now gated at `main.py:999`.
- `breakout_detector.py` docstring lists 5 breakout conditions; the code enforces
  **6** (`bullish_body`, `c > o`, at :91).
- `point_in_time_universe.py` docstring and class docstring say "Nifty 200"; the
  configured universe is `nifty500`.
- `position_sizer.py` docstring says 1.5% / 15%; config is 2.0% / 20%.

### MINOR-15 — Smaller engine issues

- `_close_position` fills `profit_target` at exactly `target_price` after testing
  `close >= target` (`_process_exits:339`) — mildly optimistic. Affects only the
  benched fixed-target strategies.
- `_calc_available_cash:889` values open positions at **cost basis**, while
  `_calc_portfolio_value:873` marks to market. Live uses market value throughout.
  Small sizing drift as positions move.
- `_preload_data:941` caches OHLCV only for the universe as of `start_date`.
  Since the universe never actually changes (BLOCKER-1) this is currently inert,
  but it will silently starve the scan once a real PIT universe is introduced.

---

## 3. Assessment against the stated objective

The objective is fast compounding, high capital turnover and high utilization,
with aggressive but survivable risk.

Three structural observations, all independent of BLOCKER-1:

1. **Utilization is capped at ~80% by construction, and realistically far lower.**
   MAJOR-10 caps deployed capital at 80%. On top of that, the vcp_v2 funnel
   produced roughly 43 trades in 18 months across ~500 stocks. Most of the time
   most of the capital is in cash. That is close to the opposite of the stated
   goal, and it is a property of the entry funnel (2× volume **and** strong close
   **and** bullish body **and** not extended **and** ≥30% prior uptrend **and** all
   8 trend-template conditions), not of a tunable parameter.

2. **The exit geometry is already fast, and may be the wrong kind of fast.** The
   trailing stop floor is `EMA10 × 0.97` (`stop_loss_manager.py:105`), roughly 3%
   below the 10-day EMA. Once a position moves up, this becomes a very tight
   trail. Combined with `TIME_STOP_DAYS = 15`, holding periods are short — good
   for turnover, but the closed breakout study
   (`breakout_validation_plan.md`) found stop-loss exits were 82% of trades in
   both windows for the analogous variant. Short holds driven by getting stopped
   out are not the same thing as capital-time efficiency.

3. **Concentration is higher than the documentation implies.** Four positions at
   20% each, with a 30% sector cap that passes-by-default whenever Screener.in is
   unreachable (`risk/risk_manager.py`, and Screener reachability from the VPS is
   a known-intermittent TODO). The realistic worst case is four correlated
   midcaps gapping together. Phase 7 has real work to do here.

None of this means VCP is the wrong answer. It means the current configuration is
tuned for capital preservation while the stated objective is compounding speed,
and that the evidence base for preferring VCP over the rejected alternatives is
void until BLOCKER-1 is fixed.

---

## 4. What I did not do

- Did not modify any strategy, config, or execution code.
- Did not run a backtest. The local `data/market_data.db` holds 49 symbols
  spanning 2024-06-25 → 2026-06-25; `fundamentals.db` has no `universe_snapshots`
  table at all. **The data needed to run or re-validate anything lives only on the
  VPS.** No numbers in this repo can currently be reproduced locally.
- Did not verify the claimed magnitude of BLOCKER-1 (the "499/500 match today"
  figure comes from a prior session's note, not from this audit — the local DB
  cannot confirm it). The structural argument above does not depend on that
  figure.

---

## 5. Recommended order of work

1. **Establish a real point-in-time universe** (BLOCKER-1). Everything downstream
   is unmeasurable until this exists. This is a data-sourcing problem, not a
   coding problem, and it is the gate on the entire plan.
2. **Audit and repair the price cache** (BLOCKER-2): detect split/bonus
   discontinuities, establish one adjustment basis, add a regression test.
3. **Re-measure vcp_v2 on the corrected data** — treat every existing number as
   deleted. Expect the edge to shrink; be prepared for it to vanish.
4. **Unify the decision engine** (Phase 3) — one canonical module consumed by both
   executors, honouring BLOCKER-3 (no non-PIT input may enter it).
5. **Fix paper-mode fidelity** (MAJOR-9) before paper trading is used as evidence.
6. **Build the missing metrics** (MODERATE-13), then and only then run Phases 4–11.

Steps 1–3 will most likely invalidate the current strategy selection. That is the
point of doing them first.
