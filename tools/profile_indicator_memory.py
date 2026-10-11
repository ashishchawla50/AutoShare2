"""Reusable memory diagnostics for the indicator panel (Step 1).

Answers, with measurements rather than assertions:
  1. scaling   does RSS stabilise as more ISINs are processed, or keep climbing?
  2. tracked   is the memory Python-tracked (tracemalloc) or native (RSS >> traced)?
  3. storage   does compressed .npz decompression drive the high-water?
  4. rebuild   how much is DataFrame reconstruction vs the raw arrays?
  5. retention does eviction actually release memory?
  6. gc        does an explicit collect change anything material?

Usage:
    python tools/profile_indicator_memory.py --all
    python tools/profile_indicator_memory.py --scaling --counts 100,500,1000,1500
    python tools/profile_indicator_memory.py --storage --n 60
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.config import config


def rss_mb() -> float:
    """Resident set size in MB. /proc on Linux, psutil-free."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except Exception:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def traced_mb() -> tuple[float, float]:
    cur, peak = tracemalloc.get_traced_memory()
    return cur / 1024 / 1024, peak / 1024 / 1024


def _panel_files() -> list[Path]:
    d = Path(config.DB_PATH) / "panel"
    return sorted(p for p in d.glob("*.npz"))


# ── 1. scaling: bounded or unbounded? ────────────────────────────────────────

def scaling(counts: list[int]) -> list[dict]:
    """Load N panels through the real cache and record RSS at each checkpoint.

    The decisive test: if RSS keeps climbing with N after the cache budget is
    saturated, something retains evicted panels. If it plateaus, the cache is doing
    its job and the residual is allocator/native high-water.
    """
    import research.indicator_panel as ip
    files = _panel_files()
    out = []
    print(f"\n=== 1. SCALING ({len(files)} panels available) ===")
    print(f"{'N':>6} {'rss_mb':>9} {'traced_cur':>11} {'traced_pk':>10} "
          f"{'cache_mb':>9} {'cached':>7} {'load_s':>8}")
    ip.clear_memory_cache(); gc.collect()
    tracemalloc.start()
    base = rss_mb()
    print(f"{'base':>6} {base:>9.1f}")
    done = 0
    t0 = time.time()
    for target in counts:
        while done < min(target, len(files)):
            ip.load_panel(files[done].stem)
            done += 1
        st = ip.cache_stats()
        c, p = traced_mb()
        row = {"n": done, "rss_mb": round(rss_mb(), 1), "traced_cur_mb": round(c, 1),
               "traced_peak_mb": round(p, 1), "cache_mb": st["cache_mb"],
               "cached_isins": st["cached_isins"], "elapsed_s": round(time.time() - t0, 1)}
        out.append(row)
        print(f"{row['n']:>6} {row['rss_mb']:>9.1f} {row['traced_cur_mb']:>11.1f} "
              f"{row['traced_peak_mb']:>10.1f} {row['cache_mb']:>9.1f} "
              f"{row['cached_isins']:>7} {row['elapsed_s']:>8.1f}")
    # retention check
    ip.clear_memory_cache()
    gc.collect()
    after = rss_mb()
    c, p = traced_mb()
    print(f"\n  after clear_memory_cache + gc.collect():")
    print(f"    rss {after:.1f} MB   traced_cur {c:.1f} MB   traced_peak {p:.1f} MB")
    print(f"    rss released: {out[-1]['rss_mb'] - after:.1f} MB")
    print(f"    rss above baseline still held: {after - base:.1f} MB")
    tracemalloc.stop()
    return out + [{"n": "cleared", "rss_mb": round(after, 1),
                   "traced_cur_mb": round(c, 1), "baseline_mb": round(base, 1)}]


# ── 2/3. storage format experiment ───────────────────────────────────────────

def storage(n: int) -> list[dict]:
    """Compare .npz(compressed) / .npz(uncompressed) / .npy / mmap .npy.

    Experiment only — nothing in the production dataset is converted.
    """
    import tempfile
    files = _panel_files()[:n]
    print(f"\n=== 2+3. STORAGE FORMAT ({len(files)} panels) ===")
    tmp = Path(tempfile.mkdtemp(prefix="panelfmt_"))
    # materialise each format once
    for f in files:
        z = np.load(f, allow_pickle=True)
        np.savez(tmp / f"{f.stem}.unc.npz", index=z["index"],
                 columns=z["columns"], values=z["values"])
        np.save(tmp / f"{f.stem}.values.npy", z["values"])
        np.save(tmp / f"{f.stem}.index.npy", z["index"])
        del z
    gc.collect()

    results = []

    def bench(label, fn):
        gc.collect()
        tracemalloc.start()
        r0 = rss_mb()
        t0 = time.time()
        checksum = 0.0
        for f in files:
            arr = fn(f)
            # sum the whole array: fully-quarantined ISINs emit a 3-column
            # frame, so a fixed column index is not safe here
            checksum += float(np.nansum(np.asarray(arr, dtype=np.float64)))
            del arr
        dt = time.time() - t0
        r1 = rss_mb()
        c, pk = traced_mb()
        tracemalloc.stop()
        gc.collect()
        r2 = rss_mb()
        row = {"format": label, "load_s": round(dt, 2), "rss_before": round(r0, 1),
               "rss_after": round(r1, 1), "rss_after_gc": round(r2, 1),
               "rss_delta": round(r1 - r0, 1), "traced_peak_mb": round(pk, 1),
               "checksum": round(checksum, 3)}
        results.append(row)
        print(f"  {label:<24} load {dt:>6.2f}s  rss {r0:>6.1f}->{r1:>6.1f} "
              f"(gc {r2:>6.1f})  delta {r1-r0:>6.1f}  traced_pk {pk:>6.1f}")

    bench("npz compressed (prod)", lambda f: np.load(f, allow_pickle=True)["values"])
    bench("npz uncompressed", lambda f: np.load(tmp / f"{f.stem}.unc.npz",
                                                allow_pickle=True)["values"])
    bench("npy", lambda f: np.load(tmp / f"{f.stem}.values.npy"))
    bench("npy mmap", lambda f: np.load(tmp / f"{f.stem}.values.npy", mmap_mode="r"))

    cs = {r["checksum"] for r in results}
    print(f"  checksums identical across formats: {len(cs) == 1}  {cs}")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return results


# ── 4. DataFrame reconstruction cost ─────────────────────────────────────────

def rebuild(n: int) -> list[dict]:
    files = _panel_files()[:n]
    print(f"\n=== 4. DATAFRAME RECONSTRUCTION ({len(files)} panels) ===")
    rows = []
    for f in files:
        gc.collect()
        tracemalloc.start()
        z = np.load(f, allow_pickle=True)
        vals, idx, cols = z["values"], z["index"], z["columns"]
        arr_bytes = vals.nbytes + idx.nbytes
        _, pk_arr = traced_mb()
        df = pd.DataFrame(vals, columns=list(cols), index=pd.to_datetime(idx))
        deep = int(df.memory_usage(index=True, deep=True).sum())
        shallow = int(df.memory_usage(index=True, deep=False).sum())
        _, pk_df = traced_mb()
        tracemalloc.stop()
        rows.append({
            "isin": f.stem, "file_kb": round(f.stat().st_size / 1024, 1),
            "arrays_kb": round(arr_bytes / 1024, 1),
            "df_shallow_kb": round(shallow / 1024, 1),
            "df_deep_kb": round(deep / 1024, 1),
            "traced_peak_arrays_kb": round(pk_arr * 1024, 1),
            "traced_peak_after_df_kb": round(pk_df * 1024, 1),
            "rows": len(df), "cols": len(df.columns)})
        del df, vals, idx, cols, z
    agg = {k: round(float(np.mean([r[k] for r in rows])), 1)
           for k in ("file_kb", "arrays_kb", "df_shallow_kb", "df_deep_kb",
                     "traced_peak_after_df_kb")}
    print(f"  mean per panel: file {agg['file_kb']} KB -> arrays {agg['arrays_kb']} KB "
          f"-> DataFrame {agg['df_shallow_kb']} KB (deep {agg['df_deep_kb']} KB)")
    print(f"  mean traced peak during rebuild: {agg['traced_peak_after_df_kb']} KB")
    print(f"  DataFrame overhead vs raw arrays: "
          f"{agg['df_shallow_kb'] / max(agg['arrays_kb'], 1e-9):.2f}x")
    return rows + [{"MEAN": agg}]


# ── 5. eviction / retention ──────────────────────────────────────────────────

def retention(n: int) -> dict:
    """Does eviction actually release memory, or does something still hold it?"""
    import weakref
    import research.indicator_panel as ip
    files = _panel_files()[:n]
    print(f"\n=== 5. RETENTION / EVICTION ({len(files)} panels) ===")
    ip.clear_memory_cache(); gc.collect()
    r0 = rss_mb()
    refs = []
    for f in files:
        d = ip.load_panel(f.stem)
        if d is not None:
            refs.append((f.stem, weakref.ref(d)))
    r1 = rss_mb()
    st_loaded = ip.cache_stats()
    ip.clear_memory_cache()
    gc.collect()
    r2 = rss_mb()
    alive = sum(1 for _, w in refs if w() is not None)
    print(f"  rss {r0:.1f} -> {r1:.1f} (loaded) -> {r2:.1f} (cleared+gc)")
    print(f"  cache at peak: {st_loaded['cache_mb']} MB / {st_loaded['cached_isins']} ISINs")
    print(f"  panels still referenced after clear+gc: {alive} / {len(refs)}")
    if alive:
        print("  !! something outside the cache retains panel DataFrames")
    return {"rss_start": round(r0, 1), "rss_loaded": round(r1, 1),
            "rss_cleared": round(r2, 1), "alive_after_clear": alive,
            "n_tracked": len(refs), "cache_at_peak": st_loaded}


# ── 6. gc experiment ─────────────────────────────────────────────────────────

def gc_experiment(n: int) -> dict:
    import research.indicator_panel as ip
    files = _panel_files()[:n]
    print(f"\n=== 6. GC EXPERIMENT ({len(files)} panels) ===")
    res = {}
    for label, collect_every in (("no explicit gc", 0), ("gc every 100", 100)):
        ip.clear_memory_cache(); gc.collect()
        r0 = rss_mb(); t0 = time.time()
        for i, f in enumerate(files, 1):
            ip.load_panel(f.stem)
            if collect_every and i % collect_every == 0:
                gc.collect()
        dt = time.time() - t0
        r1 = rss_mb()
        res[label] = {"rss_after": round(r1, 1), "rss_delta": round(r1 - r0, 1),
                      "runtime_s": round(dt, 2)}
        print(f"  {label:<16} rss {r0:>6.1f} -> {r1:>6.1f}  ({dt:.2f}s)")
    ip.clear_memory_cache(); gc.collect()
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Indicator-panel memory diagnostics")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--scaling", action="store_true")
    ap.add_argument("--storage", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--retention", action="store_true")
    ap.add_argument("--gc", action="store_true")
    ap.add_argument("--counts", default="100,500,1000,1500")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    out = {"rss_at_start_mb": round(rss_mb(), 1)}
    print(f"process RSS at start: {out['rss_at_start_mb']} MB")
    if a.all or a.scaling:
        out["scaling"] = scaling([int(x) for x in a.counts.split(",")])
    if a.all or a.storage:
        out["storage"] = storage(a.n)
    if a.all or a.rebuild:
        out["rebuild"] = rebuild(min(a.n, 25))
    if a.all or a.retention:
        out["retention"] = retention(min(a.n * 5, 500))
    if a.all or a.gc:
        out["gc"] = gc_experiment(min(a.n * 5, 500))
    out["rss_at_end_mb"] = round(rss_mb(), 1)
    print(f"\nprocess RSS at end: {out['rss_at_end_mb']} MB")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str))
