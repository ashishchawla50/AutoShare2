#!/usr/bin/env python3
"""End-to-end paper pipeline test — run any time, including market hours.

Clears the paper book at current prices, reruns the full scan → rank → entry
pipeline with the entry window patched open, and takes real paper entries.
Lets you verify a code change immediately instead of waiting for the next
9:30 entry window.

Everything goes through the bot's own machinery: exits via _execute_exit
(paper sell order + ledger + drawdown recording + Telegram), entries via
check_entries (sizing, risk gates, paper buy order + fake GTT + Telegram).
Real [PAPER] alerts are sent on purpose so the run is visible in Telegram.

Usage (on the VPS):
    ~/AutoShare/venv/bin/python3 scripts/e2e_paper_test.py

PAPER MODE ONLY — refuses to run when PAPER_TRADING_MODE is false.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.config import config

if not config.PAPER_TRADING_MODE:
    print("REFUSED: this script closes the whole book and enters at will — paper mode only.")
    sys.exit(1)

from utils.logger import setup_logging
setup_logging()

import logging
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("LOG %(name)s: %(message)s"))
for name in ("trading", "orders", "scanner", "risk"):
    logging.getLogger(name).addHandler(console)

import main as m

bot = m.TradingBot()
bot.startup()
if not bot._kite.is_connected:
    print("FATAL: Kite not connected — run TOTP login first")
    print("E2E COMPLETE")
    sys.exit(1)

try:
    bot.refresh_universe_snapshot()
except Exception as exc:
    print(f"universe snapshot refresh failed ({exc}) — using cached universe")

bot.refresh_universe_ohlcv()
print(f"OHLCV loaded: {len(bot._universe_ohlcv)} stocks")
bot.check_regime()
rs = bot._regime_state
print(f"Regime: {rs.regime.value} | allow_new_trades={rs.allow_new_trades} | mult={rs.position_size_multiplier}")

# ── STEP 1: clear the book at current prices ────────────────────────────────
print("\n=== STEP 1: closing all open paper positions ===")
for pos in bot._portfolio_manager.get_open_positions():
    sym = pos["symbol"]
    df = bot._universe_ohlcv.get(sym)
    price = float(df["close"].iloc[-1]) if df is not None and not df.empty else float(pos["entry_price"])
    bot._execute_exit(sym, price, "manual")
    print(f"closed {sym} @ ₹{price:.2f}")

remaining = bot._portfolio_manager.get_open_positions()
print(f"Open positions after reset: {len(remaining)} (expected 0)")
print(f"Portfolio: ₹{bot._portfolio_manager.get_portfolio_value():,.0f} | "
      f"available cash: ₹{bot._portfolio_manager.get_available_cash():,.0f}")

can_trade, dd_reason = bot._drawdown_monitor.check_all_limits()
print(f"Drawdown limits OK: {can_trade} {('— ' + dd_reason) if not can_trade else ''}")

# ── STEP 2: full scan pipeline ──────────────────────────────────────────────
print("\n=== STEP 2: pre-scan filters ===")
bot.run_pre_scan()
print(f"Universe after filters: {len(bot._universe_ohlcv)} stocks")

print("\n=== STEP 3: strategy scans ===")
bot.run_strategy_scan()
bot.run_momentum_scan()
bot.run_breakout_scan()
bot.run_mean_reversion_scan()
print(f"Raw watchlist across strategies: {len(bot._watchlist)} setups")

print("\n=== STEP 4: rank_opportunities (pool + per-strategy caps) ===")
bot.rank_opportunities()
for s in bot._watchlist:
    print(f"  kept: {s.symbol:<14} [{s.strategy}] score={s.composite_score:.1f} px=₹{s.entry_price:.2f}")

# ── STEP 5: take real paper entries now ─────────────────────────────────────
print("\n=== STEP 5: check_entries with entry window patched open ===")
m.is_entry_window = lambda: True
bot.check_entries()

final = bot._portfolio_manager.get_open_positions()
print("\n=== RESULT ===")
print(f"Entered {len(final)} paper positions (cap {config.MAX_POSITIONS}):")
for p in final:
    print(
        f"  {p['symbol']:<14} [{p.get('strategy')}] {p['shares']} sh @ ₹{float(p['entry_price']):.2f}"
        f" = ₹{p['shares'] * float(p['entry_price']):,.0f} | stop ₹{float(p['stop_loss']):.2f}"
    )
print(f"Portfolio: ₹{bot._portfolio_manager.get_portfolio_value():,.0f} | "
      f"cash: ₹{bot._portfolio_manager.get_available_cash():,.0f}")
ok = len(final) <= config.MAX_POSITIONS
print(f"Pool cap respected: {'PASS' if ok else 'FAIL'}")
print("E2E COMPLETE")
sys.exit(0 if ok else 1)
