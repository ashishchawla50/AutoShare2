# XYZ trade example — when we sell, how much, and when the stop loss moves

One XYZ trade, run through the two exit plans recorded in
[WINNING_STRATEGY.md](WINNING_STRATEGY.md):

1. **Current exits**: the vcp_v2 exit rules, with the first stop at −10%
2. **my_strategy**: sell 50% at +10%, then let the rest run

Updated 2026-10-11.

---

## The setup (same for both)

- **Capital:** ₹50,000 (the pilot). Each position is at most 20% of capital, so ₹10,000.
- **XYZ** is in a strong uptrend and goes quiet between ₹94 and ₹98 for a few weeks.
- One evening it closes at ₹99, above ₹98, on double the normal volume (the rule needs 1.5× since 2026-10-11). That is the breakout.
- **Next morning we buy 100 shares at ₹100 = ₹10,000.**
- **First stop loss: ₹90** (−10%). If it is hit right away, the loss is ₹1,000, which is 2% of
  capital: exactly the bot's risk limit per trade.

At ₹1 lakh capital everything is the same with 200 shares (₹20,000), so every rupee amount doubles.

---

## Strategy 1 — Current exits

**What gets sold:** 100% of the shares, all at once, when an exit rule fires. It never sells part.

| XYZ price | What we sell | Stop loss becomes |
|---|---|---|
| ₹100 (buy) | — | **₹90** |
| Every day | — | **10-day average × 0.97**, if higher. Example: 10-day average ₹97 → stop ₹94.10 |
| Closes at ₹110 (+10%) | nothing | at least **₹100** (the trade can no longer lose money) |
| Closes at ₹115 (+15%) | nothing | at least **₹105** |
| Closes at ₹120 (+20%) | nothing | at least **₹110** |
| Closes at ₹130 (+30%) | nothing | at least **₹120** |
| Stop is hit | **100%** | — |
| 15 trading days held and still under ₹107 | **100%** (time stop) | — |

The stop **only moves up**. A change decided on a day's closing price applies from the next day.
The 10-day-average rule usually lifts the stop above ₹90 within a day or two.

## Strategy 2 — my_strategy

**What gets sold:** **50%** at ₹110, then **the other 50%** when the stop is hit.

| XYZ price | What we sell | Stop loss becomes |
|---|---|---|
| ₹100 (buy) | — | **₹90**, fixed until ₹110 |
| Touches ₹110 (+10%) | **50% (50 shares) at ₹110** | **₹100** (buy price) |
| After ₹110, every day | — | **10-day average × 0.97**, if higher (never below ₹100) |
| Stop is hit | **the other 50% (50 shares)** | — |

There is no profit target on the second half and no time stop.

---

## The same four price paths, through both plans

### A — Big winner
XYZ touches ₹110 on day 3, closes ₹121 on day 10 and ₹132 on day 15, peaks at ₹145 on day 24,
then falls. By then the follow-the-price stop is about ₹136.

| | Current exits | my_strategy |
|---|---|---|
| Day 3 (₹110) | nothing sold; stop rises over the next days | **sell 50 @ ₹110** (+₹500); stop → ₹100 |
| Day 10 (₹121) | stop ≥ ₹110 | stop follows the price |
| Day 15 (₹132) | stop ≥ ₹120 | stop follows the price |
| Day 26 (falls to ₹136) | **sell 100 @ ₹136** (+₹3,600) | **sell 50 @ ₹136** (+₹1,800) |
| **Result** | **+₹3,600 (+36%)** | **+₹2,300 (+23%)** |

### B — Breakout fails
XYZ slides from day 1 and reaches ₹90 on day 6.

| | Current exits | my_strategy |
|---|---|---|
| What happens | The 10-day-average rule had already raised the stop to about ₹94: **sell 100 @ ₹94** | Stop is still ₹90: **sell 100 @ ₹90** |
| **Result** | **−₹600 (−6%)** | **−₹1,000 (−10%)** |

### C — Touches ₹110, then falls back
XYZ touches ₹111 on day 4 but closes at ₹108, then slides to ₹100 by day 8.

| | Current exits | my_strategy |
|---|---|---|
| What happens | Never closed at ₹110, so no table step; the 10-day-average stop (about ₹101) is hit: **sell 100 @ ₹101** | **Sell 50 @ ₹110** on day 4 (+₹500); stop → ₹100; **sell 50 @ ₹100** on day 8 (₹0) |
| **Result** | **+₹100 (+1%)** | **+₹500 (+5%)** |

### D — Goes nowhere
XYZ drifts between ₹98 and ₹104 and closes at ₹104 on day 15.

| | Current exits | my_strategy |
|---|---|---|
| What happens | Time stop on day 15 (under +7%): **sell 100 @ ₹104** | No time stop: **keep holding** until ₹110 or ₹90 is hit |
| **Result** | **+₹400 (+4%)**, and the money is free for the next trade | Still open. The ₹10,000 stays tied up. |

Costs (STT, stamp duty, and ₹16 per sell order) are about ₹40 per ₹10,000 trade. my_strategy
sells twice, so it pays the ₹16 sell charge twice.

**In short:** the **current exits** lose less on failures (B), stay out of dead trades (D), and
keep the most from a big run (A). **my_strategy** cashes in more often (C) and wins 57% of trades
against 39%. In testing, both earned about the same overall.

---

## Test results (summary — details in WINNING_STRATEGY.md)

| | Current exits | my_strategy |
|---|---|---|
| PF, all years 2021–26 | 1.52 | 1.46 |
| PF, 2021–24 / 2025–26 | 1.68 / 0.76 | 1.70 / 0.69 |
| Average per trade | +1.65% | +2.01% |
| Win rate | 39% | 57% |
| Average days held (trading days) | 12–13 | 28 |

PF = money made on winners divided by money lost on losers.

### How many trades, and how many days, over Sept 2021 → June 2026

At ₹90 stop each XYZ-style position is 20% of capital, so **4 positions fit at once**. A signal
arriving while all 4 are full is skipped.

| 4 positions at once (realistic) | Current exits | my_strategy |
|---|---|---|
| Trades taken (of 297 signals) | **150** | 90 |
| Days held per trade: average / longest | **12 / 51** | 28 / 114 |
| Days with no position at all | 40% | **17%** |
| Win rate | 39% | **57%** |
| Rough total return, ~4.8 years | +19% | **+27%** |

With 8 smaller positions (10% each) the order flips: the current exits take 219 trades
for about +26%, and my_strategy takes 154 trades for about +20%.

### Full portfolio test result (2026-10-11): current exits win

₹50,000, 2% risk, both plans with a ₹90 first stop:

| | Current exits | my_strategy |
|---|---|---|
| 2021–24: trades / PF / return | 126 / **1.40** / **+28.2%** | 96 / 1.40 / +25.1% |
| 2025–26: trades / PF / return | 39 / **1.75** / **+8.2%** | 29 / 1.01 / +0.3% |
| Average days held (calendar) | **19** | 40–48 |

**The current exits stay.** my_strategy tied in 2021–24 but earned nothing in 2025–26.
At ₹1 lakh, keep 2% risk with 20% positions. 8 smaller positions (1% risk) earned almost
nothing in the same test. Details are in WINNING_STRATEGY.md.

---

## Why not a tight ₹98 stop with selling at ₹104?

Tested on the same signals, "stop ₹98, sell half at ₹104, stop to ₹103, rest at ₹110" lost money
(PF 0.85, about −0.2% per trade). NSE stocks often move 2% in a day for no reason, so a ₹98 stop
is hit by normal noise about 72% of the time before the price reaches ₹104.

## Important notes

- **The live bot today is different from both plans.** It starts with a ₹93 stop (−7%), and it
  also sells 33% at +20% and 33% at +30%. The backtest never sells part of a position
  (DIVERGENCE_INVENTORY.md, item B1).
- **The full portfolio test kept the current exits;** my_strategy was rejected. The weak point of
  the strategy is which breakouts it buys, not how it sells.
