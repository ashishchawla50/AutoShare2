"""Step-1 memory-safety stress test: the full historical run, twice.

The decisive acceptance test. Runs the complete historical window end to end on the
fast (indicator-panel) path, sampling RSS continuously, and repeats it so peak memory
reproducibility can be judged. Also re-checks that memory behaviour did not alter any
trade or the ledger.

Not an optimisation tool — a safety gate. Rerunnable later.

Usage:
    python tools/stress_full_run.py --runs 2 --start 2021-09-01 --end 2026-08-10
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="%(message)s")

PAGE = os.sysconf("SC_PAGE_SIZE")


def rss_mb() -> float:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * PAGE / 1024 / 1024


class RssSampler(threading.Thread):
    """Samples RSS continuously so we capture the true high-water, not just the end."""

    def __init__(self, interval: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.peak = 0.0
        self.samples: list[float] = []
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            r = rss_mb()
            self.peak = max(self.peak, r)
            self.samples.append(round(r, 1))
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def one_run(start: date, end: date, capital: float, strategy: str) -> dict:
    from backtest.backtest_engine import BacktestEngine
    import research.indicator_panel as ip

    ip.clear_memory_cache()
    gc.collect()
    s = RssSampler(1.0)
    start_rss = rss_mb()
    s.start()
    t0 = time.time()
    engine = BacktestEngine(start_date=start, end_date=end, starting_capital=capital,
                            strategy=strategy, cost_profile="cnc", use_panel=True)
    metrics = engine.run()
    runtime = time.time() - t0
    s.stop(); s.join(timeout=3)
    end_rss = rss_mb()
    cache = ip.cache_stats()

    total_pnl = sum(t["realised_pnl"] for t in engine._closed_trades)
    ledger_err = abs(total_pnl - (engine.capital - engine.starting_capital))
    trades = [(t.get("isin"), str(t["entry_date"]), str(t["exit_date"]),
               round(float(t["entry_price"]), 6), round(float(t["exit_price"]), 6),
               int(t["shares"]), round(float(t["realised_pnl"]), 6), t["exit_reason"])
              for t in engine._closed_trades]
    return {
        "start_rss_mb": round(start_rss, 1),
        "peak_rss_mb": round(s.peak, 1),
        "end_rss_mb": round(end_rss, 1),
        "runtime_s": round(runtime, 1),
        "trading_days": len(s.samples) and None or None,
        "n_trades": metrics.total_trades,
        "final_capital": round(engine.capital, 6),
        "ledger_error": round(ledger_err, 6),
        "cache_final": cache,
        "panel_stats": getattr(engine, "_panel_stats", {}),
        "rss_p50": round(sorted(s.samples)[len(s.samples) // 2], 1) if s.samples else None,
        "n_samples": len(s.samples),
        "trades": trades,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Full-run memory stress test")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--start", default="2021-09-01")
    ap.add_argument("--end", default="2026-08-10")
    ap.add_argument("--capital", type=float, default=50000.0)
    ap.add_argument("--strategy", default="vcp_v2")
    ap.add_argument("--json", default="/tmp/stress_full.json")
    a = ap.parse_args()

    results = []
    for i in range(1, a.runs + 1):
        print(f"\n===== FULL RUN {i}/{a.runs} =====", flush=True)
        r = one_run(date.fromisoformat(a.start), date.fromisoformat(a.end),
                    a.capital, a.strategy)
        results.append(r)
        print(f"run {i}: peak_rss {r['peak_rss_mb']} MB  end_rss {r['end_rss_mb']} MB  "
              f"runtime {r['runtime_s']}s  trades {r['n_trades']}  "
              f"final ₹{r['final_capital']:.2f}  ledger_err {r['ledger_error']}", flush=True)
        print(f"        cache {r['cache_final']}", flush=True)
        Path(a.json).write_text(json.dumps(results, indent=1, default=str))

    print("\n===== SUMMARY =====")
    for i, r in enumerate(results, 1):
        print(f"  run {i}: peak {r['peak_rss_mb']} MB | end {r['end_rss_mb']} MB | "
              f"{r['runtime_s']}s | {r['n_trades']} trades | ₹{r['final_capital']:.2f}")
    if len(results) > 1:
        peaks = [r["peak_rss_mb"] for r in results]
        print(f"  peak RSS spread: {max(peaks) - min(peaks):.1f} MB "
              f"({min(peaks)} .. {max(peaks)})")
        same = all(r["trades"] == results[0]["trades"] for r in results)
        cap = all(r["final_capital"] == results[0]["final_capital"] for r in results)
        print(f"  trades identical across runs: {same}")
        print(f"  final capital identical across runs: {cap}")
    Path(a.json).write_text(json.dumps(results, indent=1, default=str))
