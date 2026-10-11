# LIVE_BACKTEST_PARITY.md

**Date:** 2026-08-11
**Scope:** Phase 3 — rule-by-rule comparison of the two implementations.
**Status:** Comparison only. No code unified yet.

This supersedes `DIVERGENCE_INVENTORY.md`, which is stale in two places (A1 and
B1 are both closed — see §4).

---

## 1. The structural problem

There is no canonical strategy engine. `vcp_v2` exists twice:

- **Live:** `main.py` — `run_pre_scan` (:205), `run_strategy_scan` (:231),
  `rank_opportunities` (:505), `check_entries` (:599), `check_exits` (:891).
- **Backtest:** `backtest/backtest_engine.py` — `_scan_for_entries` (:421),
  `_process_exits` (:310).

Shared code is limited to leaf-level primitives: `strategy/vcp_detector.py`,
`strategy/trend_template.py`, `strategy/multi_timeframe.py`,
`strategy/breakout_detector.py`, `risk/position_sizer.py`,
`exit/stop_loss_manager.py`, `exit/profit_manager.py`.

**Everything that composes those primitives into a decision — filtering, ranking,
slot allocation, gap handling, regime exits — is written twice.** That is precisely
where all eight divergences below live, and it is why they keep reappearing.
Comment-level instructions to "keep the two in sync" (`main.py:238`) are not a
mechanism.

---

## 2. Full rule comparison

Legend: **MATCH** identical · **DIVERGE** materially different · **N/A** absent one side

| # | Rule | Live | Backtest | Verdict |
|---|---|---|---|---|
| 1 | Universe source | NSE live Nifty 500, refreshed daily | `universe_snapshots` (fabricated, BLOCKER-1) | **DIVERGE — critical** |
| 2 | Stock filters (9) | `run_batch_filters` (main.py:226) | none — `apply_all_filters` imported, never called | **DIVERGE (MAJOR-4)** |
| 3 | Trend template | `analyse_stock` | `analyse_stock` | MATCH |
| 4 | VCP detection | `detect_vcp` | `detect_vcp` | MATCH |
| 5 | Tightness gate (≥50) | `multi_timeframe` | `multi_timeframe` | MATCH |
| 6 | Pivot watch add | `pivot_watch.add_symbol`, JSON-persisted | `self._pending_pivots` dict | MATCH (logic) |
| 7 | Watch window | 30d (`VCP2_PIVOT_WATCH_DAYS`) | 30d | MATCH |
| 8 | Breakout confirm | `is_valid_breakout` on last candle | `is_valid_breakout` on `prev_day_df[-1]` | MATCH |
| 9 | Relative strength | 35% of composite score | **absent entirely** | **DIVERGE (MAJOR-6)** |
| 10 | Ranking | `rank_setups` by composite | none — universe iteration order | **DIVERGE (MAJOR-5)** |
| 11 | Regime gate (entry) | `regime_detector`, VIX-aware | `regime_sim`, VIX-less | DIVERGE (documented, backtest more permissive) |
| 12 | Gap filter | 3 rules incl. `open > pivot×1.05` | 2 rules | **DIVERGE (MAJOR-8)** |
| 13 | Position sizing | `calculate_position_size` | `calculate_position_size` | MATCH |
| 14 | Risk %, position cap | 2.0% / 20% | 2.0% / 20% | MATCH |
| 15 | Dust floor | ₹6,000 × regime mult | ₹6,000 × regime mult | MATCH |
| 16 | Cash reserve | 20% | 20% | MATCH |
| 17 | Sector cap 30% | `risk_manager.approve_entry` | **absent** | **DIVERGE** |
| 18 | Duplicate-holding check | `approve_entry` | `if symbol in self._open_positions` | MATCH (effect) |
| 19 | Max positions | 8 pool (effectively 4, MAJOR-10) | 8 pool (effectively 4) | MATCH |
| 20 | Manual blocklist | enforced twice | **absent** | DIVERGE (live-only, intentional) |
| 21 | Hard stop 7% | `calculate_stop_loss_price` | `calculate_stop_loss_price` | MATCH |
| 22 | Trailing ratchet | `calculate_trailing_stop` | `calculate_trailing_stop` | MATCH |
| 23 | EMA10×0.97 floor | yes | yes | MATCH |
| 24 | Stop trigger basis | daily **close** (+ GTT intraday) | intraday **low**, fill `min(stop, open)` | DIVERGE in paper only — see §3 |
| 25 | Technical breakdown | `check_technical_breakdown` | `check_technical_breakdown` | MATCH |
| 26 | Time stop 15d/7% | `should_exit_on_time_stop` | `should_exit_on_time_stop` | MATCH |
| 27 | **Regime exit** | `check_regime_exit` (main.py:989) | **absent** | **DIVERGE (MAJOR-7)** |
| 28 | Partial profit | gated off (`ENABLE_PARTIAL_PROFIT=False`) | never called | MATCH (both off) |
| 29 | Drawdown halt | `drawdown_monitor`, absorbing | absorbing, `--no-dd-halt` available | MATCH (both trapped) |
| 30 | Daily/weekly/monthly loss limits | `check_all_limits` (main.py:608) | **absent** | **DIVERGE** |
| 31 | Consecutive-loss pause | config exists, live-enforced | **absent** | DIVERGE |
| 32 | Costs | real broker charges | `--cost-model cnc` | MATCH when flag used |
| 33 | Entry price basis | LTP at 09:30–10:30 | today's **open** (09:15) | **DIVERGE (minor, real)** |
| 34 | Fill price recorded | broker average fill | assumed exact | DIVERGE (unavoidable) |

**Count: 8 material divergences** (#1, 2, 9, 10, 12, 17, 27, 30), plus #33 and the
paper-mode issues in §3.

---

## 3. Paper mode is a third, more optimistic system

Not previously catalogued anywhere. `main.py:633-635`:

```python
ltp_data   = self._kite.get_ltp([...]) if not config.PAPER_TRADING_MODE else {}
today_open = ltp_data.get(f"NSE:{setup.symbol}", float(df["close"].iloc[-1]))
prev_close = float(df["close"].iloc[-1])
```

In paper mode `today_open` falls back to `prev_close`, therefore:

- `gap_pct` is **always exactly 0** — the gap filter (#12) can never fire.
- Entries fill at **yesterday's close**, not today's open. Overnight gap risk is
  completely absent.

Separately, paper has no GTT, so stops are only evaluated against the **daily
close** (`check_exits`, main.py:912/919). Live is protected intraday by the GTT
and `GTTBackupMonitor`; the backtest triggers on `current_low`. A position that
trades through its stop intraday but closes above it survives in paper and is
stopped out in both live and backtest.

Both effects bias the same way:

> **Paper > Live and Paper > Backtest, systematically.**

Given the plan is to paper-trade a new strategy before committing capital, paper
mode is currently unfit to serve as evidence. It should be fixed before it is used
to judge anything.

---

## 4. Corrections to `DIVERGENCE_INVENTORY.md`

| Item | Documented status | Actual status |
|---|---|---|
| A1 — live-only RS ≥ 70 gate | OPEN | **CLOSED** 2026-08-10 (`opportunity_ranker.py:15-20`). The gate is gone; RS remains a 35% score weight, which is divergence #9 above — a different and smaller problem. |
| B1 — live books partial profits | OPEN | **CLOSED** by `ENABLE_PARTIAL_PROFIT = False` (`config.py:80`); gated at `main.py:999`. Both engines now ride to the trailing stop. |

`CLAUDE.md` repeats both stale entries and should be corrected in the same commit
as any parity work.

---

## 5. Target architecture

The stated goal — one canonical decision engine, two executors — is right. Proposed
shape:

```
strategy/engine.py                     # NEW — the only place decisions are made
    decide_entries(market_state) -> list[Decision]
    decide_exits(positions, market_state) -> list[ExitDecision]

market_state: universe, OHLCV-through-T-1, regime, open positions,
              portfolio value, available cash
              — a plain data structure, no I/O, no clock, no broker
```

- **Live executor** (`main.py`) builds `market_state` from the cache + Kite, calls
  the engine, translates `Decision` into orders + GTTs.
- **Backtest executor** builds `market_state` from SQLite, calls the same engine,
  translates `Decision` into simulated fills.

Rules for the engine, to stop drift recurring:

1. It performs **no I/O** and reads **no clock**. Everything arrives via
   `market_state`. This makes it directly unit-testable and makes divergence
   structurally impossible rather than a matter of discipline.
2. **No non-PIT input may enter it** (BLOCKER-3). Current market cap, D/E, pledge,
   ROE and the ASM/ESM list are not point-in-time and must not become engine
   inputs. Either source dated history, or drop those gates from live so both
   sides run the same PIT-safe ruleset. Porting them into the backtest as-is
   would trade a known divergence for an unknown look-ahead bias — strictly worse.
3. A parity test asserts that engine decisions on identical `market_state` are
   byte-identical regardless of executor, and it runs in CI-equivalent (`pytest`).

---

## 6. Resolution plan for each divergence

Ordered by whether the *backtest* or *live* should move.

**Backtest must adopt live's behaviour** (live is right, backtest is optimistic):

- #10 ranking — real portfolio construction requires choosing the best signal.
- #12 gap filter — add the missing `open > pivot × 1.05` rule.
- #27 regime exit — the backtest currently rides through deteriorations live exits.
- #17 sector cap, #30 loss limits — these bind in exactly the adverse sequences
  Phase 6 and 9 are meant to stress-test; omitting them overstates survivability.

**Live must adopt the backtest's behaviour** (backtest is right, live is
unvalidated):

- #2 stock filters — every filter live applies beyond the validated ruleset makes
  live strictly different from what was measured. Each should be justified by a
  backtest variant that passes both windows, or removed. This is the same standard
  already applied to the ROE gate.

**Needs a decision, not a port:**

- #9 relative strength — must be added to the backtest (PIT-safe: it is computable
  from cached OHLCV) *or* removed from live's scoring. Currently it silently
  decides which trade is taken in live and is invisible in every published number.
- #1 universe — BLOCKER-1; not a parity fix, a data-sourcing problem.
- #33 entry price basis — LTP-at-09:30 vs open-at-09:15 cannot be reconciled
  exactly. Options: move live's entry to a market-on-open order, or model the
  09:30 price in the backtest. The first is simpler and reduces variance.

**Accept and document:**

- #11 VIX-less regime — backtest is more permissive; already documented, direction
  of the caveat is safe.
- #20 blocklist, #34 fill price — live-only by necessity.

---

## 7. Sequencing

Parity work should **not** start before BLOCKER-1 and BLOCKER-2 are resolved.
Unifying two engines that both run on fabricated data produces one engine that
runs on fabricated data — the divergences would be closed, and the numbers would
be exactly as void as they are now. Fix the data, re-measure, then unify against a
ruleset that has actually been shown to work.
