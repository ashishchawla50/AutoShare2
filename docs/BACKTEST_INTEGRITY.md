# BACKTEST_INTEGRITY.md

**Date:** 2026-08-11
**Scope:** Phase 2 — is the existing backtest trustworthy?
**Verdict:** **No.** One blocker voids every published number. Two more constrain
what can be fixed. Several parts of the engine are, however, genuinely well built.

Each item states what was checked, where, and the finding.

---

## Summary table

| Category | Verdict | Item |
|---|---|---|
| Survivorship / PIT universe | **FAIL — blocker** | BLOCKER-1 |
| Corporate actions (splits/bonuses) | **FAIL — blocker** | BLOCKER-2 |
| Point-in-time fundamentals | **FAIL — constrains Phase 3** | BLOCKER-3 |
| Look-ahead in indicators | PASS | §3 |
| Look-ahead in signal timing | PASS | §4 |
| Look-ahead in regime | PASS | §5 |
| Corporate-events filter timing | PASS (with caveat) | §6 |
| Execution: entry | PARTIAL | §7 |
| Execution: stops & gaps | PASS | §8 |
| Execution: slippage / impact / fills | **FAIL for scale testing** | §9 |
| Costs | PASS | §10 |
| Ledger integrity | PASS | §11 |
| Drawdown halt semantics | PARTIAL — absorbing | §12 |

---

## 1. BLOCKER-1 — Survivorship and inclusion bias

**Checked:** `backtest/point_in_time_universe.py:46`; every writer of
`universe_snapshots` (grep, whole repo).

**Finding:** The table has exactly two writers —
`data_fetcher/universe_fetcher.py:281` and `main.py:1167` — and both persist the
**current** NSE index composition. No historical membership data exists in the
system, and no code path can create it. `get_universe()` lines 86–112 additionally
collapse every pre-first-snapshot date onto a single cached fallback list.

A backtest run over 2021–2026 therefore trades one fixed list: the Nifty 500 as it
stood when the snapshot was taken.

**Two biases, not one:**

- *Survivorship* — companies that failed, delisted, or were demoted are absent.
  Their drawdowns never occur.
- *Inclusion / selection look-ahead* — every name in the list had already **earned**
  Nifty 500 membership by the snapshot date. A strategy that buys uptrend
  continuation is being handed a pool pre-filtered on the outcome it is trying to
  predict. For VCP, momentum and breakout alike, this is the more serious of the
  two.

**Impact:** PF 1.11 / 1.61 / 1.75 / 1.84 / 1.77, the sizing study, and the four
strategy verdicts are all void. Note the asymmetry: a strategy **rejected** under
a favourable bias is more convincingly dead than one that passed under it. The
vcp_v2 pass is the number at risk.

**What a fix requires:** a genuine historical constituent series. Options, in
descending order of quality: NSE's published index-reconstitution circulars
(semi-annual, archived); a commercial PIT constituent dataset; or a defensible
proxy such as a monthly top-N-by-turnover screen rebuilt from cached OHLCV, which
is imperfect but is *reconstructible from information available at the time* and
therefore honest. The proxy is the only one achievable with data already on hand.

---

## 2. BLOCKER-2 — Corporate-action adjustment

**Checked:** `data_fetcher/ohlcv_fetcher.py:101-200`; grep for any adjustment
logic across the repo.

**Finding:** None exists. `_fetch_from_kite` requests only the trailing `days`
window (default 500) and `_save_to_cache` uses `INSERT OR REPLACE`. Rows older
than that window are written once by the initial backfill and never revisited.

Two failure modes:

1. **Mixed-basis cache.** If the vendor re-serves history on a new adjustment
   basis after a split, rows inside the refetch window get the new basis and
   older rows keep the old one. The result is a permanent artificial price
   discontinuity frozen at the boundary. Nothing in the system detects it.
2. **Unadjusted actions.** A 1:5 split is a −80% single-bar move: a fabricated
   stop-out, a fabricated "prior uptrend ≥30%" (`vcp_detector.py:108`), and
   fabricated volatility contraction.

`utils/data_utils.flag_data_anomalies` and `validate_ohlcv` will not catch either
— a split-adjusted bar is internally consistent, and the volume check keys on
25× the 50-day median.

**Not yet measured:** how many splits/bonuses occurred in the traded names over
2021–2026, and whether any cached series actually shows a discontinuity. That is
a direct, cheap query against the VPS database and should be the first thing run.

---

## 3. Point-in-time fundamentals — BLOCKER-3

**Checked:** `data/fundamentals.db` schema; `fundamental_fetcher.get_fundamentals:157`.

**Finding:** the `fundamentals` table is keyed by symbol with **no as-of date** —
one current row per symbol, overwritten on refresh. `get_fundamentals` returns
today's market cap, D/E, pledge and ROE regardless of the date being simulated.

**There is no live leak today**, because the backtest never reads fundamentals.
The finding is recorded because it *constrains the Phase 3 parity work*: live's
`filter_market_cap`, `filter_fundamentals` and `filter_surveillance` (current
ASM/ESM list) **cannot** be ported into the backtest as-is. Doing so would trade
a known divergence for an unknown look-ahead bias — strictly worse.

The only clean resolutions are to source dated fundamental history, or to remove
these gates from the *live* engine so both sides run the same PIT-safe ruleset.

---

## 4. Look-ahead in indicators — PASS

**Checked:** `utils/indicator_utils.py` (all functions),
`strategy/vcp_detector.py`, `strategy/trend_template.py`.

- `swing_highs` / `swing_lows` (:95, :115): loop bound `range(left, len(high) - right)`
  means a swing is marked only after `right` confirming bars exist. No bar within
  `right` of the series end can be marked. Correct — and the `>=`/`>` asymmetry
  fixed in July 2026 is right.
- `ema`, `rsi`, `atr`: all causal (`ewm`, `shift(1)`). No centred windows.
- `_calculate_atr_compression` (:279): `tr.tail(10)` / `tr.tail(50)` — trailing only.
- `pivot_price = consolidation_df["high"].max()` (:181) — trailing 60-day window,
  no future bars.

No look-ahead found.

---

## 5. Look-ahead in signal timing — PASS

**Checked:** `backtest_engine._scan_for_entries:453-465`.

```python
sim_df       = df[df.index.date <= sim_date]
prev_day_df  = sim_df.iloc[:-1]      # signal computed on data through T-1
today_row    = sim_df.iloc[-1]       # entry at T's open
entry_price  = float(today_row["open"])
```

Signal on T−1's close, entry at T's open. This is correct and matches the live
cadence (evening scan → next-morning order). The vcp_v2 breakout confirmation
(:671) also reads `prev_day_df.iloc[-1]`. No leak.

---

## 6. Look-ahead in regime — PASS

**Checked:** `backtest/regime_sim.py:97`.

`raw_shifted = raw.shift(1)` assigns trading day T the classification derived from
T−1's close, matching the live 8:35 AM detection. The 2-day upgrade / immediate
downgrade hysteresis (:103-114) mirrors live.

**Documented and correct caveat** (module docstring): India VIX history is not
stored, so `EXTREME_VOLATILITY` and `HIGH_VOLATILITY` can never fire in the
backtest, and the BULL `vix_calm` term passes by default. The simulated gate is
therefore *more permissive* than live — results are an upper bound in that
dimension. This is the right direction for a caveat to point, and it is honestly
stated.

**Secondary issue:** breadth (:70-79) is computed over `ohlcv_by_symbol`, which is
the fabricated universe. So the regime signal inherits BLOCKER-1 — breadth
measured across known survivors overstates market health, particularly in 2022.

---

## 7. Corporate-events filter timing — PASS, with caveat

**Checked:** `_event_blocked:897`, `_preload_events:908`.

Events are matched by `ex_date` against the simulated date with symmetric
`RESULTS_BUFFER_DAYS` and forward-only `OTHER_EVENT_BUFFER_DAYS`. That is
PIT-correct: an ex-date is knowable in advance.

**Caveat:** results *dates* are announced only ~7 days ahead via board-meeting
notices. The ±7-day symmetric window around a results date therefore assumes
knowledge available roughly at the boundary of what was actually knowable. It is
close enough to defensible, but it is not risk-free, and `_preload_events` warns
correctly that days outside the coverage range are silently unfiltered.

---

## 8. Execution: entry — PARTIAL

Correct: entry at next open; entries deduct `pos_value + buy_cost` from cash
(:794); cash is tracked across the loop so intra-day sequencing is honest.

**Divergent from live** (see `LIVE_BACKTEST_PARITY.md`):
- Gap filter is missing live's `today_open > pivot × 1.05` rule (MAJOR-8).
- No ranking — first-in-list-order wins (MAJOR-5).
- No stock filters (MAJOR-4), no RS (MAJOR-6).

**Optimistic:** `profit_target` tests `close >= target` then fills at exactly
`target` (:339). Affects only the benched fixed-target strategies, not vcp_v2.

---

## 9. Execution: stops and gaps — PASS

`_process_exits:332`:

```python
if current_low <= pos["stop_loss"]:
    today_open = float(today_row["open"])
    fill = min(pos["stop_loss"], today_open) if today_open > 0 else pos["stop_loss"]
```

Intraday low triggers, and a gap-down fills at the open rather than at the stop
price. This is the honest treatment and it is explicitly commented as such. Good.

---

## 10. Execution: slippage, impact, fills — FAIL for scale testing

**Checked:** `portfolio/pnl_calculator.py:40`.

`slippage_pct = 0.001` — a flat 0.1% per side, applied as a fee. There is no:

- bid-ask spread model,
- market-impact term scaling with order size vs. ADV,
- partial-fill or failed-fill simulation,
- liquidity cap on position size.

At ₹50k this is acceptable. The **Phase 11 scale test (₹2L → ₹10L) cannot produce
a meaningful answer under this model**, because the single variable that should
degrade with size is a constant. Any "the strategy scales fine" conclusion would
be an artifact of the model, not a property of the strategy.

---

## 11. Costs — PASS

**Checked:** `portfolio/pnl_calculator.py:24-104`, `backtest/cost_model.py`.

`CNC_DELIVERY_COSTS` is correct for Zerodha equity delivery:

| Component | Value | Correct? |
|---|---|---|
| Brokerage | ₹0 | Yes — CNC delivery is free |
| STT buy / sell | 0.1% / 0.1% | Yes — delivery is charged both sides |
| Exchange txn | 0.00297% | Yes — post-Oct-2024 NSE rate |
| SEBI | 0.0001% | Yes |
| Stamp duty | 0.015% buy only | Yes |
| GST | 18% on brokerage + exchange | Yes |
| DP charge | ₹13.5 + GST per scrip per sell | Yes — CDSL |

The `legacy` default retains ₹20/side brokerage that CNC does not incur, and is
correctly documented as kept only so published numbers do not silently shift.
**`--cost-model cnc` must be used for any decision-relevant run** — this is already
project policy (`CLAUDE.md` note 11) and it is correct.

One nuance: slippage sits inside `TradeCosts` alongside genuine fees. It is an
assumption, not a fee, and separating the two would make sensitivity testing
cleaner.

---

## 12. Ledger integrity — PASS

`run():165-176` asserts `sum(realised_pnl) == capital - starting_capital` within
₹5. This is a real invariant that would catch an entire class of accounting bugs,
and it is the strongest single piece of engineering in the backtest layer.

---

## 13. Drawdown halt — PARTIAL

`_simulate_day:289` is **absorbing**: it blocks entries → equity cannot grow →
drawdown-from-peak never shrinks → the halt never lifts. This is documented in
the code, and `--no-dd-halt` exists to measure the underlying edge.

The handling is honest. The concern is that `CLAUDE.md` note 10 states **the same
trap exists in live**. A permanent halt is not a safety mechanism; it is an
un-recoverable failure state. Phase 6 contemplates higher risk levels, which makes
tripping it more likely, so this needs a designed recovery rule before risk is
raised.

---

## 14. What must happen before any number is believable

1. Build a genuine (or defensibly reconstructed) point-in-time universe.
2. Audit the price cache for split/bonus discontinuities; settle on one
   adjustment basis; add a regression test.
3. Re-run vcp_v2 on corrected data with `--cost-model cnc`, treating every
   existing figure as deleted.
4. Add a size-dependent slippage/impact model before any scale test.
5. Re-derive the *rejections* too — momentum, breakout and mean reversion were
   dismissed on the same void data, and they deserve a fair re-test under the
   corrected regime before Phase 4 concludes anything.

Until step 1 is done, no measurement taken from this engine should influence a
strategy decision.
