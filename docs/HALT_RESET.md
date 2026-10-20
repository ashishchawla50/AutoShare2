# Drawdown halt: reset procedure (DRAFT, 2026-10-19)

**Status: draft for owner decision. Nothing here is implemented.** Sections 1–2 describe
the code as it is today; sections 3–5 propose what to build and what to do.

---

## 1. What stops new entries today

| Gate | Fires when | Lifts when | Code |
|---|---|---|---|
| Emergency halt | drawdown from peak ≥ 20% (`MAX_DRAWDOWN_PCT`), or an operator creates the file | `data/emergency_halt.flag` is deleted **and** drawdown is below 20% | `DrawdownMonitor._check_drawdown` → `utils/health_check.set_emergency_halt` |
| Drawdown pause | drawdown from peak ≥ 15% (`DD_HALT_ENTRIES_AT`) | drawdown falls back below 15% | `DrawdownMonitor._check_drawdown` |
| Loss limits | realised P&L ≤ −3% today, ≤ −6% over 7 days, ≤ −10% over 30 days | the losing exits age out of the window | `DrawdownMonitor._check_daily/weekly/monthly_loss` |
| Regime | BEAR, EXTREME_VOLATILITY | regime upgrades (2-day confirmation) | `RegimeDetector` |

Halts block **entries only**. Exits, trailing stops, GTTs and the backup monitor keep running.

## 2. Why the drawdown gates are traps

**Drawdown = (peak − equity) / peak**, where equity = starting capital + realised + unrealised
P&L (`PortfolioManager.get_trading_equity`).

1. **With no open positions, equity cannot move.** When a gate fires, the open positions run
   to their stops and the book goes flat. After that, drawdown is frozen, so the gate can
   never lift by itself. This applies to the 15% pause as much as to the 20% halt.
   - Measured on 2026-10-19 (fast engine, ₹50k, live config with one parity rule switched off):
     the 15% pause fired on 2023-02-09 and blocked entries for **623 trading days**, which
     covered the whole 2023 rally. The full run went from PF 1.13 to 0.25.
   - 2021–22 dev runs came within about 0.2 pp of the 20% line.
2. **Deleting the flag does not reset the 20% halt.** `check_all_limits` checks the flag
   first. With the flag gone, `_check_drawdown` still sees drawdown ≥ 20%, writes the flag
   again and sends another alert at the next entry check. While drawdown is ≥ 15%, the 15%
   pause blocks entries anyway. Today there is **no working reset** short of editing the
   database.
3. **Live's peak never rises (bug, found 2026-10-19).** `PortfolioTracker.record_daily_metrics`
   stores `peak_value = MAX(peak_value)` of earlier rows, seeded at starting capital.
   `PortfolioManager.update_peak_value` exists but is never called. All 85 rows on the VPS
   read ₹50,000. Live therefore measures drawdown from **starting capital**, not from the
   high-water mark: after a run to ₹60k, a fall to ₹51k reads as 0%, not 15%. The backtest
   uses the true running peak, so it halts earlier than live would.
4. **The 10% half-size tier is dead.** `RiskManager.approve_entry` computes the multiplier
   but `check_entries` never applies it (`docs/LIVE_BACKTEST_PARITY.md` §2a).

## 3. Proposed mechanism (needs code; not built)

- **A peak rebase file:** `data/dd_peak_reset.json` = `{"date": ..., "equity": ..., "by": ..., "reason": ...}`.
  `get_peak_equity()` returns the higher of that equity and every snapshot recorded
  after that date. Writing the file is the only supported reset. It is logged and
  announced on Telegram.
- **Fix the peak bug (§2.3) first**, so the peak tracks the real high-water mark.
- **Keep the gates absorbing until a person acts.** An automatic timer would turn a risk
  control into a pause. The point of the gate is to force a review.

## 4. Proposed reset conditions for `emergency_halt.flag`

Reset only when **all** of these are true, and record each answer in the reset file's
`reason`:

1. **Cause identified.** Compare the losing trades against the backtest replay
   (`tools/compare_live_backtest.py`):
   - Every loss went through a GTT stop at the expected size, with no missed or duplicate
     orders and no data error.
   - If any loss came from a bug or an operational error: fix it, deploy it, restart, and
     confirm the restart time before resetting.
2. **The drawdown fits the strategy's known risk.** The October 2026 Monte Carlo gave about a
   28% chance of a 20% drawdown at ₹50k. A drawdown deeper than about 25%, or one reached in
   fewer trades than the backtest's worst sequence, means the live strategy is not the tested
   one. Do not reset in that case. Re-run the validation first.
3. **Not into a hostile regime.** The regime is not BEAR or EXTREME_VOLATILITY on the reset
   day. Entries would be blocked anyway, and a reset is not a bet on recovery.
4. **Capital decision made.** Rebase at current equity and keep the same capital (default).
   Topping up capital is a separate decision. It needs its own commit to `STARTING_CAPITAL`,
   and the rebase uses the new figure.
5. **Cooling-off period.** At least one full week passes between the halt firing and the reset.

Reset steps:
1. Write `data/dd_peak_reset.json`.
2. Delete `data/emergency_halt.flag`.
3. Check the next `trading.log` regime/entry lines and the Telegram alert.
4. Record the event in `WHOLE_STRATEGY.md` (changelog) and `CLAUDE.md`.

The 15% pause uses the same reset file and the same checks 1–3.

## 5. What the backtest does after a halt

| Mode | Use | Flag |
|---|---|---|
| Absorbing (no reset) | Worst-case operational outcome: what happens if nobody acts | default (`dd_halt=True`, `dd_pause` on) |
| No halt | Measuring the edge itself | `--no-dd-halt` (20%) and `--parity-off dd_pause` (15%) |
| Modelled reset | Realistic operations, once §3 is built: after the gate fires, wait N trading days, then rebase the peak to current equity | not built; N is the owner's choice (proposal: 5 trading days, matching §4.5) |

Decision-relevant results should report **both** the no-halt and absorbing runs, as
`CLAUDE.md` note 10 already requires. Add the modelled reset once it exists.

## 6. Decisions needed from the owner

1. Approve the rebase-file mechanism (§3) and the peak-bug fix (§2.3).
2. Approve or amend the reset conditions (§4), including the 25% "untested strategy" line.
3. Fix the dead 10% tier (apply it in sizing) or delete it.
4. Choose N for the modelled-reset backtest mode.
