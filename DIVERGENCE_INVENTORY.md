# Live vs Backtest Divergence Inventory (2026-08-07)

Systematic comparison of `main.py` (live) against `backtest/backtest_engine.py`
(the engine that produced the vcp_v2 PF 1.61 validation). Prompted by the Jul 29
incident (ROE/liquidity gates silently shrinking the pond) and the Aug 7 fidelity
audit — the question is not "is this rule good" but "does the same rule run in
both engines".

Severity: **A** = changes which trades happen, **B** = changes P&L on the same
trade, **C** = cosmetic/structural.

---

## A. Live-only gates that block trades the validated engine takes

| # | Gate | Live | Backtest | Sev |
|---|---|---|---|---|
| A1 | **RS ≥ 70 percentile** (`rank_setups` → `passes_rs_filter`) | **Hard drop** of any breakout candidate below the 70th RS percentile | **None** — vcp_v2 path has no RS check at all | **A** |
| A2 | **Corporate events** (`filter_corporate_events`) | results ±7d, div/bonus/split 0–3d ahead | Backported 2026-08-07 (`--events-filter`), **off by default** | **A** |
| A3 | **Price ≥ ₹100** (`filter_price`) | yes | none | A |
| A4 | **Market cap ≥ ₹5,000 Cr** | yes (pass-by-default if Screener.in data missing) | none | A |
| A5 | **Liquidity ≥ ₹10 Cr/day** | yes | none | A |
| A6 | **ASM/ESM/T2T surveillance** | yes | none | A |
| A7 | **Circuit-breaker hits ≤ 3/60d** | yes | none | A |
| A8 | **D/E ≤ 2, pledge ≤ 50%** | yes (pass-by-default) | none | A |
| A9 | **Listing age ≥ 252d** | yes | partial — needs 210 rows of OHLCV | A |
| A10 | **Data quality (≤5 missing/90d)** | yes | none | A |
| A11 | **Manual blocklist** | yes | none | A |
| A12 | **Sector cap 30%** (`RiskManager`) | yes | none | A |
| A13 | **Daily/weekly/monthly loss limits + consecutive-loss pauses** | yes (`DrawdownMonitor`) | only the 20% max-DD halt | A |
| A14 | **Entry extension** — open >5% past pivot rejected (`evaluate_gap`) | yes | **not checked at entry** (only inside `is_valid_breakout` on the prior close) | A |
| A15 | **Ranking under slot pressure** | scores and takes the best N | fills slots in **universe iteration order** (alphabetical bias) | A |
| A16 | **VIX regimes** (HIGH/EXTREME_VOLATILITY) | can block entries | VIX-less — those states can never trigger | A |

**A1 is the most under-documented.** `CLAUDE.md` says "no RS gate at watch-add
(validated config has none)" — true, but the gate simply moved downstream: every
confirmed breakout still passes through `rank_setups`, which silently drops
RS < 70. The fallback when a symbol is missing from `rs_scores` is 50.0, i.e.
**dropped**. A valid vcp_v2 breakout at RS 65 is taken by the backtest and
refused by live.

## B. Same trade, different P&L

| # | Behaviour | Live | Backtest | Sev |
|---|---|---|---|---|
| B1 | **Partial profit booking** | Sells 33% at +20%, 33% at +30%, 34% rides the trail (`check_partial_profit`) | **Never called** — `check_partial_profit` is imported but dead code; positions ride 100% to the stop | **B** |
| B2 | **Regime-triggered exit** | BEAR → exit within 3 days; VIX>35 → 2 days (`check_regime_exit`) | none | B |
| B3 | **Dust floor** | none until 2026-08-07 | ₹30k until 2026-08-07 | B (fixed — now a shared ₹6,000) |
| B4 | **Slippage** | real fills (now recorded via `get_fill_price`) | modeled 0.1%/side | B |
| B5 | **Brokerage** | real CNC = ₹0 | modeled ₹20/side + GST | B |
| B6 | **DP charge** | real ₹15.93/sell | not modeled | B |
| B7 | **Stop fill on gap-down** | GTT triggers at market | fills at `min(stop, open)` | B |
| B8 | **Exit evaluation cadence** | daily close (+ GTT/backup monitor intraday) | daily bar | C — intentional, documented |

**B1 is the biggest P&L-shape divergence.** The validated PF 1.61 comes from an
engine that rides winners to the trailing stop. Live amputates two-thirds of
every winner at +20/+30%. For a trend strategy whose expectancy lives in the
right tail, that is exactly the "fixed targets amputate winners" failure the
2026-07-14 campaign blamed for momentum/breakout/MR's rejection — and it is
running, unvalidated, in live right now.

## C. Structural

- C1: Live scans an evening snapshot and enters next morning at the open;
  backtest scans `prev_day_df` and enters at `today_row["open"]`. Equivalent.
- C2: Backtest universe is point-in-time Nifty 500 (current membership only —
  survivorship caveat); live uses today's list.
- C3: Backtest pivot watch is in-memory per run; live persists to
  `.pivot_watchlist.json`, so live carries pivot vintage across deploys
  (a known cause of entry-price differences vs backtest).

---

## Recommended order of attack

1. **A1 (RS gate)** — decide by backtest, exactly like the events filter. Run
   vcp_v2 with an RS ≥ 70 gate over both windows; keep in both engines or
   delete from live. This is the highest-frequency silent divergence.
2. **B1 (partial profits)** — either implement partial booking in the backtest
   and re-validate, or disable it in live. Today live is running exit geometry
   that has never been tested.
3. **A2 (events filter)** — verdict pending the r5/r6 runs.
4. **A3–A11 (pond filters)** — lower priority: they mostly remove stocks that
   would fail the trend template anyway, but they should be applied in the
   backtest before the next full re-validation so the pond matches.
5. **B4–B6 (cost model)** — correct to real CNC charges and re-baseline once.

Everything in section A/B that is not fixed is, by definition, a rule the live
bot applies that no validation covers.
