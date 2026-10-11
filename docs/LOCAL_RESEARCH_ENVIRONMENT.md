# LOCAL_RESEARCH_ENVIRONMENT.md

**Date:** 2026-08-12
**Purpose:** Phase A audit of the local machine as the intended research host.
**Status:** Environment audited and data foundation verified locally. **One premise of
the migration does not hold — see §1.**

---

## 0. Headline

**The local machine is slower than the VPS for this workload, not faster.**

Measured on the identical job (full indicator-panel build, 3,184 ISINs):

| | VPS (Oracle Cloud) | Local (i5-7200U) |
|---|---|---|
| Panel build | **526 s** | **~890 s** (2,250/3,184 in 630 s) |

That is roughly **1.7× slower locally.** The local CPU has the *same physical core
count* as the VPS (2), is a 2016 mobile part, is thermally constrained, and is
currently sharing the machine with Chrome, VS Code and Defender.

Moving research to this machine therefore does **not** by itself fix the 5–6 hour
research cycle. The only thing that fixes that is the Step 2 algorithmic restructure
(removing repeated pandas slicing). The migration is still worth doing — for RAM
headroom, no SSH round-trips, faster edit-test loops, and freeing the VPS for its
live role — but it should not be expected to deliver a speedup on its own.

---

## 1. Machine specification (measured, not assumed)

| Item | Value |
|---|---|
| OS | Microsoft Windows 11 Pro N 64-bit, build 26200 |
| CPU | Intel Core i5-7200U @ 2.50 GHz (max 2.71 GHz) |
| Cores | **2 physical / 4 logical** |
| RAM total | 7.9 GB |
| RAM available | **1.1 GB (85.6 % in use)** at audit time |
| Committed | 18.4 GB of a 20.3 GB limit (heavy pagefile use) |
| Disk C: | 930.5 GB total, **614.7 GB free** |
| Other volumes | D: 184.8 GB free · E: 198.8 GB free · F: 73.6 GB · G: 116.9 GB free |

**RAM is the binding constraint, not disk.** No single process is at fault — the
7.9 GB is consumed by the ordinary dev environment: Chrome (~1.2 GB across tabs),
VS Code (~1.0 GB across windows), Claude (234 MB), Defender (209 MB).

Closing Chrome and surplus VS Code windows should free ~2–3 GB. With those closed the
machine comfortably runs the ~420 MB research process plus 2 workers; at the current
1.1 GB available it can run **one** research process and nothing else.

## 2. Python environment

| Item | Value |
|---|---|
| Python | 3.12.10 (64-bit, MSC v.1943) — matches the project requirement |
| SQLite | 3.49.1 (newer than the VPS; read-compatible) |
| numpy | 1.26.4 ✅ matches requirements.txt |
| pandas | 2.2.0 ✅ |
| scipy | 1.12.0 ✅ |
| SQLAlchemy | 2.0.27 ✅ |
| pytest | 8.0.2 ✅ |
| requests | 2.31.0 ✅ |
| kiteconnect | 5.0.1 ✅ |
| psutil | 5.9.8 (available — useful for memory profiling) |
| **Absent** | pyarrow, numba, joblib, matplotlib |

`pyarrow` absent is why the indicator panel uses `.npz` rather than Parquet — that
decision remains correct here. `joblib`/`multiprocessing` are not yet needed; the
stdlib `concurrent.futures` is sufficient for the parallelism plausible on 2 cores.

## 3. Repository

- Location: `C:\Users\Ashish\Documents\Claude\AutoShare` (`/c/Users/...` under Git Bash)
- Git HEAD: `b218e83`
- Test suite: **211 passed, 1 skipped** locally
- Both Bash (Git Bash) and PowerShell are available; the tooling runs under both.

## 4. Local data state — Phase B is already complete

The full three-layer dataset was rebuilt locally **from scratch** during the earlier
reproducibility work, and every semantic count matched the VPS exactly.

| Layer | Local path | Size | State |
|---|---|---|---|
| 2 — raw NSE archive | `data_local/bhavcopy_raw/` | 156 MB, **1,283 .zip** | SHA-256 verified, 0 problems |
| 2 — parsed | `data_local/bhavcopy.db` | 721 MB | rebuilt offline from the archive |
| 3 — corrected | `data_local/reconstructed.db` | 421 MB | **rebuilt from scratch locally** |
| 3 — test copy | `data/reconstructed.db` | 421 MB | used by the test suite |
| Panel | `data/panel/` | ~324 MB when complete | rebuilding locally now |

**Local `tools/validate_data_foundation.py --root data_local` → 13/13 PASS →
`DATA FOUNDATION: GO`.**

Verified identical to the VPS: 3,184 ISINs · 2,468,251 adjusted rows · 235 symbol
changes · 672 delisted/suspended · 2,893 discontinuities (234 applied / 9 residual /
666 probable / 1,984 unknown) · 3,340 quarantine windows over 1,500 securities ·
60 monthly universes · EASEMYTRIP 0 universe months · HDFC 12 months in 2022 and 0 in
2025 · TATASTEEL split 10.0 · BAJAJFINSV multi-action 10.0 · RAINBOW first row
2022-05-10 · LATENTVIEW first row 2021-11-23.

## 5. Storage requirement

| Component | Size |
|---|---|
| Raw NSE archive (layer 2, immutable) | 156 MB |
| `bhavcopy.db` (parsed layer 2) | 721 MB |
| `reconstructed.db` (layer 3) | 421 MB |
| Indicator panel cache | ~324 MB |
| **Subtotal** | **~1.6 GB** |
| Research results, run configs, logs | allow 2–5 GB |
| **Recommended free space** | **~10 GB** |

Against 614.7 GB free on C:, storage is a non-issue.

## 6. Expected research capacity and parallelism

Given 2 physical cores and constrained RAM:

- **Recommended workers: 2** (one per physical core), not 4. Hyperthreading adds
  little for numeric/pandas work, and each worker needs its own panel cache.
- Per-worker memory budget: ~420 MB observed for a full run. Two workers ≈ 840 MB,
  which needs Chrome/VS Code closed at the current 1.1 GB available.
- Workers must be **process-level and read-only** against `reconstructed.db`
  (SQLite read concurrency is fine; writes are not). Each run writes its own result
  file — no shared mutable state.
- Do not scale workers past physical cores expecting linear gains; this CPU will
  thermally throttle before it saturates.

**Realistic expectation:** parallelism buys ~2× at best. The 50× target must come from
the Step 2 algorithmic work, not from this hardware.

## 7. Known issues found during the audit

1. **`indicator_panel.build_all(--root X)` ignores `--root` for its output.** The
   dataset is read from `--root`, but the panel is written to
   `Path(config.DB_PATH)/"panel"` via `_panel_dir()`. Building with
   `--root data_local` wrote to `data/panel`. Harmless today (both are the same
   local dataset) but it must be fixed before research and live configs diverge.
2. **RAM headroom is the real constraint.** 1.1 GB available makes multi-worker
   research impossible without closing applications first.
3. **No `pyarrow`.** Keeps `.npz` as the panel format — fine, but worth installing if
   Parquet becomes useful for result storage.

## 8. Research vs live separation

| | Research (local) | Live/paper (VPS) |
|---|---|---|
| Data | `reconstructed.db`, panel cache, raw archive | `market_data.db`, live Kite feed |
| Source flag | `BACKTEST_DATA_SOURCE=reconstructed` (default) | not used |
| Writes | `research_results/` | `portfolio_state.db`, logs, Telegram |
| Secrets | none required | `.env` on the VPS only |

The fabricated legacy source stays blocked in both: selecting it raises
`FabricatedDataSourceError` / `FabricatedUniverseError` unless
`ALLOW_FABRICATED_BACKTEST_DATA=true` is explicitly set. No credentials were copied to
the local machine, and none belong in the repository.
