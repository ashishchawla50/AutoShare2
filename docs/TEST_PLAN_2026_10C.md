# Test plan 2026-10 C — the speed-of-profit rule (pre-registered)

Written and committed 2026-10-11 **before running**. Runner:
`python tools/run_test_plan_2026_10.py velocity`. Results go in
`docs/TEST_RESULTS_2026_10.md` (section "Batch C").

## The thumb rule (the user's, and the project's ranking rule from now on)

> **Less percentage profit in fewer days is much better than more percentage profit in more days.**

So strategies are ranked by **how fast they make money while the money is in a trade**, not by
profit per trade.

## The measure: velocity

    velocity = total net P&L (₹)  ÷  total rupee-days in positions

- One rupee-day is ₹1 held in a position for one calendar day, at the buy value; the minimum is
  1 day per trade.
- It is shown as **% per 30 days invested**.
- Example: ₹10,000 that makes ₹200 in 5 days scores 1.2% per 30 days.

**Also reported every time:** account CAGR, PF, max drawdown, days held, and days with no
position. Velocity measures money *while it is in a trade*. If good signals are rare, a faster
exit can raise velocity while the account grows less because the money waits idle. This
trade-off is shown, never hidden.

## Method

Same rolling starts as Batch B:
- **DEV:** 11 runs starting 2021-09-01 … 2024-03-01, each ending 2024-12-31
- **OOS:** 4 runs starting 2025-01-01 … 2025-10-01, each ending 2026-06-30

**Medians** across runs are judged. Setup: ₹50,000, 2% risk / 20% position, CNC costs, regime
gate on, drawdown halt off.

**Baseline:** the current default — vcp_v2, breakout volume 1.5×, first stop −7%, trailing
exits, time stop at 15 trading days if under +7%.

## Candidates

| ID | Change |
|---|---|
| T1 | Time stop at 10 trading days if under +7% |
| T2 | From trading day 5, exit any day the profit is under +2% |
| T3 | Maximum hold of 10 trading days (exit on day 10 whatever the profit) |
| T4 | Maximum hold of 5 trading days |
| P1 | Take profit at +3% (sell everything when the high reaches +3%) |
| P2 | Take profit at +5% |
| P3 | Take profit at +10% |
| C1 | Take profit at +5% **and** maximum hold of 10 trading days |
| R-mr | Mean reversion strategy (4-day holds) |
| R-mom | Momentum strategy |
| R-bo | 20-day breakout strategy |

All other exits (stop, trailing, breakdown) stay as in the baseline for T and P candidates.

## Rule R3

1. **Guards.** A candidate must make money:
   - median PF above 1.0 in DEV and in OOS
   - median CAGR above 0% in DEV and in OOS
   - median DEV max drawdown above −20%
2. **Speed.** Its median velocity must beat the baseline's in **both** DEV and OOS.
3. **Choice.** Among candidates passing 1 and 2, adopt the one with the **highest DEV velocity**.
   If none passes, the baseline stays.

The winner becomes the default strategy: time-stop candidates through `config/config.py`
(which live already reads); take-profit candidates need a new config setting plus live exit code.
Run once; no settings changed after results are seen.
