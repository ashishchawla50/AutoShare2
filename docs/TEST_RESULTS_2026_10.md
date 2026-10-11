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
