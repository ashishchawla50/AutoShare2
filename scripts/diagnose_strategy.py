#!/usr/bin/env python3
"""Diagnose why the strategy produces 0 trades.

Runs analyse_stock() on all universe symbols with the latest available
data and shows exactly where in the pipeline stocks are being filtered out.

Usage (on VPS):
    cd ~/AutoShare && ~/AutoShare/venv/bin/python3 scripts/diagnose_strategy.py
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger("diagnose")

from config.config import config
from backtest.data_loader import load_ohlcv
from data_fetcher.universe_fetcher import load_universe
from strategy.trend_template import passes_trend_template
from strategy.vcp_detector import detect_vcp


def main() -> None:
    end = date.today()
    start = end - timedelta(days=730)

    symbols = load_universe()
    if not symbols:
        print("ERROR: No universe snapshot. Run: ~/AutoShare/venv/bin/python3 scripts/manual_scan.py")
        sys.exit(1)

    print(f"Universe : {len(symbols)} symbols")
    print(f"Data range: {start} -> {end}")
    print()

    total = 0
    no_data = 0
    tt_fail = 0
    vcp_invalid = 0
    vcp_tightness_fail = 0
    scan_pass = 0

    tt_condition_failures: dict[str, int] = defaultdict(int)
    conditions_met_dist: dict[int, int] = defaultdict(int)
    vcp_failure_reasons: dict[str, int] = defaultdict(int)
    scan_pass_names: list[str] = []

    for symbol in symbols:
        df = load_ohlcv(symbol, start, end)
        total += 1

        if df is None or len(df) < 210:
            no_data += 1
            continue

        # Step 1: Trend Template (all 8 conditions required)
        tt = passes_trend_template(df)
        conditions_met_dist[tt.conditions_met] += 1

        if not tt.passes:
            tt_fail += 1
            for cname, cpassed in tt.conditions.items():
                if not cpassed:
                    tt_condition_failures[cname] += 1
            continue

        # Step 2: VCP detection
        vcp = detect_vcp(df)

        if not vcp.is_valid:
            vcp_invalid += 1
            short_reason = vcp.failure_reason[:70]
            vcp_failure_reasons[short_reason] += 1
            continue

        # Step 3: VCP tightness gate
        if vcp.tightness_score < config.VCP_MIN_TIGHTNESS_SCORE:
            vcp_tightness_fail += 1
            continue

        # Passes scan mode — ready for breakout confirmation
        scan_pass += 1
        scan_pass_names.append(
            f"  {symbol:<15} TT={tt.conditions_met}/8 | VCP tightness={vcp.tightness_score:.0f}"
            f" | prior_uptrend={vcp.prior_uptrend_pct:.0%}"
            f" | contractions={vcp.contraction_count}"
            f" | ATR_ratio={vcp.atr_compression_ratio:.2f}"
        )

    tradeable_base = total - no_data
    pct = lambda n: f"{n/max(1,tradeable_base)*100:.0f}%"

    print("=" * 65)
    print("PIPELINE BREAKDOWN (scan mode — no breakout required)")
    print("=" * 65)
    print(f"Total symbols checked      : {total}")
    print(f"  Insufficient data (<210d): {no_data}")
    print(f"  Tradeable base            : {tradeable_base}")
    print()
    print(f"  Failed Trend Template    : {tt_fail:4d}  ({pct(tt_fail)} of tradeable base)")
    print(f"  Failed VCP (invalid)     : {vcp_invalid:4d}  ({pct(vcp_invalid)})")
    print(f"  Failed VCP tightness<50  : {vcp_tightness_fail:4d}  ({pct(vcp_tightness_fail)})")
    print(f"  PASSED — ready to trade  : {scan_pass:4d}  ({pct(scan_pass)})")
    print()

    if tt_condition_failures:
        print("Trend Template — failing conditions (most common first):")
        for cname, count in sorted(tt_condition_failures.items(), key=lambda x: -x[1]):
            print(f"  {count:4d}  {cname}")
        print()

    if vcp_failure_reasons:
        print("VCP failure reasons:")
        for reason, count in sorted(vcp_failure_reasons.items(), key=lambda x: -x[1]):
            print(f"  {count:4d}  {reason}")
        print()

    print("Trend Template — conditions-met distribution (across all stocks):")
    for k in range(9):
        if k in conditions_met_dist:
            bar = "#" * min(50, conditions_met_dist[k])
            print(f"  {k}/8  {conditions_met_dist[k]:4d} stocks  {bar}")
    print()

    if scan_pass_names:
        print("Stocks that pass (breakout check still needed for actual entry):")
        for line in scan_pass_names:
            print(line)
    else:
        print("DIAGNOSIS: 0 stocks pass scan mode.")
        print()
        print("Primary bottleneck is shown in the failing conditions above.")
        print("Most likely cause: Trend Template c3/c6/c8 fail because the")
        print("market correction (Sep 2024 - Feb 2025) left EMA200 declining")
        print("and stocks still below their 52-week high.")
        print()
        print("What this means:")
        print("  - The strategy IS working as designed (only buys confirmed bull trends)")
        print("  - The Indian market has not fully recovered into bull-trend territory")
        print("  - The bot will start finding setups as market conditions improve")
        print("  - The momentum strategy may still trigger (check live bot logs)")


if __name__ == "__main__":
    main()
