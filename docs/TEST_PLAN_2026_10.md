# Test plan, October 2026 — pre-registered

Written and committed 2026-10-11 **before any of these tests was run**. Results go in
`docs/TEST_RESULTS_2026_10.md`. Nothing in this file may be changed after results are seen,
except Part 2 (B2), which is written after the B1 exploration and committed before B2 runs.

## Common setup

- **Engine:** `backtest/fast` (vcp_v2 path proven against the reference oracle). Full portfolio
  rules: slots, cash, sizing, regime gate (BEAR blocks, CAUTIOUS halves), real CNC costs, and
  `--no-dd-halt` so every window is measured in full.
- **Baseline B0:** vcp_v2 entries, current exits, first stop −7% (live), ₹50,000, 2% risk,
  20% max position.
- **Windows:** DEV 2021-09-01 → 2024-12-31 · OOS 2025-01-01 → 2026-06-30.
- **Adoption rule R:** a candidate beats the baseline only if **all three** hold:
  1. PF higher in **both** DEV and OOS
  2. total return higher in **both** DEV and OOS
  3. DEV max drawdown above −20%
- Each candidate is run **once**. No re-runs with tweaked settings after seeing results; a new
  idea becomes a new pre-registered test.
- OOS has only ~40 trades, so even a pass is weak evidence. Live trading data is the final judge.

## Group A — open questions

| ID | Candidate vs baseline | If it passes R | If it fails R |
|---|---|---|---|
| **A1** | First stop −10% vs −7% | Groups B and C use −10% | Groups B and C use −7% |
| **A2** | RS ≥ 70 percentile entry filter (live's removed rule, computed as in `scanner/relative_strength.py`; percentile among that day's universe members with ≥252 rows; a breakout that fails it consumes its pivot) | Add to backtest and live | Stays removed |
| **A3** | Partial profits as live implements them (33% at +20% and 33% at +30% on the day's close, one tier per day, after the breakdown and time-stop checks) | Enable `ENABLE_PARTIAL_PROFIT` and add to the backtest | Stays off |

A2 and A3 are compared against **B0 with A1's chosen stop**.

## Group C — robustness of the current edge (diagnostics, no adoption)

Run on **B0 with A1's chosen stop**.

| ID | Test | "Healthy" if |
|---|---|---|
| **C1** | Year-by-year results (trades grouped by entry year, full window 2021-09 → 2026-06) | profitable (PF > 1) in at least 3 of the 5 full or partial years |
| **C2** | Sensitivity: each setting ×0.8 and ×1.2, one at a time: breakout volume multiple, strong-close threshold, max entry extension, pivot watch days, time-stop days, time-stop min profit, first stop, VCP lookback | every one of the 16 nudged runs has full-window PF ≥ 1.0, and the median nudged PF is within 0.2 of the baseline |
| **C3** | Slippage: 0.1%, 0.25%, 0.5% per side (each buy fills that much higher, each sell that much lower) | full-window PF ≥ 1.0 at 0.25% per side |
| **C4** | Monte Carlo: full-window trade P&L reshuffled 10,000 times | probability of a 20% drawdown below 10% |

## Group B — better entries

- **B1, exploration (DEV only, no OOS).** For every breakout signal, record features known on
  entry day:
  - relative-strength percentile
  - breakout volume ÷ 50-day average
  - close vs pivot
  - share price
  - Nifty vs its 50-day EMA
  - market breadth
  - regime
  - gap at the open
  - ATR % (volatility)
  - distance from the 52-week high
  - close vs 50-day EMA
  - days the pivot had been watched
  - stock 63-day return vs Nifty

  Compare outcomes by quintile under the A1-chosen exits.
- **B2, confirmation.** At most **two** single filters, plus their combination, chosen from B1 on
  DEV evidence only. They are written into Part 2 below and committed **before** their OOS run.
  Judged by rule R.
- **B3, more signals.** Judged by rule R:

| ID | Candidate |
|---|---|
| B3a | pivot watch 45 days (vs 30) |
| B3b | pivot watch 20 days |
| B3c | breakout volume 1.5× (vs 2.0×) |
| B3d | CAUTIOUS regime at full size (vs half) |

## Group D — live vs backtest

- **D1:** a tool that replays the backtest over the live trading period and matches every live
  trade (same symbol, same entry day), reporting missed and extra trades and P&L differences.
  It needs `portfolio_state.db` from the VPS; the local copy has 0 trades.

---

## Part 2 — B2 filters (written after B1, before the B2 OOS run)

*(empty until B1 is done)*
