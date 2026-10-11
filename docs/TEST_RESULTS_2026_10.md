# Test results, October 2026

Results of the pre-registered plan in [TEST_PLAN_2026_10.md](TEST_PLAN_2026_10.md), run
2026-10-11. The plan was committed in `34446d4` and its Part 2 (B2) in `89513da`, each **before**
the tests it covers were run. Reproduce with `python tools/run_test_plan_2026_10.py <group>`;
raw results are in `data/test_plan_2026_10/*.json`.

Setup throughout: vcp_v2, ₹50,000, 2% risk / 20% position, real CNC costs, regime gate on,
drawdown halt off. DEV = 2021-09 → 2024-12, OOS = 2025-01 → 2026-06.
**Rule R:** PF higher in both windows **and** return higher in both **and** DEV drawdown above −20%.

## Summary

| Test | Result | Action |
|---|---|---|
| A1 first stop −10% | **fail** (OOS PF 1.75 vs 1.78) | keep −7% |
| A2 RS ≥ 70 filter | **fail** (OOS: 7 trades, PF 0.69) | stays removed |
| A3 live partial profits | **fail** (DEV return +7.7% vs +22.1%) | keep `ENABLE_PARTIAL_PROFIT = False` |
| B2a RS ≥ 41 | **fail** (DEV better, OOS identical) | not adopted |
| B2b close ≥ pivot +0.8% | **fail** (OOS PF 1.27 vs 1.78) | not adopted |
| B2c both | **fail** (DEV PF 1.72, OOS 1.27) | not adopted |
| B3a pivot watch 45 days | fail | — |
| B3b pivot watch 20 days | fail | — |
| **B3c breakout volume 1.5×** | **PASS** | candidate (see "What to do") |
| B3d CAUTIOUS at full size | fail (OOS PF 1.60 vs 1.78) | — |
| C1 year by year | **not healthy**: 2 of 5 years profitable | — |
| C2 setting sensitivity | **not healthy**: 4 of 16 nudges give PF < 1 | — |
| C3 slippage | healthy, barely: PF 1.05 at 0.25% per side | — |
| C4 Monte Carlo drawdown | **not healthy**: 28% chance of a 20% drawdown | — |
| D1 live vs backtest | tool built; no live trades in the local DB | run with the VPS DB |

## Main finding: the current edge is fragile

Group C says the vcp_v2 edge, as it stands, is weak and unstable:

- **One good year carries everything.** Over the full window (2021-09 → 2026-06, PF 1.21,
  +19.2%), trades entered in 2023 made +₹23,957. 2022 (−₹4,122) and 2024 (−₹9,576) lost, and
  2025 broke even (+₹150).
- **Small setting changes flip it to a loss.** A 20% nudge drops PF below 1.0 for: entry
  extension 6% (0.91), VCP lookback 48 (0.96), strong close 0.968 (0.95). The median nudged PF
  is 1.13 against 1.21.
- **Results depend on where the run starts.** OOS run alone: PF 1.78, +8.4%. The same 2025
  trades inside the full continuous run: PF 1.02, +₹150. Which positions happen to be open when
  a period begins changes which signals get taken. The 1.78 is partly luck of the starting state.
- **Drawdown risk is high at ₹50k:** median worst drawdown −17.1%, 1-in-20 worst −26.9%, and a
  28% chance of reaching the 20% halt.
- **Slippage:** PF 1.10 at 0.1% per side, 1.05 at 0.25%, 0.94 at 0.5%. Little margin.

## Group A

| | DEV trades | DEV PF | DEV return | DEV max DD | OOS trades | OOS PF | OOS return | OOS max DD |
|---|---|---|---|---|---|---|---|---|
| B0 baseline (−7%) | 130 | 1.29 | +22.1% | −17.1% | 39 | **1.78** | **+8.4%** | −5.8% |
| A1 first stop −10% | 126 | **1.40** | **+28.2%** | **−13.8%** | 39 | 1.75 | +8.2% | −5.9% |
| A2 RS ≥ 70 | 66 | 1.54 | +20.1% | −8.6% | 7 | 0.69 | −0.7% | −2.2% |
| A3 partials 33%/33% | 135 | 1.10 | +7.7% | −15.7% | 40 | 1.66 | +8.2% | −5.5% |

- **A1** missed by a hair in OOS (1.75 vs 1.78, a difference of one or two trades), after a clearly
  better DEV. Under the rule it fails. It stays the most promising single change.
- **A3: live partial profits hurt.** Selling at +20%/+30% cuts the few big winners that pay for
  everything. Keep them off. *Bug found while replicating:* live's `check_partial_profit`
  re-fires the +20% tier when 33% of the shares rounds down (e.g. 10 shares → sells 3, then 3
  again, because 3/10 < 0.33). It is harmless while the feature is off.

## Group B

**B1 exploration (DEV only, 240 signals).** Strongest patterns, bottom quintile against the rest:

- RS percentile below 41: PF 0.58
- close less than 0.8% above the pivot: PF 0.52
- a hot market (breadth above 0.78, or Nifty more than 3.4% above its 50-day EMA): PF 0.76–0.91
- CAUTIOUS-regime signals did *better* than BULL ones: PF 1.99 vs 1.46

The other features showed no consistent pattern. Full table: `data/test_plan_2026_10/b1.json`.

**B2 confirmation:**

| | DEV trades | DEV PF | DEV return | OOS trades | OOS PF | OOS return |
|---|---|---|---|---|---|---|
| base | 130 | 1.29 | +22.1% | 39 | 1.78 | +8.4% |
| B2a RS ≥ 41 | 116 | 1.44 | +30.2% | 39 | 1.78 | +8.4% |
| B2b pivot +0.8% | 112 | 1.49 | +35.7% | 33 | 1.27 | +2.9% |
| B2c both | 102 | **1.72** | **+45.8%** | 33 | 1.27 | +2.9% |

Big DEV gains that do not carry into OOS: this is the pattern-fitting the pre-registration
exists to catch. B2a removed no OOS trade at all, so OOS gives no evidence either way.

**B3 more signals:**

| | DEV trades | DEV PF | DEV return | OOS trades | OOS PF | OOS return | Rule R |
|---|---|---|---|---|---|---|---|
| base | 130 | 1.29 | +22.1% | 39 | 1.78 | +8.4% | |
| B3a watch 45d | 144 | 1.04 | +3.2% | 46 | 1.21 | +2.7% | fail |
| B3b watch 20d | 121 | 0.89 | −6.8% | 33 | 1.66 | +5.9% | fail |
| **B3c volume 1.5×** | **161** | **1.32** | **+25.4%** | **50** | **1.95** | **+11.8%** | **PASS** |
| B3d CAUTIOUS full | 111 | 1.37 | +31.0% | 32 | 1.60 | +8.7% | fail |

## What to do

1. **B3c (breakout volume 1.5× instead of 2.0×) is the only change that passed.** It gave more
   trades and better results in both windows, though the DEV margin is small (PF 1.32 vs 1.29).
   In C2, volume 1.6× and 2.4× moved PF by only ±0.02, so the effect is mild. **Recommendation:**
   adopt it in research. Change live only after the user decides, because changing live rules is
   a strategy decision. It also changes the frozen oracle fixtures, which use 2.0×.
2. **Do not raise capital.** The edge is fragile (Group C) and has a 28% chance of a 20% drawdown
   at ₹50k. The ₹1L promotion criteria (≥30 live trades matching the backtest) remain the gate.
3. **Keep live partial profits off** (A3), and keep the RS gate removed (A2).
4. **Run D1 with the VPS database** once live trades exist, to check that live trades like the
   backtest.
5. **Future pre-registered ideas:**
   - A1 again with fresh data (the −10% stop nearly passed)
   - a market-heat filter (from B1)
   - fixing the start-state sensitivity: compare strategies on rolling start dates rather than
     one fixed split

---

## Follow-up 2026-10-11: breakout volume 1.5× adopted

`MIN_BREAKOUT_VOLUME_MULTIPLIER` changed from 2.0 to 1.5 in `config/config.py` (commit `ef72fb6`),
the only candidate that passed rule R (B3c).

The oracle fixtures were **not** re-captured. The reference engine that produced them was never
committed: it had the union-preload fix, and this repo's `backtest_engine.py` still has the bug
that loads only the start month's universe. Re-capturing from it would freeze wrong numbers. Both
engines read the volume multiple in one identical comparison, so `tests/test_fast_engine.py` pins
2.0× and the parity proof stays exact. **Not deployed to the live VPS.**

## Batch B: faster trades (plan `docs/TEST_PLAN_2026_10B.md`, committed `ba685b7` before running)

**The user's thumb rule:** "2% in 5 days beats 10% in 2 years." Candidates are ranked by
account CAGR and days held. Each figure is the median over 11 DEV start dates (2021-09 → 2024-03,
each to 2024-12) and 4 OOS start dates (2025-01 → 2025-10, each to 2026-06). Baseline =
the new default (volume 1.5×, −7% stop, current exits). Days held are calendar days.

| Candidate | DEV CAGR | DEV PF | DEV DD | DEV days | DEV trades | OOS CAGR | OOS PF | OOS days | OOS trades | R2 |
|---|---|---|---|---|---|---|---|---|---|---|
| **baseline** | +7.0% | 1.32 | −11.8% | 18.4 | 124 | **+5.7%** | **1.55** | 18.8 | 44 | — |
| F1 time stop 10 days | +13.2% | 1.42 | −14.0% | 15.4 | 137 | +4.1% | 1.34 | 13.7 | 49 | fail |
| F2 from day 5, exit if under +2% | **+15.4%** | **1.58** | −9.9% | 14.1 | 149 | −3.6% | 0.72 | 9.7 | 55 | fail |
| F3 take profit +10% | −1.1% | 0.96 | −12.1% | 11.2 | 165 | +4.4% | 1.42 | 13.6 | 52 | fail |
| F4 take profit +5% | −0.4% | 0.99 | −10.9% | 8.3 | 194 | +1.8% | 1.16 | 9.6 | 56 | fail |
| F5 F2 + F3 | −1.0% | 0.98 | −11.7% | 7.2 | 200 | −3.9% | 0.72 | 7.9 | 60 | fail |

**Context, the benched rule strategies under the same rolling starts:**

| Strategy | DEV CAGR | DEV PF | DEV days | OOS CAGR | OOS PF | OOS days |
|---|---|---|---|---|---|---|
| mean_reversion | −20.8% | 0.61 | 4.2 | −21.6% | 0.57 | 4.2 |
| momentum | −1.7% | 0.95 | 12.7 | +1.8% | 1.08 | 12.5 |
| momentum_v2 | −0.3% | 0.98 | 15.6 | −11.7% | 0.44 | 15.0 |
| breakout | −5.3% | 0.85 | 7.8 | −14.4% | 0.50 | 8.3 |
| breakout_v2 | −6.8% | 0.74 | 15.1 | −2.8% | 0.85 | 16.7 |

**What it says about the thumb rule:**

- **Fixed take-profits (+5%, +10%) remove the edge.** CAGR is about 0% in DEV. Selling every
  winner early leaves nothing to pay for the −7% losers.
- **Faster time stops (F1, F2) looked very good in 2021–24** (F2: +15.4% CAGR against +7.0%) but
  **lost in 2025–26.** This is the same DEV-good, OOS-bad pattern as the B2 filters. F1 is the
  closest call and is worth re-testing once more data exists.
- **Every short-hold strategy in the codebase loses money.** Mean reversion holds 4 days but has
  −21% CAGR. No tested strategy has delivered "2% in 5 days" repeatably on NSE after costs.
- **The baseline already does what it can:** about 18 calendar days per trade and +5.7–7.0% CAGR
  (median). It stays.

---

## Batch C: speed-of-profit rule (plan `docs/TEST_PLAN_2026_10C.md`, committed `22a4ff1` before running)

**The user's thumb rule:** *less % profit in fewer days is much better than more % in more days.*
Speed = net P&L ÷ rupee-days in positions, shown per 30 days invested. Medians over 11 DEV and 4
OOS rolling start dates. Days held are calendar days; "idle" means no position open.

| Candidate | DEV speed /30d | DEV CAGR | DEV PF | DEV days | DEV idle | OOS speed /30d | OOS CAGR | OOS PF | OOS days | OOS idle | Makes money | Faster both |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **baseline (current)** | **+1.75%** | +7.0% | 1.32 | 18.4 | 16% | **+1.99%** | +5.7% | 1.55 | 18.8 | 38% | yes | — |
| T1 time stop 10d (<7%) | +2.73% | +13.2% | 1.42 | 15.4 | 18% | +1.73% | +4.1% | 1.34 | 13.7 | 46% | yes | no |
| T2 day 5+, exit if <2% | +3.23% | +15.4% | 1.58 | 14.1 | 21% | −2.31% | −3.6% | 0.72 | 9.7 | 49% | no | no |
| T3 max hold 10 days | −1.10% | −3.9% | 0.87 | 11.3 | 20% | +1.74% | +3.7% | 1.27 | 11.5 | 47% | no | no |
| T4 max hold 5 days | −0.00% | +0.0% | 1.00 | 6.6 | 28% | −0.99% | −1.1% | 0.93 | 6.8 | 54% | no | no |
| P1 take profit +3% | −2.32% | −6.6% | 0.81 | 5.7 | 31% | −1.29% | −1.5% | 0.85 | 7.7 | 57% | no | no |
| P2 take profit +5% | −0.12% | −0.4% | 0.99 | 8.3 | 25% | +0.85% | +1.8% | 1.16 | 9.6 | 54% | no | no |
| P3 take profit +10% | −0.33% | −1.1% | 0.96 | 11.2 | 19% | +1.97% | +4.4% | 1.42 | 13.6 | 44% | no | no |
| C1 TP +5% & max 10d | −1.07% | −3.6% | 0.90 | 7.3 | 26% | −1.05% | −1.2% | 0.89 | 8.4 | 59% | no | no |
| R-mr mean reversion | −4.66% | −20.8% | 0.61 | 4.2 | 16% | −6.04% | −21.6% | 0.57 | 4.2 | 28% | no | no |
| R-mom momentum | −0.33% | −1.7% | 0.95 | 12.7 | 3% | +0.52% | +1.8% | 1.08 | 12.5 | 17% | no | no |
| R-bo 20-day breakout | −1.30% | −5.3% | 0.85 | 7.8 | 12% | −5.09% | −14.4% | 0.50 | 8.4 | 27% | no | no |

**Winner under rule R3: the baseline (no change).**

- **Under the thumb rule itself, the current exits are fastest in 2025–26** (+1.99% per 30 days
  invested). Every take-profit and maximum-hold variant made *less* per day. Cutting a trade
  short shrank its profit faster than it shortened the hold, because the losers still run their
  full −7% while the winners get capped.
- **T1 (10-day time stop) and T2 were faster in 2021–24 but slower in 2025–26.** That is the
  recurring DEV-only pattern. T1 still made money in both windows and is the one to re-test with
  fresh data.
- **Fast is not the same as profitable.** Mean reversion is the quickest (4 days) and loses about
  5–6% per 30 days invested.
- **The real limit is signal supply, not holding time.** Money sits idle 16–59% of days. Faster
  exits only add idle days (P1: 57% idle in OOS). More good signals is the lever for speed.
