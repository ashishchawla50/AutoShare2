"""Download India VIX daily history from Kite into data/india_vix.csv.

Run where a valid Kite access token exists (the VPS):
    venv/bin/python tools/fetch_india_vix.py --start 2019-01-01
Read-only: one historical-data call per ~4-year chunk, no orders.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.config import config  # noqa: E402

INDIA_VIX_TOKEN = 264969          # Kite instrument token for NSE:INDIA VIX
CHUNK_DAYS = 1400                 # Kite allows ~2000 days of daily candles per call


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--out", default=str(Path(config.DB_PATH) / config.INDIA_VIX_CSV))
    args = ap.parse_args()

    from kiteconnect import KiteConnect
    from broker.auth_manager import AuthManager

    token = AuthManager().get_access_token()
    if not token:
        sys.exit("No Kite access token")
    kite = KiteConnect(api_key=config.ZERODHA_API_KEY)
    kite.set_access_token(token)

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    rows = []
    a = start
    while a <= end:
        b = min(end, a + timedelta(days=CHUNK_DAYS))
        rows += kite.historical_data(INDIA_VIX_TOKEN, a, b, "day")
        a = b + timedelta(days=1)

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close"])
        for r in rows:
            w.writerow([r["date"].date().isoformat(), r["open"], r["high"], r["low"], r["close"]])
    print(f"{len(rows)} rows {rows[0]['date'].date()} → {rows[-1]['date'].date()} written to {args.out}")


if __name__ == "__main__":
    main()
