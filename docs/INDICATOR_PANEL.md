# INDICATOR_PANEL.md — Step 1

**Date:** 2026-08-12
**Status:** Panel built and validated. **Step 1 acceptance NOT met** — see §9.
**Scope:** infrastructure only. No strategy rule, threshold or parameter was changed.

---

## 1. Architecture

```
Layer 3 (data/reconstructed.db, ISIN-keyed, corporate-action corrected)
    └─ research/indicator_panel.py   one vectorised pass per ISIN
         └─ data/panel/<ISIN>.npz    float64, compressed, load-on-demand
              └─ research/fast_gate.py   O(1) lookups → trend-template pre-gate
                   └─ backtest_engine(use_panel=True)
```

The reference path (`utils/indicator_utils.py`, `strategy/*`) is **untouched and still
default** (`use_panel=False`). Nothing changes unless the flag is set.

| File | Role |
|---|---|
| `research/indicator_panel.py` | build / cache / load the panel (new) |
| `research/fast_gate.py` | panel-backed trend-template pre-gate (new) |
| `tests/test_indicator_panel.py` | 16 validation tests (new) |
| `backtest/backtest_engine.py` | `use_panel` flag + `_gate_tt` helper (modified) |

## 2. Contents — 50 columns, nothing invented

Every column corresponds to something the existing engine already computes.

| Group | Columns | Warm-up | Source |
|---|---|---|---|
| Raw | open, high, low, close, volume, turnover | 0 | Layer 3 |
| Return | ret_1d | 1 | — |
| EMA | ema_10/21/50/150/200, ema_200_20d_ago | period (+20) | `trend_template`, `stop_loss_manager`, momentum |
| RSI | rsi_2, rsi_14 | period+1 | `mean_reversion` |
| ATR | tr, atr_14, tr_mean_10, tr_mean_50, atr_compression_ratio | 15 / 50 | `vcp_detector._calculate_atr_compression` |
| Rolling extremes | close_hi/lo_20/60/252, high_hi/low_lo_20/60/252 | period | `trend_template` (close), VCP pivot (high/low) |
| Volume | vol_sma_20, vol_sma_50 | period | `breakout_detector.get_avg_volume_50d` |
| Momentum | mom_21/63/126/252 | period+1 | momentum family |
| Volatility | vol_ret_20 | 21 | general |
| VCP inputs | pre_base_low_min, consol_start_close | 60 | `vcp_detector` steps 1–2 |
| Weekly | wk_sma_30, wk_close | 30 weeks | `stage_analysis` (Weinstein) |
| Swings | is_swing_high/low, swing_high/low_conf | 5 | `vcp_detector._identify_contractions` |
| Meta | bars_available, segment_id, quarantined | — | — |

**Not precomputed:** VCP contraction pairing/dedup. It is path-dependent and
strategy-specific; the reference still performs it. The panel supplies its inputs.

## 3. ISIN identity

Keyed by ISIN, never ticker. `IBULHSGFIN → SAMMAANCAP` (INE148I01020) yields one
continuous series. Verified by `test_isin_identity_survives_ticker_change`.

## 4. Point-in-time safety

Every value at row T derives only from rows ≤ T. Enforced by three tests:

- `test_future_mutation_does_not_change_past` — multiply every OHLC after T by 7.3 and
  volume by 91; every value at or before T must be **bit-identical** (`atol=0, rtol=0`).
- `test_truncating_history_does_not_change_earlier_values` — computing on data through
  T equals computing through T+k for all rows ≤ T.
- `test_confirmed_swing_forms_are_causal`.

**The test has teeth — verified, not assumed.** Injecting a deliberate leak
(`close.shift(-1)`) into the panel makes it fail with
`AssertionError: LEAK_TEST changed when the FUTURE changed`; removing it passes again.

**Swing detection is the one non-causal primitive.** A swing high at index *i* needs
`right=2` future bars to confirm. Both forms are stored: `is_swing_high` at the bar it
describes (matching the reference, stable once its right window is past) and
`swing_high_conf` shifted forward by `right` (safe at a decision point). Consumers
deciding at T must use `_conf`, or restrict the raw form to indices ≤ T−2, which is
what the reference does implicitly by slicing.

## 5. Warm-up

NaN. Never 0, never a constant, never filled forward or backward. `ewm` emits from bar
0 by default, so EMA warm-up is explicitly masked to NaN for the first `period−1` bars.
Asserted by `test_warmup_is_nan_not_fabricated`.

## 6. Quarantine

An EMA spanning an unrepaired corporate action is meaningless, so with
`segment_on_quarantine=True` (research default) the series is split at every
quarantine window; each segment warms up independently and quarantined rows are NaN.
`False` reproduces reference semantics and exists so the fast/reference regression can
be run on identical semantics.

## 7. Storage and cache invalidation

`.npz` (float64, compressed), one file per ISIN, `data/panel/`. Chosen because
`pyarrow` is not installed (so Parquet/Feather are unavailable) and SQLite is slow for
bulk numeric reads. float32 was rejected — it would breach the 1e-9 tolerance.

**Measured:** 3,184 ISINs · 2,468,251 rows · 50 columns · **324 MB** · **526 s** build.

A bounded in-memory LRU (`_MEM_CAP = 400` panels) keeps RAM sane on the 956 MB VPS.
**This cap is currently too high — see §9.**

`cache_is_valid()` invalidates on any of: `PANEL_VERSION` change, `indicator_panel.py`
content hash change, Layer-3 fingerprint change (row count, max date, quarantine count,
applied-action count), or a `segment_on_quarantine` mismatch.

## 8. Validation results

### Fast vs reference indicators — 16/16 tests pass
Tolerance: floats `rtol=atol=1e-9`; booleans/events **exact**. EMA, RSI, ATR, rolling
extremes, volume SMA, ATR-compression ratio (incl. its `round(3)`), weekly MA and swing
flags all match the reference implementations.

### Trade-level regression — PASSES exactly
2023-01-02 → 2023-03-31, ₹50k, `vcp_v2`, CNC costs, reconstructed dataset.

| | Reference | Fast | |
|---|---|---|---|
| Trades | 3 | 3 | ✅ identical |
| Trade list (ISIN, dates, prices, shares, P&L, reason) | — | — | ✅ **byte-identical** |
| Final capital | ₹49,255.590597 | ₹49,255.590597 | ✅ diff 0.000000 |
| Ledger invariant | held | held | ✅ |
| `pivot_watch_added` / `expired` | 52 / 30 | 52 / 30 | ✅ |
| `high_conf` / `entered` | 3 / 3 | 3 / 3 | ✅ |
| **`fail_tt`** | **19,008** | **19,011** | ❌ **+3** |
| **`fail_vcp`** | **5,624** | **5,621** | ❌ **−3** |

### Performance — measured, not estimated

| | Reference | Fast | |
|---|---|---|---|
| Runtime (60 trading days) | 970.2 s | 485.8 s | **2.00×** |
| Peak RSS | 127 MB | 400 MB | 3.1× worse |
| Extrapolated full 1,283-day run | ~5.8 h | ~2.9 h | — |
| Gate outcomes | — | 17,655 reject / 5,317 pass / 1,727 abstain | — |

**Target was ≥50×. Achieved 2.00×.**

## 9. Why Step 1 does not pass

Three open items.

**(a) 2.00× against a 50× target.** The panel eliminates indicator recomputation for
the 69% of symbol-days the gate rejects, but runtime only halved — so indicator maths
was *not* the dominant cost. The remaining bottleneck is the engine's inner loop:
`sim_df = df[df.index.date <= sim_date]` and `prev_day_df = sim_df.iloc[:-1]` execute a
full boolean mask and DataFrame copy **per symbol per day** — roughly 1,500 symbols ×
60 days × ~700 rows ≈ 63 M row comparisons plus ~90 k DataFrame allocations, before any
indicator is touched. Removing that requires integer-indexed numpy views in place of
pandas slicing, which is the Step 2 canonical-engine restructure. **The panel is a
prerequisite for the 50×, not sufficient for it.**

**(b) 3 unexplained symbol-day reclassifications.** Trades, ledger and final balance are
identical, and the shift is between two rejection buckets (both discard the candidate),
so it cannot alter a trade in this window. But it means the gate and the reference
disagree on 3 of 25,522 evaluations (0.012%) and **I have not established why.** The
leading hypothesis is quarantine segmentation changing EMA seeding for a security
quarantined earlier in its history but clean during the window. A 1,529-point spot
check found 0 mismatches — a 6 % sample, far too underpowered to confirm or refute a
0.012 % effect. The decisive test is a full rebuild with `--no-segment` followed by a
re-run; the delta should go to zero. Not yet run. Per the Step-1 rules, an unexplained
discrepancy means stop.

**(c) Peak memory tripled**, 127 MB → 400 MB, on a 956 MB box. `_MEM_CAP = 400` panels
is too generous; it should be cut (~100) and measured again.

## 10. Known limitations

- EMA seeding: the panel uses each ISIN's full history; the reference uses the engine's
  loaded window. They coincide when the warm-up start precedes the dataset start (true
  for the 2023 regression window). Elsewhere the panel is canonical and better-warmed —
  documented rather than silently divergent. `fast_gate` abstains rather than guess.
- VCP contraction pairing is still reference-computed.
- The gate covers only the trend template. Extending it to the VCP prior-uptrend gate
  is possible and would fall in the same rejection bucket, but was not attempted.
- Panel build is single-threaded (526 s); the VPS has 2 cores.
