# Breakout Exit-Geometry Validation — Pre-Registration

**Registered:** 2026-08-10, before any result was generated.
**Question:** Was fixed-target exit geometry the reason the Range Breakout strategy
failed the July 2026 campaign, and does a trailing-ratchet variant clear the bar at
pilot capital?

This document is committed *before* the runs. Any change to the acceptance bar,
windows, or interpretation rules after results exist must be recorded as an
amendment below, with its date — not by editing the original text.

---

## 1. What is being compared

Entry rules are **byte-identical** across both arms — `strategy/breakout_scanner.py`
`check_range_breakout()` is untouched (20-day high, 1.5x volume, EMA50, RSI 45–75).
Only exit geometry and nothing else differs.

| | **Control** (`breakout`) | **Variant** (`breakout_v2`) |
|---|---|---|
| Hard stop | 5% (`BREAKOUT_STOP_PCT`) | 5% (`BREAKOUT_STOP_PCT`) — same |
| Profit target | +10% fixed (`BREAKOUT_TARGET_PCT`) | none (`target_price = 0.0`) |
| Trailing stop | none | `calculate_trailing_stop()` ratchet |
| Breakdown exit | none | `check_technical_breakdown()` |
| Time stop | 7 days, unconditional | 15 days + 7% min-profit gate (`should_exit_on_time_stop`) |
| Position sizing | 20% of portfolio | 20% of portfolio — same |

The variant is deliberately **vcp_v2's full exit geometry** applied to breakout
entries, not merely "control minus the target". Three exit rules change together.
This is intentional: the hypothesis under test is that the whole fixed-target exit
model is what harvested losses at full frequency while amputating winners.

## 2. Capital and the sizing override — read this before questioning the numbers

All four runs use `--capital 50000` (pilot capital) and
**`--breakout-pos-pct 0.20` on both arms.**

The override is mandatory, not a tuning choice. At the `BREAKOUT_POSITION_SIZE_PCT`
default of 10%, a ₹50,000 portfolio sizes each breakout position to ₹5,000, which is
below the ₹6,000 `MIN_POSITION_VALUE` dust floor. The engine rejects every entry
(`is_valid = pos_value >= min_pos_value`), so the control arm would return **zero
trades in every window** — and because it never enters, equity never grows past the
₹60,000 needed to clear its own floor, making the condition absorbing.

The floor is not the thing that is wrong here: `DUST_FLOOR_DECISION.md` puts the real
CNC round-trip 0.5% crossover at ~₹5,700, so ₹5,000 positions genuinely are dust.
20% sizing yields ₹10,000 positions, clearing the floor, and matches the live
`MAX_POSITION_SIZE_PCT` cap of 20% that the pilot already runs. At a 5% stop this is
1.0% risk per trade — more conservative than vcp_v2's 2.0%.

**Consequence, stated up front:** this is *not* a reproduction of the July 2026
result. That run was ₹5L, `legacy` costs, drawdown-halt on, and 10% sizing. The
published breakout PF 0.69 is **context, not a control cell**. The comparison that
decides anything here is Variant vs Control *within this matrix*.

## 3. Fixed run configuration

Applied identically to all four runs:

- `--cost-model cnc` — real Zerodha delivery charges. The `legacy` default charges
  ₹20/side brokerage CNC does not incur, which distorts small-capital questions
  specifically (CLAUDE.md note 11).
- `--no-dd-halt` — the 20% drawdown halt is absorbing; a run that trips it is void
  for the remainder (CLAUDE.md note 10). The bar below is therefore an *unhalted*
  bar. A halt-on operational run is a separate question, deferred until an arm passes.
- **Events filter OFF** — omitted from all four. Rationale: the July campaign predates
  the events-filter backport, so leaving it off keeps exit geometry as the single
  variable. If the variant passes, a 5th robustness run with
  `--events-filter --events-db corporate_events_full.db` may be added.
- `--dump-trades` — a ledger per run. Without it a 6.7h run cannot be diagnosed, and
  a PF resting on 2–3 outlier trades cannot be distinguished from a real edge.

Windows: **Dev 2021-06-01 → 2024-12-31**, **OOS 2025-01-01 → 2026-06-30**. The OOS
window is untouched and must not be inspected before the dev result is recorded.

## 4. Acceptance bar

Evaluated **independently** in each window. An arm passes a window only if all four hold:

| Criterion | Threshold |
|---|---|
| Profit factor | > 1.50 |
| Bootstrap P(mean trade P&L > 0), 10k resamples | ≥ 80% |
| Max drawdown (unhalted) | < 25% |
| Total trades | ≥ 30 |

All four print automatically from `print_report()` — no extra flags required.

**Pre-registered interpretation rule:** if an arm returns **fewer than 30 trades** in
a window, that window is **INCONCLUSIVE for that arm, not a rejection**. Thin samples
must not be scored as evidence in either direction. Likewise, zero trades means a
structural block (floor, cash, or regime), and must be diagnosed as a defect before
any performance claim is made from that run.

## 5. Decision matrix

| Case | Dev | OOS | Verdict |
|---|---|---|---|
| 1 | Variant passes | Variant passes | **Exit geometry was the cause.** Breakout_v2 is a promotion candidate. Proceed to a halt-on operational run and an events-filter robustness run before any live consideration. |
| 2 | Variant fails | Variant passes | **Reject.** A single passing OOS window after a dev failure is the exact signature that rejected momentum_v3 (dev 0.99 → OOS 0.55, in reverse). Do not promote on the strength of one window. |
| 3 | Variant passes | Variant fails | **Reject.** Dev-window fitting. Same disposition as momentum v1–v4. |
| 4 | Variant fails | Variant fails | **Reject and close the question.** Breakout's failure is in the entry signal, not the exits. Do not revisit without a new entry hypothesis. |

In cases 2–4 breakout remains in `config.DISABLED_STRATEGIES` permanently and this
line of inquiry is closed. Only Case 1 permits further work.

**Control arm's role:** it is a validity check, not a candidate. If the control arm
does not itself produce ≥30 trades, the harness is not exercising the strategy and
the entire matrix is void regardless of what the variant prints.

## 6. Deployability caveat, recorded in advance

Even under Case 1, breakout at its native 10% sizing is undeployable at ₹50,000 —
positions land at ₹5,000, under the cost floor. A Case 1 result therefore supports
deployment only at 20% sizing, or at the ₹1.5L promotion tier where 10% sizing yields
₹15,000 positions. This is a known constraint before the runs, not a finding to be
rediscovered afterwards.

## 7. Code state at registration

- `backtest/backtest_engine.py` — `breakout_v2` added to `VALID_STRATEGIES`; sizing
  dispatch fixed to `chosen_strategy in ("breakout", "breakout_v2")`. Without this
  fix the variant fell through to VCP risk-based sizing and the two arms differed
  2:1 in position size, silently invalidating the comparison.
- `scripts/run_backtest.py` — `--breakout-pos-pct` override added.
- `main.py` — dust-floor cap reverted; live floor is again 1:1 with
  `DUST_FLOOR_DECISION.md`.
- Live divergences A1 (RS ≥ 70 gate) and B1 (partial profit booking) removed /
  gated off, per `DIVERGENCE_INVENTORY.md`.
- 163/163 tests pass.

---

# RESULTS — recorded 2026-08-10

All four runs: ₹50,000 capital, `--breakout-pos-pct 0.20`, `--cost-model cnc`,
`--no-dd-halt`, events filter off. Config confirmed in each run's log banner.

| | Dev Control | Dev Variant | OOS Control | OOS Variant |
|---|---|---|---|---|
| Profit factor | 0.65 | 0.70 | 0.51 | 0.68 |
| Bootstrap P(E>0) | 0.1% | 3.5% | 0.1% | 10.1% |
| Max drawdown | −59.88% | −41.57% | −21.85% | −10.45% |
| Total trades | 388 | 289 | 145 | 72 |
| Win rate | 36.9% | 28.0% | 37.9% | 27.8% |
| Avg win | +5.21% | +9.44% | +3.80% | +9.63% |
| Best trade | +10.88% | +97.84% | +9.38% | +29.47% |
| Ending capital | ₹22,568 | ₹30,345 | ₹39,202 | ₹45,607 |

**Verdict: CASE 4 — variant fails both windows. Reject and close the question.**

Every cell clears the ≥30-trade bar, so all four are genuine results, not thin
samples. The control arm reproduced the known failure (dev PF 0.65 against the
July 2026 campaign's 0.69 at different capital, costs and sizing), which
validates the harness.

## The hypothesis was directionally right and quantitatively insufficient

The variant beat the control on profit factor and drawdown in **both** windows —
a consistent, replicated improvement, not a one-window artifact:

- Avg win roughly doubled (+5.21% → +9.44% dev; +3.80% → +9.63% OOS)
- Best trade escaped the +10% cap (+10.88% → +97.84% dev)
- Max drawdown improved 18.3pp (dev) and 11.4pp (OOS)
- Capital destruction slowed (₹22,568 → ₹30,345 dev; ₹39,202 → ₹45,607 OOS)

So fixed-target geometry *was* amputating winners, exactly as hypothesised. But
the best profit factor achieved was 0.70 against a 1.50 bar, and bootstrap
peaked at 10.1% against an 80% bar.

**Cause of death: the entry signal.** Stop-loss exits are 82% of all variant
trades in both windows, and win rate falls to ~28% once the target is removed.
When four of five entries hit the stop, no exit rule can rescue the system —
there is nothing left to ride. Removing the target freed the rare large winner
while discarding the mechanism that banked the modest ones; the two effects
nearly cancel.

## Disposition

`breakout` and `breakout_v2` remain in `config.DISABLED_STRATEGIES` permanently.
Per the Case 4 rule this line of inquiry is closed: **do not revisit breakout
exit geometry.** Any future attempt requires a new *entry* hypothesis that
demonstrably lowers the stop-out rate below ~82%, validated on its own terms.

No robustness run with `--events-filter` was performed — that was pre-registered
as conditional on a Case 1 or Case 2 pass, which did not occur.

## Note on trade-count comparability

The two arms do not sample identically. `size_invalid` was 7,733 (dev control)
vs 557 (dev variant): as the control's equity decayed toward ₹22,568, its 20%
position fell below the ₹6,000 floor and entries were increasingly blocked, on
top of cash rejections at 100% exposure. The variant, holding capital better,
was starved less. Profit factor is a per-trade ratio and is unaffected, but the
trade counts are not a like-for-like exposure comparison.
