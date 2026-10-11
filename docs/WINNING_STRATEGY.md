# Winning strategy — two exit plans for vcp_v2

Recorded 2026-10-11. Two exit plans came out on top after testing about 25 variants on every
vcp_v2 breakout signal from Sept 2021 to June 2026 (297 signals):

1. **Current exits**: the vcp_v2 exit rules in the code, with the first stop at −10%
2. **my_strategy**: sell 50% at +10%, then let the rest run

**Final status (2026-10-11): the full portfolio test kept the CURRENT EXITS, and my_strategy was
rejected.** It tied in 2021–24 but fell to PF 1.01 in 2025–26 against 1.75. See "Portfolio test
RESULT" near the bottom. The per-signal study below is what led to the test.

Both plans use the **same entry**: the vcp_v2 breakout. The stock is in a strong uptrend, goes
quiet in a tight range, closes above the top of that range (the pivot) on 2× normal volume (1.5× since 2026-10-11;
all results in this file were run at 2×), and
we buy at the next morning's open.

---

## Strategy 1 — Current exits

| When | What we sell | Stop loss becomes |
|---|---|---|
| Buy at ₹100 | — | **₹90** (−10%) |
| Every day | — | Raised to **10-day average price × 0.97** if that is higher |
| Close ≥ ₹110 (+10%) | nothing | at least **₹100** |
| Close ≥ ₹115 (+15%) | nothing | at least **₹105** |
| Close ≥ ₹120 (+20%) | nothing | at least **₹110** |
| Close ≥ ₹130 (+30%) | nothing | at least **₹120** |
| Stop hit | **100%: everything** | — |
| 15 trading days held and profit under +7% | **100%: everything** (time stop) | — |

- It **never sells part** of the position: one buy, one sell.
- The stop **only moves up**. Changes decided on a day's close apply from the next day.
- **The 10-day-average rule usually lifts the stop above ₹90 within a day or two.** In the
  test, only 2% of trades were stopped at the original −10%.

## Strategy 2 — my_strategy

| When | What we sell | Stop loss becomes |
|---|---|---|
| Buy at ₹100 | — | **₹90** (−10%), fixed until ₹110 |
| Price touches ₹110 (+10%) | **50%** of the shares at ₹110 | **₹100** (buy price) |
| After ₹110, every day | — | Raised to **10-day average × 0.97** if higher (never below ₹100) |
| Stop hit | **the remaining 50%** | — |

- **Two sells at most:** half at +10%, the other half when the stop is hit.
- **No profit target** on the second half. It follows the price up.
- **No time stop.** A stock that drifts between ₹90 and ₹110 is held until one of them is hit.

---

## Results

297 signals, Sept 2021 → June 2026. Market-regime filter on. Each signal is treated as its own
trade, and costs are deducted. PF = money made on winners divided by money lost on losers;
above 1 means profit.

### Per trade (in %)

| | Current exits | my_strategy |
|---|---|---|
| **All years: PF** | **1.52** | 1.46 |
| All years: average per trade | +1.65% | **+2.01%** |
| Win rate | 39% | **57%** |
| **2021–24** PF | 1.68 | **1.70** |
| **2025–26** PF | **0.76** | 0.69 |
| Average days held | **13** | 28 |
| Best trade | **+111%** | +61% |

### How the trades ended

**Current exits**

| Outcome | Trades | Result each |
|---|---|---|
| Stopped at the original ₹90 | 6 (2%) | −10.0% |
| Stopped at a raised stop | 231 (78%) | +1.9% on average (from a small loss to a big win) |
| Time stop (15 days, under +7%) | 60 (20%) | +3.2% on average |
| Trades above +20% / +30% / +50% | 15 / 13 / 7 | |

**my_strategy**

| Outcome | Trades | Result each |
|---|---|---|
| Fell to ₹90 before reaching ₹110 | 127 (43%) | −10.1% |
| Half sold at ₹110, rest stopped near ₹100 | 29 (10%) | +5.2% |
| Half sold at ₹110, rest sold higher | 141 (47%) | +12.7% on average, best +61% |

### In rupees, with real costs and whole shares

The position is 20% of capital, and a stock is skipped if the position would be under ₹6,000.
Costs use the real Zerodha CNC model (STT, stamp duty, and ₹16 per sell order), about 0.4% per
trade.

| Price group | Trades | ₹50k: Current exits | ₹50k: my_strategy | ₹1L: Current exits | ₹1L: my_strategy |
|---|---|---|---|---|---|
| ~₹100 (under 250) | 93 | +₹3,884 (PF 1.12) | **+₹35,488 (2.13)** | +₹9,222 (1.14) | **+₹73,510 (2.17)** |
| ~₹500 (250–750) | 84 | **+₹23,386 (1.89)** | +₹14,495 (1.39) | **+₹48,771 (1.93)** | +₹31,715 (1.42) |
| ~₹1000 (750–1500) | 47 | **+₹13,475 (1.93)** | +₹10,014 (1.49) | **+₹29,294 (2.02)** | +₹21,714 (1.52) |
| ~₹2000 (1500–3500) | 41 | −₹4,839 (0.67) | −₹11,877 (0.51) | −₹8,766 (0.71) | −₹22,348 (0.55) |
| ~₹5000 (over 3500) | 19 / 29* | −₹993 (0.81) | −₹1,444 (0.83) | **+₹1,600 (1.12)** | −₹2,418 (0.91) |
| **All** | 284 / 294* | +₹34,912 (PF 1.37) | **+₹46,676 (1.38)** | +₹80,120 (**1.42**) | **+₹1,02,174** (1.40) |

\* At ₹50k (₹10,000 position) only 19 of the 32 stocks above ₹3,500 fit; at ₹1L, 29 do.

**The rupee totals are NOT what the bot would earn.** They add up every signal as if each had
fresh money. The real bot holds at most 8 positions, so it skips many signals.

### What the results say

- The two plans are **almost tied** on PF. **my_strategy** wins more often (57% against 39%) and
  earns more per trade. The **current exits** hold trades half as long (13 days against 28), keep
  the rare huge winners (+111%), and lose less in 2025–26.
- my_strategy does best on **cheap stocks (under ₹250)**. The current exits do best on
  **₹250–1500** stocks. Each group is too small to pick a plan by price.
- **₹1500–3500 stocks lose with both plans.**
- **2025–26 loses with both plans.** The weak point is which breakouts get bought, not how
  they are sold.

### Number of trades and days used

With a −10% first stop and 2% risk per trade, each position is **20% of capital**. The bot also
keeps 20% in cash, so **only 4 positions fit at once**, even though the limit is 8. A signal that
arrives while every slot is full is skipped. The table simulates that, taking signals in date
order. The 8-slot rows show what would happen with smaller positions (10% each).

The window is Sept 2021 → June 2026: 1,041 trading days, 297 signals.

| | Current exits | my_strategy |
|---|---|---|
| Days held per trade: average / median / longest | **12 / 10 / 51** | 28 / 22 / 114 |
| **4 slots (realistic): trades taken** | **150** (117 in 2021–24, 33 in 2025–26) | 90 (67 / 23) |
| 4 slots: signals skipped (all slots full) | 147 | 207 |
| 4 slots: total days in positions | 1,764 | 2,491 |
| 4 slots: average slots in use | 1.7 of 4 | 2.4 of 4 |
| 4 slots: days with **no** position | 40% | **17%** |
| 4 slots: days with all 4 slots full | 24% | 38% |
| 4 slots: win rate / PF / average per trade | 39% / 1.20 / +0.65% | **57% / 1.33 / +1.50%** |
| **4 slots: rough total return over ~4.8 years** | +19% | **+27%** |
| 8 slots (10% each): trades taken | **219** | 154 |
| 8 slots: PF / average per trade | **1.39 / +1.17%** | 1.29 / +1.30% |
| **8 slots: rough total return over ~4.8 years** | **+26%** | +20% |

"Rough total return" adds up each trade's % × its share of capital. It leaves out compounding,
CAUTIOUS-regime half sizing and cash rounding, so use it only to compare the two plans.

**What this shows:**

- **my_strategy holds trades 2–2½ times longer,** so it takes fewer trades: 90 against 150 with
  4 slots. It skips 70% of signals.
- **With 4 slots (the realistic case), my_strategy comes out ahead:** +27% against +19%, PF 1.33
  against 1.20. The current exits often sit in cash (no position on 40% of days), while
  my_strategy's longer trades keep the money working.
- **With 8 slots, the order flips:** the current exits lead, +26% against +20%.
- **So the winner depends on the slot count,** and on which signals happen to be taken. Only a
  real portfolio backtest can settle it.
- **Both plans earn little:** about +4–6% a year before compounding. Neither is yet a strong edge.

### More capital does not mean more positions

The number of positions comes from percentages, not rupees. 2% risk with a 10% stop gives a
20% position, and with 20% kept in cash that is 80% ÷ 20% = **4 positions at any capital**.
More capital only makes each position bigger.

| Risk per trade | Position size | Positions at once | At ₹50k | At ₹1L |
|---|---|---|---|---|
| 2% (today) | 20% | 4 | ₹10,000 each | ₹20,000 each |
| 1% | 10% | 8 | ₹5,000 each: **not allowed** (under the ₹6,000 minimum) | ₹10,000 each: works |

So the real effect of reaching ₹1L is that **1% risk with 8 positions becomes possible**. That
setup favoured the current exits in the slot simulation, while 4 positions favoured my_strategy.

### Plans that lost to these two (same test, ₹90 first stop)

| Plan | All-years PF | Average per trade |
|---|---|---|
| Sell 50% at ₹110 (stop to ₹100), rest at ₹115 | 1.31 | +1.34% |
| "Tweak (105 SL)": at ₹110 stop to ₹105, sell everything at ₹115 | 1.32 | +1.43% |
| Sell 50% at ₹110 (stop to ₹109), rest at ₹115 | 1.28 | +1.21% |
| Stop ₹98, sell 50% at ₹104 (stop to ₹103), rest at ₹110 | 0.85 | −0.23% |

Plans that sell everything at a fixed price (₹110 or ₹115) always lost to plans that let some
shares keep running. The rare big winners pay for the −10% losses.

---

## Limits of these tests

1. **Not a full portfolio backtest.** The "trades and days" section simulates slots only. It does
   not include real cash, compounding, CAUTIOUS-regime half sizing, or the bot's rule of ranking
   signals within the same day. Which signals get taken changes the result.
2. **The current-exit simulation is simplified.** It leaves out the "breakout failed" and
   "heavy selling" exits that the real backtester has.
3. **Daily candles only.** When a stop and a target are both touched on the same day, the stop is
   assumed to come first.
4. **Many variants were tried on the same 297 signals,** so the best ones look better than they
   will be in the future.
5. **The live bot differs:** it starts with a ₹93 stop (−7%), and it sells 33% at +20% and 33% at
   +30%, which the backtest never does (DIVERGENCE_INVENTORY.md item B1).

---

## Portfolio test plan (written 2026-10-11, BEFORE running)

This is fixed now, before any result is seen, so it cannot be bent to fit the results.

**Engine:** `scripts/run_backtest.py --strategy vcp_v2` (the fast engine, proven against the
reference oracle), with my_strategy built in as an exit mode. Full portfolio rules apply: slots,
cash, sizing, regime gate (BEAR blocks entries, CAUTIOUS halves size), real CNC costs, and the
drawdown halt disabled (`--no-dd-halt`) so each window is measured in full.

**Both plans use a −10% first stop.** "Current exits" means the engine's full vcp_v2 exits:
trailing table, 10-day-average floor, breakout-failed and heavy-selling exits, 15-day time stop.

**Setups:**
- **S1:** ₹50,000, 2% risk, 20% max position (4 positions)
- **S2:** ₹1,00,000, 1% risk, 10% max position (8 positions)

**Windows:** 2021-09-01 → 2024-12-31 (develop) and 2025-01-01 → 2026-06-30 (confirm).

**Decision rule, per setup:**
- my_strategy replaces the current exits **only if** its PF is higher in **both** windows **and**
  its worst drawdown in 2021–24 stays above −20%.
- Otherwise the current exits stay.
- No re-runs with changed settings after seeing results; any new idea is a new pre-registered test.

Today's live stop (−7%) with the current exits is run as a reference only. It is not part of the
decision.

## Portfolio test RESULT (run 2026-10-11, after the plan above was committed)

**Verdict: the current exits STAY in both setups. my_strategy is rejected.**

| Setup | PF higher in both windows? | 2021–24 drawdown above −20%? | Decision |
|---|---|---|---|
| S1 ₹50k, 2% risk | **No**: 1.40 vs 1.40 (tie), then 1.01 vs **1.75** | yes (−12.9%) | current exits stay |
| S2 ₹1L, 1% risk | **No**: 1.15 vs 1.06, then 0.80 vs **1.04** | yes (−8.7%) | current exits stay |

### S1 — ₹50,000, 2% risk, 20% max position (4 positions)

| Plan | Window | Trades | Win % | PF | Return | CAGR | Max DD | Avg days held | Avg positions open | Days with no position |
|---|---|---|---|---|---|---|---|---|---|---|
| Current exits, −10% | 2021–24 | 126 | 40% | **1.40** | **+28.2%** | +7.7% | −13.8% | 19 | 1.9 | 40% |
| Current exits, −10% | 2025–26 | 39 | 51% | **1.75** | **+8.2%** | +5.4% | −5.9% | 19 | 1.4 | 58% |
| my_strategy, −10% | 2021–24 | 96 | 60% | 1.40 | +25.1% | +7.0% | −12.9% | 40 | 3.1 | 22% |
| my_strategy, −10% | 2025–26 | 29 | 52% | 1.01 | +0.3% | +0.2% | −9.3% | 48 | 2.6 | 38% |
| (ref) current exits, −7% | 2021–24 | 130 | 36% | 1.29 | +22.1% | +6.2% | −17.1% | 18 | 1.9 | 41% |
| (ref) current exits, −7% | 2025–26 | 39 | 51% | 1.78 | +8.4% | +5.5% | −5.7% | 19 | 1.4 | 58% |

### S2 — ₹1,00,000, 1% risk, 10% max position (8 positions)

| Plan | Window | Trades | Win % | PF | Return | CAGR | Max DD | Avg days held | Avg positions open | Days with no position |
|---|---|---|---|---|---|---|---|---|---|---|
| Current exits, −10% | 2021–24 | 157 | 39% | 1.06 | +2.7% | +0.8% | −9.4% | 18 | 2.3 | 40% |
| Current exits, −10% | 2025–26 | 44 | 41% | **1.04** | +0.4% | +0.2% | −4.6% | 18 | 1.5 | 58% |
| my_strategy, −10% | 2021–24 | 115 | 59% | **1.15** | **+6.3%** | +1.8% | −8.7% | 38 | 3.6 | 22% |
| my_strategy, −10% | 2025–26 | 32 | 47% | 0.80 | −2.4% | −1.6% | −7.1% | 47 | 2.9 | 38% |
| (ref) current exits, −7% | 2021–24 | 160 | 36% | 0.94 | −2.6% | −0.8% | −11.0% | 17 | 2.2 | 41% |
| (ref) current exits, −7% | 2025–26 | 44 | 41% | 1.06 | +0.5% | +0.3% | −4.5% | 18 | 1.5 | 58% |

Days held here are calendar days (the earlier tables used trading days).

### What the portfolio test showed

1. **my_strategy fails in 2025–26.** It ties in 2021–24 (PF 1.40 each) but collapses to PF 1.01
   in 2025–26 against 1.75. The simplified slot simulation had my_strategy ahead; the real
   portfolio (cash, regime sizing, actual signal order) reversed that. This is why the full test
   was needed.
2. **S2 is much worse than S1.** 8 positions at 1% risk earned almost nothing (+0.8% CAGR in
   2021–24) against +7.7% for 4 positions at 2% risk. At ₹1L, **keep 2% risk with 20% positions**;
   more, smaller positions do not help.
3. **A −10% first stop looked better than −7%** with the current exits at S1: PF 1.40 against 1.29
   in 2021–24, drawdown −13.8% against −17.1%, and a tie in 2025–26. This was a reference row,
   not a pre-registered test, so **it is a hypothesis to test separately, not a decision.**
4. **Money sits idle a lot:** no position on 40–58% of days with the current exits. There are too
   few signals, not too few slots. More good signals (entries) is where profit can grow.

The scripts used are in the 2026-10-11 session scratchpad (not in the repo). The
numbers above are the full record.
