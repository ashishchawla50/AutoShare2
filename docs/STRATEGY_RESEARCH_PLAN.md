# STRATEGY_RESEARCH_PLAN.md — Phase 1: Research Framework Assessment

**Date:** 2026-08-12
**Scope:** Inspect the current backtest architecture and specify exactly what must be
built or changed before any strategy family or holding horizon can be compared fairly.
**Status:** Assessment only. **No strategy implemented, no parameter tuned.**
**Data foundation:** frozen at DATA FOUNDATION: GO (2026-08-11).

---

## 0. Headline findings

Four blockers, in order of severity. Two of them mean that **any cross-strategy
number the current engine produces would be uninterpretable**, independent of how
good the data now is.

| # | Finding | Consequence |
|---|---|---|
| **B1** | Each strategy uses a **different position-sizing regime** | Cross-family comparison is confounded at the root |
| **B2** | A 5-year run takes **~5.3 hours**; the research matrix needs hundreds of runs | Research is computationally infeasible as built |
| **B3** | **Intraday cannot be simulated** — there are no intraday bars, and the only accessible source excludes delisted securities | Horizon A is not answerable with current data |
| **B4** | Capital-time metrics (the primary objective) **are not computed at all** | The winner cannot be selected on the stated criterion |

---

## 1. B1 — Strategies are not comparable, because they are not sized alike

`backtest_engine._scan_for_entries` applies a **different sizing rule per strategy**:

| Strategy | Sizing | Effective position |
|---|---|---|
| breakout / breakout_v2 | `BREAKOUT_POSITION_SIZE_PCT` | 10% of portfolio |
| mean_reversion | `MR_POSITION_SIZE_PCT` | 10% |
| momentum v1–v4 | `MOMENTUM_POSITION_SIZE_PCT` | 8% |
| vcp / vcp_v2 | `calculate_position_size` (risk-based) | binds at the **20%** cap |

A VCP trade therefore carries **2.5× the capital of a momentum trade** on identical
conviction. Comparing family returns under that arrangement measures leverage, not
edge. The same applies to exits: `_process_exits` branches on strategy name across
eight `if/elif` arms, so each family gets its own stop, target and time-stop geometry
baked in.

This also means the historical family "rejections" were never like-for-like — but
those numbers are void anyway and are not being used as a reference.

**Required:** one sizing/exit framework applied identically to every candidate, with
entry logic the only thing that varies. Exit geometry becomes an explicit, separately
sweepable dimension (Phase 8), not a property bundled into each strategy's name.

---

## 2. B2 — The engine is too slow for the research matrix

Measured: the Phase-3 smoke run (2023-01-02 → 2023-03-31, 60 trading days,
`vcp_v2`, reconstructed universe) took **~15 minutes** — roughly **15 s per trading
day**. Extrapolated to the full 1,283-day window: **~5.3 hours per single run**, on a
2-core / 956 MB VPS.

The reason is algorithmic, not hardware. `analyse_stock` is called once per symbol per
day and **recomputes every indicator over the whole history each time** — EMAs, ATR,
swing detection, VCP contractions. That is O(n²) in the length of the series, repeated
across ~1,500 ISINs.

The research programme requires, at minimum:

- ~6 strategy families × several variants
- × 5 holding horizons
- × 7 risk levels
- × 4 cost-stress scenarios
- × walk-forward folds
- × capital scales (₹50k → ₹10L)

That is **hundreds to low thousands of runs**. At 5.3 h each this is not weeks of
compute, it is years. **The framework must be restructured before any strategy work
begins**, or Phase 3 onward simply cannot be executed.

**Required:** precompute an indicator panel once per ISIN (vectorised, pandas/numpy),
then evaluate strategy rules against the panel. This converts the inner loop from
O(n²) recomputation to O(1) lookups and should yield a 50–100× speedup. Parallelism
across the 2 cores is a secondary, much smaller win and should not be relied on.

---

## 3. B3 — Intraday is not simulatable with the data we have

**Stating this plainly, as instructed: the current data does not support realistic
intraday simulation, and I will not manufacture intraday results from daily bars.**

The reconstructed dataset is **daily OHLC only** (NSE bhavcopy). Daily bars cannot
support intraday work because:

- The **intra-bar path is unknown.** With only O/H/L/C there is no way to tell whether
  the high preceded the low. Any same-day entry-and-exit rule can be made to look
  profitable purely by assuming a favourable ordering. This is the single most common
  way intraday backtests are fabricated.
- **Opening-range breakout, VWAP, and time-based exits are undefined** without
  intra-session bars.
- Spread and depth — the dominant costs at intraday frequency — are unobservable.

### What intraday would actually require

| Requirement | Detail |
|---|---|
| Granularity | 1-minute or 5-minute OHLCV bars |
| Coverage | The Historical Liquid-500, 2021-06 → 2026-08 |
| **Critical** | Must include the **672 delisted / renamed securities**, keyed by ISIN |
| Volume | ~500 symbols × 1,283 days × 375 min ≈ **240M bars**, ~10–20 GB |
| Spread proxy | Bid-ask or at minimum per-minute high-low and trade count |

**The survivorship trap is the deciding constraint.** Kite Connect can serve minute
history, but only for instruments in the *current* instrument master — delisted and
merged securities are unavailable. An intraday study built from Kite would therefore
run on survivors only, **reintroducing exactly the 36% survivorship hole we just spent
this entire effort removing**, and its results would be as void as the old ones.

A survivorship-free intraday archive needs a commercial vendor (Global Datafeeds,
TrueData, or NSE's own historical data products), which is a purchase decision, not an
engineering one. Separately, 240M bars is not workable on a 956 MB / 2-core VPS; that
tier of research needs a larger machine.

**Recommendation:** proceed now with horizons **B–F**, which daily data supports
honestly, and treat **A (intraday) as blocked pending a data purchase**. I will not
report an intraday result either way until the data exists. If you want intraday
answered, the decision to make is whether to fund the vendor archive and a bigger box
— I can scope both precisely before you commit.

**Note on horizon B.** Very-short swing (1–3 days) *is* honestly testable on daily
bars: entry at next open, exit at a later close or on a stop, with the existing
gap-through-stop handling. It is not intraday and must not be described as such.

---

## 4. B4 — The primary objective is not measured

`BacktestMetrics` computes total return, CAGR, PF, win rate, Sharpe, Sortino, max DD
and duration, Calmar, `avg_holding_days`, `exposure_time_pct`.

Every metric the stated objective actually turns on is **absent**:

capital utilization · capital turnover · **return per capital-day** · return per ₹
deployed · median holding period · idle capital · recovery time · trades/year ·
expectancy · worst month/year · regime attribution · Monte Carlo ruin probability ·
walk-forward consistency · parameter sensitivity.

Without `return per capital-day` in particular, the central question — *does a
repeatable +5% in 2 days beat +20% in 60 days?* — cannot be answered at all.

---

## 5. Secondary gaps

| # | Gap | Why it matters |
|---|---|---|
| S1 | **No ranking layer.** `_scan_for_entries` iterates the universe and takes the first signals that fit. | On high-opportunity days the engine picks alphabetically, not by conviction. Phase 13 needs a real candidate → score → rank → allocate pipeline. |
| S2 | **Slippage is a flat 0.1%/side constant** with no spread, size-vs-ADV impact, or failed fills. | Phase 11 cost stress and Phase 14 scale testing (₹2L→₹10L) cannot give meaningful answers while the one variable that should degrade with size is held fixed. |
| S3 | **Portfolio structure is hard-coded** from config (MAX_POSITIONS, cash reserve, position cap, sector cap). | Phase 6 requires these as swept parameters. |
| S4 | **The 4-position structural ceiling.** 20% position cap + 20% cash reserve ⇒ cash exhausts after four positions; `MAX_POSITIONS = 8` never binds. | Capital utilization is capped at ~80% by construction — directly against the objective, and currently invisible. |
| S5 | **Regime model has 3 states, no VIX.** | Phase 10 needs bull / bear / sideways / high-vol / low-vol. Sideways and the volatility states do not exist today. |
| S6 | **No walk-forward harness, no Monte Carlo, no cost-stress dial.** | Phases 11, 12 and the ruin analysis in Phase 7 have no machinery at all. |
| S7 | **No sector data that is point-in-time.** Sector comes from Screener.in (current-only). | Phase 6 sector-concentration and Phase 7 correlated-loss work needs either PIT sector data or a price-correlation-based proxy. The proxy is derivable from the reconstructed data and carries no look-ahead. |
| S8 | **Trade ledger lacks capital-time fields** (capital deployed × days held). | Required input for B4's metrics. |

---

## 6. What gets built, in order

Nothing here changes a strategy rule or a parameter.

### Step 1 — Indicator panel precomputation (unblocks B2) — **ATTEMPTED, NOT PASSED**
> Built and validated (see `docs/INDICATOR_PANEL.md`): 3,184 ISINs / 2.47M rows / 50
> columns; 16/16 validation tests; trade-level regression byte-identical (final capital
> ₹49,255.590597 both paths). **But measured speedup is 2.00×, not 50×**, and 3 of
> 25,522 symbol-days reclassify between rejection buckets for reasons not yet
> established. B2 therefore remains OPEN: profiling shows the dominant cost is the
> engine's per-symbol-per-day pandas slicing, not indicator maths, so the 50× needs
> the Step 2 engine restructure. The panel is a prerequisite for it, not a substitute.

Vectorised per-ISIN panel: EMAs, ATR, RSI, rolling highs/lows, turnover, volatility,
returns over standard lookbacks, swing points. Computed once, cached to disk, keyed by
ISIN. Strategy evaluation becomes lookups. Target: ≥50× speedup, verified by
reproducing the smoke run's trade list exactly.

### Step 2 — Canonical research engine (unblocks B1)
One pipeline, matching the requested shape:

```
Historical Data → PIT Universe → Candidate Generation → Strategy
  → Ranking → Risk/Sizing → Execution Sim → Portfolio → Performance
```

A strategy declares only: **entry rule**, **candidate score**, and **preferred
horizon**. Sizing, exits, ranking, portfolio limits and costs are supplied identically
by the framework to every strategy, and are independently sweepable.

### Step 3 — Metrics layer (unblocks B4)
Extend `BacktestMetrics` with the full capital-time set, plus per-trade capital-day
accounting and regime tagging on every trade.

### Step 4 — Execution realism (S2)
Cost multiplier dial (1×/1.5×/2×/3×) and a size-aware slippage model expressed as a
function of order value vs. trailing median turnover, with an explicit participation
cap.

### Step 5 — Validation harness (S6)
Walk-forward folds, Monte Carlo (trade resampling and adverse-sequence), parameter
sensitivity, regime attribution.

### Step 6 — Framework validation before any research
The framework must be shown correct before it is trusted to rank anything:
- a zero-edge random-entry strategy must produce ≈ costs-only loss, not a spurious edge;
- a deliberately look-ahead strategy (buy at today's close, sell at today's high) must
  produce an absurd result, proving the harness *can* detect leakage;
- identical trades under identical sizing must reconcile to the ledger invariant;
- the fast panel path must reproduce the slow path's trades exactly.

Only after Step 6 does Phase 2 (holding-horizon research) begin.

---

## 7. Scope check

Horizons **B–F** are answerable now. Horizon **A (intraday)** is blocked on data, not
on engineering, and I recommend deciding that separately rather than letting it hold
up the rest.

Strategy families (momentum, breakout, VCP/contraction, pullback, mean reversion,
multi-factor, hybrid) are all implementable against daily bars once Step 2 exists.
None will be pre-judged, and VCP receives no priority — it enters the comparison as
one candidate among many, on the same footing.
