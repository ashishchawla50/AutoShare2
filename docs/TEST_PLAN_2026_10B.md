# Test plan 2026-10 B — faster trades (pre-registered)

Written and committed 2026-10-11 **before running**. Results go in `docs/TEST_RESULTS_2026_10.md`
(section "Batch B"). Runner: `python tools/run_test_plan_2026_10.py fast`.

## Why

**The user's thumb rule:** "2% profit in 5 days is much better than 10% profit in 2 years." So
strategies are ranked by **account-level annual return (CAGR)** and **days held**, not by profit
per trade.

The catch, to be tested rather than assumed: a fast exit only pays if the freed money finds a
new trade. In earlier tests vcp_v2 had no position open on 40–58% of days.

## Method — rolling start dates

A single fixed start date proved unreliable: the same 2025 trades scored PF 1.78 when tested
alone but 1.02 inside a run from 2021. So every candidate runs from many start dates, and the
**median** is judged.

- **DEV:** starts on the 1st of each quarter, 2021-09-01 → 2024-03-01 (11 runs), each ending
  2024-12-31
- **OOS:** starts 2025-01-01, 2025-04-01, 2025-07-01, 2025-10-01 (4 runs), each ending 2026-06-30
- **Common setup:** ₹50,000, 2% risk / 20% position, real CNC costs, regime gate on, drawdown
  halt off
- **Baseline:** current default — vcp_v2, breakout volume 1.5×, first stop −7%, current exits

## Candidates (all vcp_v2 entries, exits changed)

| ID | Exit change |
|---|---|
| F1 | Time stop after **10** trading days (instead of 15) if profit is under +7% |
| F2 | **Thumb rule:** from day 5 on, exit any day the profit is under +2% |
| F3 | Take profit: sell everything when the price reaches **+10%** |
| F4 | Take profit: sell everything when the price reaches **+5%** |
| F5 | F2 and F3 together |

## Rule R2 — a candidate replaces the baseline only if all of these hold

1. Median CAGR is **higher** than the baseline's in DEV **and** in OOS
2. Median PF is above 1.0 in DEV and in OOS
3. Median DEV max drawdown is above −20%

**Thumb-rule tie-break:** if the median CAGR is within ±0.5 percentage points of the baseline in
both windows, the candidate still wins when its average days held is at least 20% shorter
(and conditions 2 and 3 hold).

Each candidate is run once. No changed settings after results are seen.

## Context only (no adoption)

The same rolling-start CAGR and days held are also reported for the benched rule strategies
(mean reversion, momentum, breakout), so the thumb rule can be applied to them too.
