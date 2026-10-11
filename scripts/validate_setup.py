"""Pre-launch validation checklist.

Run before going live to verify the complete system is correctly configured.
Usage:
    python scripts/validate_setup.py

All checks must pass (✅) before switching from paper to live mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import importlib
import os


def check(label: str, condition: bool, fatal: bool = False) -> bool:
    icon = "[OK]" if condition else ("[FAIL]" if fatal else "[WARN]")
    print(f"  {icon} {label}")
    if not condition and fatal:
        print("     FATAL -- cannot proceed")
    return condition


def run_validation() -> bool:
    print("\n" + "=" * 60)
    print("PRE-LAUNCH VALIDATION CHECKLIST")
    print("=" * 60)
    all_passed = True

    # ── Python version ────────────────────────────────────────────────────────
    # 3.10 is the blessed floor: the VPS (Ubuntu 22.04) ran every backtest,
    # the full validation campaign, and all paper trading on 3.10.12 — that IS
    # the tested environment. Upgrade deliberately (rebuild venv + full test
    # pass + paper soak), never casually.
    print("\n[1] Python Environment")
    py_ok = sys.version_info >= (3, 10)
    all_passed &= check(f"Python 3.10+ ({sys.version.split()[0]})", py_ok, fatal=True)

    # ── Required packages ─────────────────────────────────────────────────────
    print("\n[2] Required Packages")
    # (import_name, display_name) — some packages have non-standard import names
    packages = [
        ("kiteconnect", "kiteconnect"),
        ("pandas", "pandas"),
        ("numpy", "numpy"),
        ("scipy", "scipy"),
        ("requests", "requests"),
        ("bs4", "beautifulsoup4"),
        ("sqlalchemy", "sqlalchemy"),
        ("apscheduler", "APScheduler"),
        ("flask", "flask"),
        ("telegram", "python-telegram-bot"),
        ("pyotp", "pyotp"),
        ("cryptography", "cryptography"),
        ("dotenv", "python-dotenv"),
        ("pytz", "pytz"),
        ("psutil", "psutil"),
        ("pytest", "pytest"),
    ]
    for import_name, display_name in packages:
        try:
            importlib.import_module(import_name)
            check(f"  {display_name}", True)
        except ImportError:
            all_passed &= check(f"  {display_name} -- NOT INSTALLED", False)

    # ── Database files ────────────────────────────────────────────────────────
    print("\n[3] Databases")
    dbs = ["market_data.db", "fundamentals.db", "corporate_events.db",
           "surveillance.db", "portfolio_state.db", "regime_history.db"]
    for db in dbs:
        exists = Path(f"data/{db}").exists()
        all_passed &= check(f"  data/{db}", exists, fatal=True)

    # ── Environment variables ─────────────────────────────────────────────────
    print("\n[4] Environment Variables")
    from config.config import config
    check("TELEGRAM_BOT_TOKEN set", bool(config.TELEGRAM_BOT_TOKEN))
    check("TELEGRAM_CHAT_ID set", bool(config.TELEGRAM_CHAT_ID))

    if not config.PAPER_TRADING_MODE:
        all_passed &= check("ZERODHA_API_KEY set", bool(config.ZERODHA_API_KEY), fatal=True)
        all_passed &= check("ZERODHA_API_SECRET set", bool(config.ZERODHA_API_SECRET), fatal=True)
        all_passed &= check("TOKEN_ENCRYPTION_KEY set", bool(config.TOKEN_ENCRYPTION_KEY), fatal=True)
    else:
        check("Paper mode — broker credentials optional", True)

    # ── Config validation ─────────────────────────────────────────────────────
    print("\n[5] Config Validation")
    errors = config.validate()
    if not errors:
        check("Config passes validation", True)
    else:
        for err in errors:
            all_passed &= check(f"  {err}", False)

    # ── Core imports ──────────────────────────────────────────────────────────
    print("\n[6] Module Imports")
    modules = [
        "config.config",
        "utils.logger",
        "utils.indicator_utils",
        "strategy.vcp_detector",
        "strategy.trend_template",
        "strategy.stage_analysis",
        "scanner.stock_filter",
        "risk.position_sizer",
        "regime.regime_detector",
        "backtest.performance_analyzer",
    ]
    for mod in modules:
        try:
            importlib.import_module(mod)
            check(f"  {mod}", True)
        except Exception as exc:
            all_passed &= check(f"  {mod} — IMPORT ERROR: {exc}", False, fatal=True)

    # ── Paper mode check ──────────────────────────────────────────────────────
    print("\n[7] Trading Mode")
    check("Paper trading mode ACTIVE", config.PAPER_TRADING_MODE)
    if not config.PAPER_TRADING_MODE:
        print("  ⚠️  WARNING: LIVE MODE — real money will be traded!")

    # ── Logs directory ────────────────────────────────────────────────────────
    print("\n[8] Logs Directory")
    check("logs/ directory exists", Path("logs").exists())

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if all_passed:
        print("[OK] ALL CHECKS PASSED -- system ready")
    else:
        print("[FAIL] SOME CHECKS FAILED -- resolve before going live")
    print("=" * 60)

    return all_passed


if __name__ == "__main__":
    passed = run_validation()
    sys.exit(0 if passed else 1)
