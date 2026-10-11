# Decision Note — Minimum-Position Floor vs Pilot Capital (2026-08-07)

## The problem

`backtest_engine.py` refuses any position under **₹30,000** (`MIN_POSITION_VALUE`, regime-scaled).
Live has **no floor at all**. At ₹50k pilot capital the position cap is 20% = **₹10,000**, so:

- The validated backtest **structurally cannot trade at ₹50k** (or ₹1L; it only trades from ~₹1.5L).
- Live happily takes ₹5–10k positions the validated methodology would refuse.
- Every live pilot trade is therefore outside the validated ruleset, and no backtest reference
  exists at pilot scale. (Surfaced by the 2026-08-07 fidelity audit.)

## Real Zerodha CNC cost math

Current charges: CNC delivery brokerage **₹0**; STT 0.1% buy + 0.1% sell; NSE exchange
transaction charge 0.00297%/side; SEBI ₹10/crore/side; stamp duty 0.015% buy-side;
GST 18% on (brokerage + exchange + SEBI); DP charge **₹13.5 + GST = ₹15.93** per scrip
per sell day. Flat-exit round trip:

| Position | Round-trip ₹ | % of position | Backtest model ₹ (ex-slippage) | Model % |
|---:|---:|---:|---:|---:|
| ₹5,000  | ₹27.04  | **0.541%** | ₹58.36  | 1.167% |
| ₹10,000 | ₹38.15  | **0.382%** | ₹69.51  | 0.695% |
| ₹20,000 | ₹60.38  | **0.302%** | ₹91.82  | 0.459% |
| ₹30,000 | ₹82.60  | **0.275%** | ₹114.13 | 0.380% |

Structure: variable charges are a scale-free **0.222%** of position; the only fixed cost is
the **₹15.93 DP charge**. (Slippage — 0.1%/side in the backtest model — is scale-independent
and excluded here; it applies equally at every size.)

**Key thresholds (real charges):**

- Round trip exceeds **0.5%** of position only below **≈ ₹5,700**.
- Exceeds 1% only below ≈ ₹2,000.
- The floor's stated rationale — *"fixed costs eat 15–40% of tiny positions"* — becomes true
  only at **₹108 down to ₹40** positions. That is a literal 1-share dust position created when
  the cash cap shrinks an entry to its minimum, which is the case the guard was actually
  written for. It has no relevance at ₹10k.

**The model itself overstates fixed costs.** `portfolio/pnl_calculator.py` charges ₹20/side
brokerage — Zerodha's *intraday* fee. CNC delivery is ₹0; real fixed cost is ₹15.93 (DP), not
₹47.20 (₹40 + GST). It also omits the DP charge and uses the pre-Oct-2024 exchange rate
(0.00335% vs 0.00297%). Net: the backtest overstates round-trip cost by ~₹24/trade — worst at
small sizes (0.695% modeled vs 0.382% real at ₹10k). Even under this overstated model, the
0.5% line sits at ≈ ₹17k — the ₹30k floor was never a cost line under either model.

## Verdict on the ₹30k floor

**Not cost-justified.** It is ~5x above the 0.5% line under the backtest's own (overstated)
cost model and ~25x above the level where fixed costs begin to matter in reality. A
cost-justified floor is **₹6,000** (rounds up the ₹5,700 crossing of the 0.5% real-cost line).

## Options

**(a) Lower the backtest floor to ₹6,000 and re-validate — RECOMMENDED.**
Floor becomes a shared config constant applied identically in backtest and live (live gains a
floor it never had; it only blocks true dust). Re-run the full gauntlet at ₹50k and ₹1L.
Cost model left untouched for the re-run — its overstated fixed costs bias *against* small
positions, so a pass at pilot scale is a conservative pass.
*Ladder:* ₹50k trades ₹10k positions (~0.38% real charges), ₹1L trades ₹20k (~0.30%); every
rung gets a validated backtest reference before promotion. If the re-run shows the edge does
NOT survive ₹50k cost drag, the honest answer is raising pilot capital to ~₹1.5L, not trading
unvalidated dust — and we learn that from the backtest, not live money.

**(b) Add the ₹30k floor to live.** Fidelity restored, but ₹30k > the ₹10k position cap →
zero trades until capital ≥ ₹1.5L. The "≥30 live trades in 3 months" promotion criterion
becomes unreachable; the ladder deadlocks at its first rung. Only defensible if (a)'s re-run
fails at ₹50k *and* ₹1.5L cannot be committed.

**(c) Keep the mismatch, re-scope pilot as plumbing-only.** Plumbing is already verified
(orders, GTTs, reconcile all exercised by 2026-07-31/08-06 work). The pilot's remaining value
is behavioral fidelity, which this option permanently forfeits — every trade stays
out-of-methodology and the promotion decision would rest on unvalidated behavior. Rejected.

## Decision

Option (a). Floor = **₹6,000**, shared between engines. Gauntlet re-run at ₹50k and ₹1L
(dev 2021-06→2024-12, untouched validation 2025-01→2026-06, 2.0%/20% live sizing, bootstrap
≥80% acceptance) is the gate: pilot continues at ₹50k only if the edge survives at that scale.

*Follow-up (separate change, not part of the re-run):* correct the cost model to real CNC
charges (₹0 brokerage, +₹15.93 DP, 0.00297% exchange) and re-baseline all strategies once.
