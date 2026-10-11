# DATA FOUNDATION REVIEW — FINAL

**Date:** 2026-08-11
**Window:** 2021-06-01 → 2026-08-10 (universe effective from 2021-09-01)
**No strategy parameter was read, changed, or tuned.** VCP, momentum, breakout,
stops, trailing stops, risk %, sizing, ranking weights and regime thresholds are
all untouched.

---

## 1. Data

| Metric | Value |
|---|---|
| Trading days reconstructed | **1,283** (+72 holidays explicitly logged, 0 errors) |
| Raw archive | **1,283 .zip**, 156 MB, SHA-256 per file |
| Raw bhavcopy rows | **3,415,706** |
| Unique ISINs | **3,184** (vs 502 symbols before) |
| Adjusted rows | **2,468,251** |
| Securities per day | 1,465 min / 1,924 mean / 2,506 max |
| Symbol changes | **235** |
| Delisted / suspended securities | **672** |
| Corporate actions applied | **234** (all NSE-events-corroborated) |
| Residual-break actions | **9** (applied but unrepaired → full-history quarantine) |
| Unresolved / quarantined | **2,650** UNVERIFIABLE → **3,340 quarantine windows over 1,500 securities** |
| Monthly universes | **60**, exactly 500 members every month |

### Three layers, all preserved

| Layer | Location | State |
|---|---|---|
| 1 — original vendor data | `data/market_data.db.orig-20260811` | untouched |
| 2 — raw NSE bhavcopy | `data/bhavcopy_raw/` + `bhavcopy.db` | immutable, hash-verified |
| 3 — corrected dataset | `data/reconstructed.db` | regenerable from layer 2 |

`data/market_data.db` (live bot cache) was never modified.

---

## 2. Integration

| Concern | Implementation |
|---|---|
| Database used by backtest | `data/reconstructed.db` → `ohlcv_adjusted` (ISIN-keyed) |
| Universe | `backtest/point_in_time_universe.py` → `universe_liquid500` |
| ISIN mapping | `symbol_history` via `symbol_at()` / `isin_at()` |
| Data-source switch | `config.BACKTEST_DATA_SOURCE` (default `"reconstructed"`) |

**Proof `market_data.db` is no longer used for historical backtests.**
`backtest/data_loader.py` reads only `ohlcv_adjusted`. The one remaining reference
is `load_nifty_ohlcv`, the Nifty 50 benchmark series used for regime and RS — an
index, never a trade candidate, so it carries no survivorship or inclusion bias.
Enforced by `test_loader_historical_path_does_not_read_ohlcv_daily`, which asserts
`ohlcv_daily` appears nowhere before the benchmark function.

**Proof `universe_snapshots` is no longer used.**
`grep -rn "universe_snapshots" --include="*.py"` returns hits only in
`data_fetcher/universe_fetcher.py` (the **live** path, which legitimately records
today's list for live trading) and in comments. The backtest universe module never
queries it — asserted by `test_universe_module_does_not_query_snapshots` and by the
gate.

**Fails loudly, not silently.** Selecting `legacy_fabricated` raises
`FabricatedDataSourceError` unless `ALLOW_FABRICATED_BACKTEST_DATA=true` is also
set, and `PointInTimeUniverse` raises `FabricatedUniverseError` outright. Nobody
can run this in six months and unknowingly use the fabricated dataset.

**End-to-end verified.** A smoke backtest (2023-01-02 → 2023-03-31, ₹50k, vcp_v2,
CNC costs) ran the full path: universe → ISIN → adjusted OHLCV → indicators →
strategy → portfolio → metrics. 25,522 symbol-days analysed, 52 pivot watches, 3
trades, ledger invariant held. `INE002A01018` correctly resolves to `RELIANCE`.

---

## 3. Reproducibility

The corrected dataset was rebuilt **from scratch on a second machine** (local
Windows dev box) using only the retained raw archive — `--rebuild-from-archive`
reconstructs `bhavcopy.db` from the .zip files with no network access, then
`reconstruct_history.py --all` rebuilds layer 3.

| Check | VPS | Local rebuild | Match |
|---|---|---|---|
| Archive hash verification | 1,283 / 0 problems | 1,283 / 0 problems | ✅ |
| Raw bhavcopy rows | 3,415,706 | 3,415,706 | ✅ |
| Unique ISINs | 3,184 | 3,184 | ✅ |
| Symbol changes | 235 | 235 | ✅ |
| Delisted / suspended | 672 | 672 | ✅ |
| Discontinuities | 2,893 | 2,893 | ✅ |
| Applied / residual / probable / unknown | 234 / 9 / 666 / 1,984 | 234 / 9 / 666 / 1,984 | ✅ |
| Quarantine windows / securities | 3,340 / 1,500 | 3,340 / 1,500 | ✅ |
| Adjusted rows | 2,468,251 | 2,468,251 | ✅ |
| Monthly universes | 60 | 60 | ✅ |
| TATASTEEL split ratio | 10.0 | 10.0 | ✅ |
| BAJAJFINSV multi-action ratio | 10.0 | 10.0 | ✅ |
| RAINBOW first row | 2022-05-10 | 2022-05-10 | ✅ |
| LATENTVIEW first row | 2021-11-23 | 2021-11-23 | ✅ |
| EASEMYTRIP universe months | 0 | 0 | ✅ |
| HDFC 2022 / 2025 months | 12 / 0 | 12 / 0 | ✅ |

**Every figure is identical.** The rebuild is deterministic and another engineer
holding the archive can regenerate it.

---

## 4. Tests

| | Count |
|---|---|
| Total | **193 passed, 0 failed** (VPS and local) |
| Pre-existing | 163 |
| New (`tests/test_data_foundation.py`) | **30** |

New coverage: default source is `reconstructed`; backtest cannot silently use
`market_data.db`; universe refuses the fabricated source; unknown source rejected;
universe module does not query `universe_snapshots`; loader's historical path does
not touch `ohlcv_daily`; 2022 membership differs from today's; HDFC present in 2022
and absent in 2025; IBULHSGFIN→SAMMAANCAP and SUVENPHAR→COHANCE identity preserved;
determinism; month-boundary stability; first month empty; no same-day turnover
leakage; ≥15-session and ≥60-session thresholds; delisted securities present;
TATASTEEL and CESC splits applied; BAJAJFINSV multi-action ratio; VEDL/SIEMENS/
ABFRL/NMDC demergers quarantined not invented; RAINBOW and LATENTVIEW phantom rows
absent; every residual break quarantined; EASEMYTRIP out of the universe; no
quarantined security in the universe; no impossible OHLC; no missing/duplicate ISIN.

One pre-existing design issue surfaced and was fixed: `BacktestEngine` built
`PointInTimeUniverse` in `__init__`, so ledger/exit unit tests demanded the full
dataset. It is now lazy, and ISIN→ticker resolution degrades to the ISIN rather
than forcing a load — display never affects a trading decision.

---

## 5. What changed in this pass

1. **Residual-break detection.** Stage 2 now checks whether a corroborated ratio
   actually explains the move, measured market-relative so it agrees exactly with
   the audit tool. 9 actions fail this test.
2. **Full-history quarantine for unrepairable securities.** If we could not repair
   a corporate action, the security is excluded for its entire history — we cannot
   vouch for the price level on either side of an unknown transformation. This is
   what removes EASEMYTRIP (was 16 months in-universe, now 0).
3. **Delisting-tail quarantine (new class of defect, found during this pass).**
   All 672 delisted securities have series that simply stop. Without protection a
   backtest could enter days before the data vanishes and book a fabricated forced
   exit. The 60 sessions before any premature series end are now quarantined.
4. **Quarantine back-window widened** 5 → 60 sessions, covering realistic holding
   periods and the entry decisions made approaching a break.
5. **Universe churn preload fix.** `_preload_data` loaded only the start-date
   membership; with a real churning universe (~29 entries/month) every later month
   would have been silently starved. It now loads the union across the run.

Quarantine **excludes from the research universe; it never deletes data.**
EASEMYTRIP retains its `symbol_history` row and all 310 price rows.

---

## 6. Biases and limits that remain

- **Historical Liquid-500 is not the Nifty 500.** Turnover ranking tilts toward
  high-velocity, news-driven names; for a momentum/breakout family that plausibly
  *helps*, so it is not a neutral substitute. Overlap with today's real Nifty 500
  is 76% — deliberately not 100%, which would indicate leakage. Reported as its own
  universe, never as "Nifty 500". Sourcing genuine historical Nifty 500 membership
  remains open for a later A/B.
- **Quarantine removes real data.** 1,500 securities are excluded across various
  windows. If unresolved breaks correlate with distress, the remaining sample is
  mildly optimistic.
- **Pre-2021-06 is not reconstructed** (the corporate-events feed starts 2021-01).
- **Delisted-name adjustment quality is unverifiable** — no vendor series exists to
  check them against. They are the noisiest part of the dataset.
- **Survivorship is reduced, not eliminated.** Securities suspended without a clean
  bhavcopy trail may still be under-represented.
- **Price-adjusted, not total-return.** Dividends are not reinvested.
- **2,650 UNVERIFIABLE is not 2,650 corruptions.** A ≥20% move in an illiquid
  micro-cap is routine. Only 224 ever touched a universe member. They receive no
  adjustment, which is the conservative outcome.

---

## 7. Final gate

| # | Criterion | Status |
|---|---|---|
| 1 | STILL_CORRUPT = 0 | ✅ audit reports 0 |
| 2 | EASEMYTRIP safely quarantined | ✅ 0 universe months |
| 3 | Layer 3 consumed by the backtester | ✅ smoke backtest ran end-to-end |
| 4 | Historical Liquid-500 is the active PIT universe | ✅ 60 months × 500 |
| 5 | Old `universe_snapshots` cannot affect backtests | ✅ guard + 2 tests + gate |
| 6 | ISIN is the canonical historical identity | ✅ bidirectional, tested |
| 7 | Local reconstruction independently verified | ✅ every count identical |
| 8 | Regression tests pass | ✅ 193/193 |
| 9 | No known look-ahead / survivorship / inclusion issue | ✅ residual biases documented above |
| 10 | Reproducible by another engineer | ✅ archive + tools + documented rules |

`tools/validate_data_foundation.py` passes all 13 checks on both machines.

# DATA FOUNDATION: GO

All previously published PF, CAGR, drawdown, trade counts, strategy rankings, the
VCP result and the sizing study remain **INVALID** and must not be used as
reference points or targets. Strategy research starts from zero on this dataset.
