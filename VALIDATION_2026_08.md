# Pilot-Scale Re-Validation — vcp_v2 at ₹50k / ₹1L (2026-08-08)

Runs executed 2026-08-07/08 on the VPS. Dev = 2021-06-01→2024-12-31,
untouched validation (OOS) = 2025-01-01→2026-06-30. Sizing 2.0%/20% (live
config), Nifty 500 PIT universe, regime gate on, dust floor ₹6,000
(`DUST_FLOOR_DECISION.md`), bootstrap = 10,000 resamples, accept ≥80%.

## Headline

**The edge survives at pilot scale.** ₹50k OOS profit factor **1.75**
(bootstrap **84.7%**, edge confirmed) against **1.77** at ₹5L under the same
cost model. Cost drag costs about **0.02 PF** between ₹6k and ₹64k positions —
an order of magnitude less than the first (wrong) run suggested.

## Results

| Run | Trades | PF | Bootstrap | Max DD | Return | Avg pos | Cost/trade |
|---|---:|---:|---:|---:|---:|---:|---:|
| **50k OOS, real costs** | 31 | **1.75** | **84.7%** ✓ | −4.35% | +5.79% | ₹6,186 | 0.71% |
| 1L OOS, real costs | 38 | **1.84** | **89.3%** ✓ | −4.69% | +7.07% | ₹12,200 | 0.57% |
| 5L OOS, real costs *(control)* | 38 | **1.77** | **88.6%** ✓ | −4.92% | +7.49% | ₹64,340 | 0.45% |
| **50k dev, real costs** | 192 | **1.19** | 73.4% ✗ | −19.78% | +22.58% | ₹8,854 | 0.63% |
| 50k OOS, legacy costs | 31 | 1.33 | 68.3% ✗ | −4.91% | +2.96% | ₹6,138 | 1.27% |
| 50k dev, legacy costs, halt ON | 48 | 0.71 | 21.1% ✗ | −21.12% | −10.17% | ₹8,564 | 1.04% |

Published baselines (₹5L, legacy costs): dev PF 1.11 @1.5%/15% and **1.23
@2.0%/20%**; OOS PF 1.61 @1.5%/15% and **1.73 @2.0%/20%**, bootstrap 85.6%,
max DD −4.4%.

Apples-to-apples (both @2.0%/20%): **OOS 1.75 at ₹50k vs 1.73 published at
₹5L. Dev 1.19 at ₹50k vs 1.23 published at ₹5L.**

## Two corrections that made the first answer wrong

**1. The drawdown halt is absorbing.** It fired 2022-02-14 and then every
remaining day — 737 times. It blocks entries, so equity can't grow, so
drawdown-from-peak never shrinks, so it never lifts. All 48 trades came from
the first 8 months; 2023 and 2024 returned exactly +0.00%. The ₹5L validation
never tripped it (max DD −19.0%), so it was never noticed. `--no-dd-halt`
added; the honest dev number is **1.19 over 192 trades**, not 0.71 over 48.

**2. The cost model charged brokerage that does not exist.** ₹20/side is
Zerodha's *intraday* fee; CNC delivery is **₹0**. The model also omitted the
₹15.93 DP charge and used the pre-Oct-2024 exchange rate. In the ₹50k dev run,
modeled costs were **₹4,003 of a ₹5,085 loss** — gross P&L was only −₹1,082.

**The control that proves this isn't goalpost-moving:** correcting costs moved
₹5L OOS from 1.73 → 1.77 (**+0.04**) but ₹50k OOS from 1.33 → 1.75 (**+0.42**).
That asymmetry is exactly what a fixed per-trade fee predicts — it is a large
fraction of a ₹6k position and a rounding error on a ₹64k one. The correction
did not preferentially rescue the pilot; it removed a phantom fee that was only
ever punishing small positions.

## Cost drag by scale (modeled, incl. 0.2% round-trip slippage)

| Capital | Avg position | Cost/trade | Ex-slippage |
|---|---:|---:|---:|
| ₹50k | ₹6,186 | 0.71% | 0.51% |
| ₹1L | ₹12,200 | 0.57% | 0.37% |
| ₹5L | ₹64,340 | 0.45% | 0.25% |

Pilot-scale drag is ~0.26pp worse per trade than ₹5L. Real, but it costs ~0.02
PF, not the ~0.4 the broken model implied.

## Acceptance verdict

| Criterion | ₹50k OOS | Verdict |
|---|---|---|
| PF > 1.5 | 1.75 | PASS |
| Bootstrap ≥ 80% | 84.7% | PASS |
| Max DD < 25% | −4.35% | PASS |
| Win rate > 40% | 48.4% | PASS |

**₹50k passes the original acceptance criteria on the untouched validation
window.** ₹1L passes more comfortably (1.84 / 89.3%) and is the validated
reference for the promotion gate.

**The dev window is the caveat.** PF 1.19 is close to the ₹5L baseline's 1.23,
but bootstrap is 73.4% (below the 80% bar) and max drawdown is **−19.78%,
brushing the 20% emergency halt**. Under the old cost model it breached and the
absorbing halt ended the run. Read plainly: a 2021-22-style stretch at ₹50k
would come within ~0.2pp of tripping the halt, and the halt as implemented is
unrecoverable in both engines. That is a live operational risk independent of
the edge, and it argues for the ₹1L rung sooner rather than later.

## Events filter (Task 3) — KEEP, with a caveat

₹50k OOS, real costs, filter off vs on:

| | Trades | PF | Bootstrap | Return |
|---|---:|---:|---:|---:|
| Off | 31 | 1.75 | 84.7% | +5.79% |
| **On** | 23 | **2.22** | **90.7%** | **+7.01%** |

The filter removed 10 trades whose aggregate P&L was **−₹1,051** (3 winners
+₹470, 7 losers −₹1,521) and, by freeing slots and cash, added 2 (−₹460). Net
it removed losers. Per the stated rule — improves or preserves PF ⇒ keep in
both engines — **the filter stays**, and the backtest keeps the backport.

Removed trades: WELCORP, BIKAJI, JINDALSTEL, PNB, HINDPETRO, PAYTM, HUDCO,
SBIN, BSE, BHARATFORG.

**Caveats.** One window, 10 removed trades — a thin sample. The dev-window
pair was dropped for runtime and should be run to confirm.

**Action this implies for live:** live's filter is **half dead**. Before
2026-08-07 its events table held 2,280 rows, all dividend/split/bonus and
**zero results rows** — the daily job only queries the corporate-actions
endpoint, which carries no results dates. So the "no buying within 7 days of
results" half has never fired in live. The validated run above used the full
history including board-meeting results dates. To actually match, live's daily
`refresh_corporate_events` needs the board-meetings endpoint added (the logic
already exists in `scripts/backfill_events.py`). Not done — it makes live
strictly tighter off a single-window result and deserves explicit sign-off.

## Runs not completed

Dropped for runtime (measured ~3.2h per OOS run, ~6.7h per dev run):
`c5l_dev` (5L dev control), `c1l_dev_nh`, `c50k_dev_ev_nh` (events on dev),
`l5l_oos` (legacy 5L reproduction), `r1b` (50k dev legacy halt-off). The
dev-window events pair is the one worth running next.
