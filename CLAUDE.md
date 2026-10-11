# CLAUDE.md — AutoShare Trading Bot

> AI session context document. Read this first. All facts are inferred from source code.

---

## Project Name
**AutoShare** — Automated NSE Swing Trading Bot

## One-Paragraph Summary
AutoShare is a fully automated equity swing trading bot targeting NSE (India) stocks via Zerodha Kite Connect. Its sole validated engine is **vcp_v2** — a two-stage Minervini VCP: a tight consolidation puts the pivot on a persistent watchlist, and entry fires only on a confirmed breakout through that pivot (out-of-sample PF 1.61, edge confirmed 2026-07-16). Three earlier strategies (EMA momentum, 20-day breakout, Connors RSI-2 mean reversion) were statistically rejected in the 2026-07 validation campaign and are benched via `config.DISABLED_STRATEGIES`. All entries are gated by a market regime detector (BULL / CAUTIOUS / BEAR / HIGH_VOLATILITY / EXTREME_VOLATILITY) that blocks new entries in adverse conditions and scales position sizes. The bot runs 24/7 on an Ubuntu VPS, executes a defined daily schedule using APScheduler, manages risk via GTT stop-loss orders that survive restarts, and sends all alerts through Telegram. Paper mode is the code default, but **the VPS deployment is LIVE since 2026-07-20** (₹50k pilot — see `GO_LIVE.md`).

## Purpose
- Automate the daily workflow of a discretionary swing trader: pre-market data refresh, pre-scan, strategy scan, entry/exit during market hours, end-of-day reporting
- Enforce systematic risk rules (position sizing, drawdown limits, regime filters) without human intervention
- Target: 18–25% CAGR, max 15% drawdown, NSE equity CNC only

---

## Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Language | Python | 3.12 (required) |
| Broker API | Zerodha Kite Connect | kiteconnect 5.0.1 |
| Data manipulation | pandas / numpy / scipy | 2.2.0 / 1.26.4 / 1.12.0 |
| Database ORM | SQLAlchemy | 2.0.27 |
| Database engine | SQLite | (bundled with Python) |
| Scheduler | APScheduler | 3.10.4 |
| Auth callback server | Flask | 3.0.2 |
| Alerts | python-telegram-bot | 21.0.1 |
| TOTP 2FA | pyotp | 2.9.0 |
| Token encryption | cryptography (Fernet) | 42.0.4 |
| HTTP data scraping | requests + BeautifulSoup4 + lxml | 2.31 / 4.12.3 / 5.1.0 |
| NSE data | nsepy + nsetools | 0.8 / 1.0.11 |
| Process management | Supervisord | (system package) |
| Testing | pytest + pytest-cov + pytest-mock | 8.0.2 |
| Type checking | mypy | 1.8.0 |
| Env loading | python-dotenv | 1.0.1 |

---

## Project Structure

```
AutoShare/
├── main.py                  # TradingBot class — daily workflow orchestrator
├── scheduler.py             # APScheduler job definitions (all cron times IST)
├── requirements.txt
├── setup.sh                 # Ubuntu VPS one-shot setup script
├── supervisord.conf         # Supervisord config for VPS process management
├── .env.example             # Environment variable template
│
├── config/
│   ├── config.py            # TradingConfig dataclass — all constants + secrets
│   └── holidays.py          # NSE holiday calendar + trading day checks
│
├── utils/
│   ├── logger.py            # JSON structured logging, domain-specific log files
│   ├── encryption.py        # Fernet encryption for access token storage
│   ├── health_check.py      # Heartbeat + dead man's switch + HTTP health server
│   ├── indicator_utils.py   # EMA, SMA, RSI, swing high/low calculations
│   ├── time_utils.py        # IST time helpers, market hours checks
│   ├── data_utils.py        # Missing trading day counting
│   └── backup.py            # SQLite database backup utility
│
├── broker/
│   ├── kite_client.py       # Zerodha Kite wrapper — rate limiting, paper mode intercept
│   ├── auth_manager.py      # Zerodha session lifecycle (OAuth + TOTP)
│   ├── auth_server.py       # Flask server receiving Kite OAuth redirect (port 8080)
│   ├── order_manager.py     # Buy/sell/GTT order abstraction
│   ├── portfolio_manager.py # Open position tracking, cash management
│   └── gtt_monitor.py       # GTT backup monitor — verifies stop-losses every 60s
│
├── data_fetcher/
│   ├── ohlcv_fetcher.py     # OHLCV via Kite, caches to SQLite market_data.db
│   ├── universe_fetcher.py  # Universe list from NSE (Nifty 500 live since 2026-07-14; config.UNIVERSE_INDEX)
│   ├── fundamental_fetcher.py # Market cap, D/E, promoter pledge from Screener.in
│   ├── surveillance_fetcher.py # ASM/ESM lists from NSE
│   └── event_fetcher.py     # Corporate events (results, dividends, bonus, splits)
│
├── strategy/
│   ├── vcp_detector.py      # Volatility Contraction Pattern (Minervini algorithm)
│   ├── pivot_watch.py       # Persistent pivot watchlist — stage 2 of the vcp_v2 entry
│   ├── trend_template.py    # Minervini Trend Template (8 conditions, all required)
│   ├── stage_analysis.py    # Weinstein Stage Analysis (Stage 1/2/3/4)
│   ├── breakout_detector.py # Pivot breakout + gap evaluation
│   ├── momentum_strategy.py # EMA-crossover momentum (simple 4-condition system)
│   ├── breakout_scanner.py  # 20-day range breakout (new N-day high + 1.5x vol + EMA50 + RSI)
│   ├── mean_reversion.py    # Connors RSI-2 mean reversion (close > EMA200, RSI(2) < 10)
│   └── multi_timeframe.py   # Combines Stage + Trend Template + VCP into final verdict
│
├── scanner/
│   ├── stock_filter.py      # 9 sequential filters (price, cap, liquidity, etc.)
│   ├── relative_strength.py # O'Neil RS scoring vs Nifty 50
│   ├── opportunity_ranker.py # Composite score ranking, TradeSetup dataclass; +5 bonus if within 10% of 52w high
│   └── sector_regime.py     # Sector-level regime analysis
│
├── risk/
│   ├── position_sizer.py    # Risk-based position sizing (2.0% risk / 20% cap)
│   ├── drawdown_monitor.py  # Daily/weekly/monthly/max drawdown enforcement
│   └── risk_manager.py      # Entry approval: sector cap, concentration checks
│
├── regime/
│   ├── regime_detector.py   # 5-state regime machine + 2-day upgrade confirmation
│   └── vix_monitor.py       # India VIX fetching + position size multiplier
│
├── exit/
│   ├── stop_loss_manager.py # Hard stop + trailing ratchet stop (4 tiers)
│   ├── profit_manager.py    # Partial profit booking at 3 tiers
│   └── time_stop.py         # Time-based exit + regime-triggered exit
│
├── portfolio/
│   ├── portfolio_tracker.py # Daily/weekly P&L snapshots, metrics recording
│   └── pnl_calculator.py    # P&L calculations
│
├── backtest/
│   ├── backtest_engine.py   # Event-driven backtester
│   ├── performance_analyzer.py # Sharpe, max DD, CAGR, win rate
│   ├── data_loader.py       # Load OHLCV from SQLite for backtesting
│   ├── cost_model.py        # COST_PROFILES: "legacy" (default, published results) / "cnc" (real Zerodha delivery)
│   └── point_in_time_universe.py # Avoids look-ahead bias in backtests
│
├── reports/
│   ├── daily_report.py      # EOD Telegram report
│   ├── weekly_report.py     # Monday 7 AM Telegram report
│   └── monthly_report.py    # Monthly performance summary
│
├── alerts/
│   └── telegram_alert.py    # All Telegram alerts (prefixed [PAPER] in paper mode)
│
├── scripts/
│   ├── setup_db.py          # Initialize all 6 SQLite databases (safe to re-run)
│   ├── validate_setup.py    # Pre-launch checklist (run before going live)
│   ├── backfill_data.py     # Seed historical OHLCV from Kite
│   ├── backfill_events.py   # Historical NSE corporate actions + board-meeting results dates
│   │                        # → writes corporate_events_full.db (NEVER the live events DB)
│   ├── run_backtest.py      # Run backtest engine
│   └── manual_scan.py       # One-off strategy scan without running the bot
│
├── tests/
│   ├── conftest.py          # Paper mode forced, Telegram disabled, ALL data paths → tmp_path
│   ├── test_conftest_isolation.py # Guards that isolation (2026-08-06 regime poisoning)
│   ├── test_order_safety.py # Live order-path hardening (fill-confirmed exits, GTT mandatory)
│   ├── test_strategy.py     # VCP, trend template, stage analysis tests
│   ├── test_filters.py      # All 9 stock filter tests
│   ├── test_risk.py         # Position sizer, drawdown monitor tests
│   ├── test_regime.py       # Regime detector state machine tests
│   ├── test_backtest.py     # Backtest engine + performance analyzer tests
│   ├── test_momentum.py     # Momentum strategy tests
│   └── test_vcp_detector.py # Dedicated VCP pattern tests
│
├── data/                    # Runtime only — gitignored
│   ├── market_data.db
│   ├── fundamentals.db
│   ├── corporate_events.db  # LIVE only — populated ~10 days ahead by the daily job
│   ├── corporate_events_full.db # Backtest only — 2021-2026 history (--events-db)
│   ├── surveillance.db
│   ├── portfolio_state.db
│   ├── regime_history.db
│   ├── .access_token.enc    # Encrypted Zerodha access token (daily)
│   ├── .last_heartbeat      # Heartbeat timestamp file
│   ├── .regime_upgrade_state # Pending regime upgrade persistence
│   ├── manual_blocklist.txt # Symbols the bot must never trade (no restart needed)
│   ├── manual_exit.txt      # Write a symbol here → bot exits it cleanly within 5 min
│   ├── .pivot_watchlist.json # vcp_v2 pivot watch state (survives restarts)
│   └── backups/             # Rotating SQLite backups
│
└── logs/                    # Runtime only — gitignored
    ├── trading.log          # Main bot events (DEBUG+)
    ├── orders.log           # All order activity (DEBUG+)
    ├── scanner.log          # Scanner + filter decisions (DEBUG+)
    ├── risk.log             # Risk check outcomes (DEBUG+)
    ├── regime.log           # Regime transitions (DEBUG+)
    ├── data.log             # Data fetching (DEBUG+)
    └── errors.log           # All WARNING+ from every module
```

---

## Build & Run Commands

```bash
# Initial setup (Ubuntu VPS — run as root once)
bash setup.sh

# Initialize databases (safe to re-run)
python scripts/setup_db.py

# Validate the system before going live
python scripts/validate_setup.py

# Backfill historical data (needs Kite access token first)
python scripts/backfill_data.py

# Run the bot (paper mode, default)
python main.py

# Run the bot (explicit paper mode)
python main.py --paper

# Run all tests
pytest

# Run tests with coverage
pytest --cov=. --cov-report=term-missing

# Type check
mypy .

# Manual strategy scan (no scheduler, one-off)
python scripts/manual_scan.py

# Quick strategy test — any strategy, seconds, local (windows end on the dataset's last date)
python scripts/run_backtest.py --window 6m --strategy mean_reversion --capital 100000 --ignore-regime
python scripts/run_backtest.py --window 1y                        # vcp_v2 (default), ₹50k, CNC costs
python scripts/run_backtest.py --strategy all --window full --no-dd-halt --dump-trades t.csv
python scripts/run_backtest.py --exit my_strategy --stop-pct 0.10 --risk-pct 0.02 --max-pos-pct 0.20
#   --exit vcp|my_strategy (vcp_v2 only); --stop-pct/--risk-pct/--max-pos-pct override config for this run
#   strategies: vcp_v2 mean_reversion momentum momentum_v2..v4 breakout breakout_v2 all
#   windows: 1m 3m 6m 1y 2y 3y 5y full   (or --start/--end)

# vcp_v2-only fast backtest (same engine as run_backtest.py --strategy vcp_v2)
python scripts/fast_backtest.py                                   # full 2021-09 → 2026-08, ₹50k, CNC costs
python scripts/fast_backtest.py --start 2025-01-01 --end 2026-06-30
python scripts/fast_backtest.py --start 2021-06-01 --end 2024-12-31 --no-dd-halt --dump-trades dev.csv

# Prove the fast engine still matches the frozen reference fixtures (~10 s)
pytest tests/test_fast_engine.py

# Rule-strategy signal parity vs the strategy check_* functions (~40 s)
pytest tests/test_fast_rules.py

# Slow reference engine (hours; only to capture new oracle fixtures)
python backtest/backtest_engine.py --start 2025-01-01 --end 2026-06-30 --strategy vcp_v2
```

### Fast backtester (`backtest/fast/`, added 2026-10-11 in AutoShare2)

All backtesting is local. `backtest/fast/` replays vcp_v2 on `data/reconstructed.db` in
~2.5 s for the full 5-year window (reference engine: 3.4 h).

- `data.py` loads every OHLCV row into flat numpy arrays (cached in `data/cache/fast/`, keyed on the dataset fingerprint).
- `signals.py` precomputes `analyse_stock`'s scan verdict (trend template, VCP, tightness, pivot) for every row of every security. The trend template is vectorised and the VCP check runs under numba. This is possible because the scan depends only on the stock's own history, never on the portfolio.
- `engine.py` is the portfolio loop. Python touches only universe members with a pending pivot or a fresh setup; all other rejections are counted in bulk.
- `rules.py` + `rule_engine.py` (added 2026-10-11) do the same for the benched rule strategies (mean_reversion, momentum v1–v4, breakout v1/v2, all). `rules.py` caches per-row entry verdicts; `tests/test_fast_rules.py` checks them row by row against `check_mean_reversion` / `check_momentum` / `check_range_breakout`. The portfolio loop follows `backtest_engine`'s branches for these strategies (caps, fixed-% sizing, targets, exits), but **it has no frozen oracle**, unlike vcp_v2.
- `--ignore-regime` (both engines) trades every day at full size. It is research-only: such results are not comparable to validation runs. momentum_v4 keeps its BULL-only rule regardless.
- `kernels.py` holds the numba code; it is imported only when a cache is built, so warm runs skip numba's import.
- **Correctness proof:** `tests/test_fast_engine.py` reproduces all six frozen oracle fixtures exactly: every trade field, every funnel counter and final capital to 1e-6.
- **Any change to strategy rules must be made in both engines,** or the reference re-captured (`tools/capture_reference_oracle.py`). A fast engine that no longer matches the oracle is wrong until proven otherwise.
- **Warm-up difference:** indicators seed from each security's first row. That equals the reference for any start on or before 2023-06-01; later starts get a better-warmed EMA here than the reference would compute.
- Caches rebuild automatically when `reconstructed.db` or `signals.py` changes (`--rebuild-cache` forces it).

---

## Environment Variables

All loaded from `.env` (copy `.env.example` to `.env`).

| Variable | Required for Live | Description |
|---|---|---|
| `ZERODHA_API_KEY` | Yes | Kite Connect API key |
| `ZERODHA_API_SECRET` | Yes | Kite Connect API secret |
| `ZERODHA_USER_ID` | Yes | Zerodha login ID |
| `ZERODHA_PASSWORD` | TOTP only | Zerodha login password (TOTP auto-login only) |
| `ZERODHA_TOTP_SECRET` | TOTP only | TOTP secret for automated 2FA (ToS risk) |
| `TOKEN_ENCRYPTION_KEY` | Yes | Fernet key for encrypting access token on disk |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat/channel ID for alerts |
| `PAPER_TRADING_MODE` | — | `false` to go live; default is `true` |
| `TOTP_AUTO_LOGIN_ENABLED` | — | `true` to enable headless TOTP login (default `false`) |
| `AUTH_OPERATOR_SECRET` | — | Secret for `POST /logout` (`X-Operator-Secret` header); unset = endpoint disabled |

Never commit `.env`. Token encryption key: generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

---

## Database Type & Migration Strategy

**Type:** SQLite (6 separate databases, all in `data/`)

| Database | Tables | Purpose |
|---|---|---|
| `market_data.db` | `ohlcv_daily`, `ohlcv_data_quality` | Daily OHLCV for the full universe (Nifty 500) |
| `fundamentals.db` | `fundamentals`, `shareholding_quarterly` | Market cap, D/E, sector, promoter pledge |
| `corporate_events.db` | `corporate_events` | Results, dividends, bonuses, splits |
| `surveillance.db` | `surveillance_list` | ASM/ESM/T2T watchlists from NSE |
| `portfolio_state.db` | `trades`, `open_positions`, `portfolio_metrics`, `loss_tracking` | All position and P&L state |
| `regime_history.db` | `regime_log` | Historical regime transitions |

**Migration strategy:** `scripts/setup_db.py` uses `CREATE TABLE IF NOT EXISTS` throughout — safe to re-run. The `migrate_portfolio_db()` function applies `ALTER TABLE` column additions with silent failure on duplicate columns (momentum strategy columns added post-v1).

---

## Authentication / Authorization Overview

**Active configuration on this VPS: TOTP auto-login is enabled.**

1. **Daily at 8:00 AM IST:** Bot auto-logs in using Zerodha password + TOTP secret (no manual action needed).
2. **On success:** Access token encrypted and saved to `data/.access_token.enc`; Kite connects automatically.
3. **On failure:** Falls back to sending a manual login URL via Telegram — open it promptly if received.
4. **Flask auth server (`broker/auth_server.py`):** Always running on port 8080 to handle the OAuth redirect (used by both manual and TOTP flows).

**If regime shows UNKNOWN:** TOTP login failed that morning. Check `logs/trading.log` for `"TOTP auto-login failed"` or `"Login URL sent"`. If TOTP fails, a manual URL is sent to Telegram as fallback.

**Manual flow (if TOTP disabled):** Set `TOTP_AUTO_LOGIN_ENABLED=false` → bot sends login URL via Telegram at 8 AM → open URL → Zerodha redirects to callback → token stored.

> Warning: TOTP automation uses Zerodha's unofficial internal endpoints (`/api/login`, `/api/twofa`), not the official Kite Connect OAuth spec. Risk of API key revocation if Zerodha detects abuse.

---

## API Overview

The bot is **not** an HTTP API server. External surfaces are:

| Port | Service | Purpose |
|---|---|---|
| 8080 | Flask (`auth_server.py`) | Receives Zerodha OAuth redirect `/callback` |
| 8080 | `/status` GET | Auth status health check |
| 8080 | `/logout` POST | Invalidate session |
| 8081 | HTTP (`health_check.py`) | Simple heartbeat endpoint for external monitors (UptimeRobot etc.) |

---

## Deployment Overview

- **Platform:** Ubuntu 22.04 LTS VPS (Oracle Cloud)
- **SSH:** `ssh -i ssh-key-2026-06-28.key ubuntu@152.67.10.33`
- **App path:** `~/AutoShare/`
- **Python:** `/home/trader/venv/bin/python` (virtualenv)
- **Process manager:** Supervisord — two supervised processes (`trading_bot`, `auth_flask`)
- **Firewall:** UFW — ports 22, 8080, 8081 open

---

## Current Development Status

**AutoShare2 (2026-10-11): restart of AutoShare, research-first and local-only.**
- **Base:** code from AutoShare `3a727c6`, which is the validated `b218e83` engine plus the August reconstructed-dataset work.
- **Do not use AutoShare `6ca6d37` ("old code", 2026-10-11).** It gutted `backtest_engine.py`: vcp_v2 deleted, regime gate hardcoded BULL, 4-stock fallback universe. It also rewrote config: `MAX_POSITIONS` 5, `MAX_DRAWDOWN_PCT` 0.05, mean reversion un-benched. Those four files were restored from `3a727c6`.
- **Fast engine:** backtests now run in seconds (`scripts/fast_backtest.py`; see the Fast backtester section).
- **vcp_v2 on the honest dataset** (reconstructed, ₹50k, CNC):
  - full 2021-09→2026-08: PF 1.20, 177 trades, bootstrap 70.8%, **edge NOT confirmed**;
  - 2025-01→2026-06: PF 1.78, 39 trades.
  - The 2026-07/08 validation numbers below came from the survivorship-biased `market_data.db` and are superseded.
- Everything below this point describes AutoShare's history and live VPS deployment.

**Phase: LIVE ₹50k Pilot (since 2026-07-20) — zero trades so far; machinery verified end-to-end through the risk gate on 2026-07-31**
- **2026-08-08: pilot-scale re-validation PASSED** (`VALIDATION_2026_08.md`). ₹50k OOS PF **1.75**, bootstrap **84.7%**, MaxDD −4.35% vs **1.77** at ₹5L under the same cost model — cost drag at ₹6k positions costs ~0.02 PF. ₹1L OOS PF 1.84 / 89.3% (promotion reference). Dev window PF 1.19 (vs 1.23 at ₹5L) but bootstrap 73.4% and **MaxDD −19.78%, brushing the 20% halt**. Two backtest bugs fixed first: the drawdown halt is **absorbing** (fired 2022-02-14 then all 737 remaining days, killing 3 of 3.5 dev years — use `--no-dd-halt` to measure the underlying edge) and the cost model charged ₹20/side brokerage that CNC delivery does not incur (`--cost-model cnc`; ₹4,003 of a ₹5,085 "loss" was phantom cost). Control against goalpost-moving: the cost fix moved ₹5L OOS 1.73→1.77 but ₹50k 1.33→1.75, exactly the asymmetry a fixed fee predicts.
- **Events filter verdict: KEEP** (OOS PF 1.75→2.22, removed 10 trades worth −₹1,051 net). But **live's filter is half dead** — its events table had zero `results` rows because the daily job only queries the corporate-actions endpoint; the "results ±7d" half has never fired live. Adding the board-meetings feed to `refresh_corporate_events` is pending sign-off (makes live stricter off a single-window result). Backtest reads `corporate_events_full.db` (69,648 rows) via `--events-db`; the live file is deliberately kept separate.
- **2026-07-16: vcp_v2 passed out-of-sample validation** — dev PF 1.11 (224 trades), OOS PF 1.61 (43 trades), bootstrap edge confirmed 85.6%, max DD −4.4% in a 38%-BEAR-blocked window. Sole validated engine.
- **Live VCP is now the two-stage pivot-watch flow** (`strategy/pivot_watch.py`, state in `data/.pivot_watchlist.json`): tight setup → pivot stored on 30-day watch → entry only on a valid breakout through the pivot. Mirrors `backtest_engine`'s vcp_v2 path — keep them in sync. No RS gate at watch-add (validated config has none) — **but this is misleading: `rank_setups` applies a hard RS ≥ 70 percentile drop to every confirmed breakout downstream, and the backtest has no RS gate anywhere. See `DIVERGENCE_INVENTORY.md` A1.**
- momentum_v3 failed OOS (dev 0.99 → OOS 0.55): momentum family rejected and stays benched.

- 134/134 tests pass as of 2026-07-14
- **5-yr gated backtest verdicts (2026-07-14, regime gate + costs, 2021–2026):** momentum PF 0.69 (−18.8%), breakout PF 0.69 (−15.6%), mean_reversion PF 0.61 (−19.1%), combined PF 0.75 — **all edges bootstrap-rejected**. Shared failure: 5% stops inside daily noise harvest losses at full frequency while fixed 10–12% targets amputate winners; ~0.5%/trade cost churn. VCP verdict pending (slow run).
- **Benched (no new entries):** momentum, breakout, mean_reversion — via `config.DISABLED_STRATEGIES`. Exits still manage open positions. Un-bench only after a revised variant passes dev (2021–2024) AND untouched validation (2025–2026) windows.
- **momentum_v2 experiment** (backtest-only): v1 entries, exit geometry inverted — 10% stop, EMA21 trend-break exit, 25-day time stop. Dev-window run queued on VPS.
- MAX_POSITIONS = 8 enforced as a **total pool** (watchlist truncated in `rank_opportunities`, re-checked per entry by `RiskManager.approve_entry`); per-strategy caps within it: momentum 3, breakout 3, MR 4
- **Incident 2026-07-10:** the VPS process had not been restarted after the Jul 9 `git pull`, ran stale pre-cap code, and opened 10 paper momentum positions in one window (exceeding MAX_POSITIONS and the momentum cap). Fixed same day: total-pool cap + risk gate for all strategies. Excess positions left to exit via normal rules; no new entries until open count < 8.
- **LIVE since 2026-07-20** — ₹50k plumbing pilot (`PAPER_TRADING_MODE=false` flipped 2026-07-19). Success = orders/GTTs/fills/ledger/reconcile behaving; P&L is noise at this size. `GO_LIVE.md` is the runbook.
- Sizing: 2.0% risk / 20% position cap (sizing study 2026-07-17; 2.5%/25% disqualified — breached the 20% DD halt)
- Personal holdings blocklisted (JIOFIN, SILVERBEES, SILVERIETF, HDFCGOLD) — audit 2026-07-19: the only buy callsite sits behind the blocklist gate
- Promotion to ₹1L capital: ≥3 months / ≥30 live trades with behavior matching backtest logic (then ₹2L → ₹5L staged)
- **2026-07-29 scan-pond restoration:** live was scanning the Nifty 500 but filtering it to ~141 stocks — far below the ~500-stock pond the PF 1.61 validation traded. Two unvalidated gates relaxed: ROE≥15% disabled (`ENFORCE_MIN_ROE=False`) and the 5-lakh-share liquidity floor removed (₹10 Cr/day traded value remains the sole liquidity gate). Entry rules untouched. Also fixed: nightly backup purge TypeError (naive/aware datetime).
- **Incident 2026-07-31 (first live signal lost):** SONACOMS fired a valid vcp_v2 breakout (rank 1, VCP 80) but sizing used `get_portfolio_value()` = whole Zerodha account (personal holdings + cash ≈ ₹5L) instead of bot capital, computed a ₹49.9k position, and the cash gate blocked it — then the consumed pivot was dropped. Fixed 2026-08-01: `get_portfolio_value()` now returns trading equity (starting capital + mode-filtered bot P&L; personal holdings never counted — `get_account_value()` keeps the account view for display), realized P&L is filtered by `is_paper`, and a risk-blocked vcp entry restores its pivot to the watch (`_restore_pivot_watch`).
- **2026-08-06 order-path hardening (external review + verification):** exits now check the sell result before closing the ledger or cancelling the GTT (failed sell → position stays open and protected, critical alert); a buy whose GTT placement fails twice is flattened immediately (`no_stop_protection`); `verify_gtt_orders` alerts on positions with no GTT ID instead of skipping them; ledger records broker average fill prices (`OrderManager.get_fill_price`) instead of intended prices; `sell_cnc`'s market-upgrade path now confirms the fill; the 8:20 reconcile also detects ledger-closed-but-still-held orphans (alert-only); `GTTBackupMonitor` sells remaining shares (not original), filters `is_paper` (a stale paper row could previously fire a real sell in live mode), and retries failed sells up to 3× instead of marking them handled; `/logout` on the public auth server requires `AUTH_OPERATOR_SECRET` (unset = disabled); `*.key`/`*.pem` gitignored. Daily-close exit cadence in `check_exits` documented as intentional (matches validated backtest; GTT + backup monitor handle intraday stops).
- 163/163 tests pass as of 2026-08-08 (new `tests/test_conftest_isolation.py`)
- **Test isolation fixed 2026-08-07:** `tests/conftest.py` gained an autouse `isolate_data_paths` fixture redirecting `config.DB_PATH`, the blocklist/manual-exit/backup paths, and the module-level `Path` constants in `utils/health_check.py`, `utils/encryption.py` and `utils/backup.py` to a per-test `tmp_path`. Before this, running pytest in the VPS production checkout wrote into the live databases — the cause of the 2026-08-06 regime poisoning. The three poisoned `regime_log` rows (327–329) were deleted 2026-08-07.

---

## Coding Conventions Observed

- `from __future__ import annotations` in every module (postponed evaluation)
- Dataclasses used for structured return types throughout (never plain tuples for multi-field results)
- All config values in `TradingConfig` dataclass with comments explaining the rationale
- Logger names follow domain prefixes: `"trading.*"`, `"orders.*"`, `"scanner.*"`, `"risk.*"`, `"regime"`, `"data.*"` — determines which log file receives the entry
- Paper mode intercept pattern: every order/GTT call checks `config.PAPER_TRADING_MODE` and returns a fake ID without hitting Kite
- Pure functions where possible in strategy/filter code — each filter is `(symbol, data) → (bool, reason_str)`

---

## Architecture Rules — Do Not Violate

1. **Paper mode is the default.** `PAPER_TRADING_MODE` must be explicitly set `false` in `.env` to go live. Config reads env on startup — no code changes needed.
2. **GTT orders are mandatory for every live position.** Enforced since 2026-08-06: if GTT placement fails twice after a buy, the position is flattened at market immediately (`no_stop_protection`), and `verify_gtt_orders` alerts on any tracked position without a GTT ID. GTTs survive restarts; plain orders do not.
3. **Regime downgrades are immediate; upgrades require 2-day confirmation.** This prevents whipsaws. The 2-day pending state is persisted to `data/.regime_upgrade_state` across restarts.
4. **No entries outside the entry window (9:30–10:30 IST).** Enforced in `check_entries()` via `is_entry_window()`.
5. **Emergency halt flag.** Create `data/emergency_halt.flag` to stop all new entries without restarting the bot. Delete it to resume.
6. **Manual blocklist.** Edit `data/manual_blocklist.txt` to exclude symbols from trading. Re-read on every entry check — no restart needed.
7. **Kite is always needed, even in paper mode.** The bot fetches real market data to make simulated paper decisions.
8. **All 8 Trend Template conditions are required.** No partial pass allowed.
9. **Never move stop further from entry.** Stop loss is a ratchet — only moves up, never down.
10. **Sector exposure cap is 30%.** Risk manager enforces this before approving any entry. `RiskManager.approve_entry` gates **all four strategies** (since 2026-07-10; previously VCP only) — it also re-checks the MAX_POSITIONS pool, duplicate holdings, and cash. Positions with unknown sector (Screener.in blocked) are excluded from the cap, pass-by-default.
11. **Never sell bot positions from the Kite app.** Manual sells desync the ledger and dangle GTTs. Use `data/manual_exit.txt` (symbol per line → clean exit within 5 min) or `data/emergency_halt.flag`. A daily 8:20 reconcile catches offline closes/partial sells as a backstop (counts `t1_quantity` — unsettled T+1 shares are still held).
12. **Always restart the bot after deploying code.** `git pull` on the VPS does not reload the running process — `sudo supervisorctl -c /etc/supervisor/supervisord.conf restart autoshare` is mandatory after every pull, and verify the process start time in `supervisorctl status` is newer than the pull. (A stale process caused the 2026-07-10 over-entry incident.)

---

## Common Development Commands

```bash
# Connect to VPS
ssh -i ssh-key-2026-06-28.key ubuntu@152.67.10.33

# Check bot status on VPS (always pass -c flag)
sudo supervisorctl -c /etc/supervisor/supervisord.conf status

# Restart trading bot on VPS (program name is 'autoshare')
sudo supervisorctl -c /etc/supervisor/supervisord.conf restart autoshare

# Restart auth server on VPS
sudo supervisorctl -c /etc/supervisor/supervisord.conf restart auth_flask

# Tail live logs on VPS
tail -f ~/AutoShare/logs/trading.log | python -m json.tool

# Run tests (local)
pytest -v

# Run a single test file
pytest tests/test_regime.py -v

# Check for type errors
mypy . --ignore-missing-imports

# Run one-off scan (no bot startup) — use venv Python on VPS
~/AutoShare/venv/bin/python3 scripts/manual_scan.py

# End-to-end paper test — clears the paper book and reruns the full
# scan → rank → entry pipeline immediately (any time of day, paper only).
# Use after every code deploy instead of waiting for the next 9:30 window.
~/AutoShare/venv/bin/python3 scripts/run_e2e_paper.py

# Backfill historical data (VPS — needs valid Kite token)
~/AutoShare/venv/bin/python3 scripts/backfill_data.py --days 730

# Backfill historical corporate events for backtesting the events filter.
# Writes corporate_events_full.db — NEVER point --db at the live events DB.
~/AutoShare/venv/bin/python3 scripts/backfill_events.py --start 2021-01-01 --end 2026-07-31

# ── Validation-grade backtest (the form to use for any real decision) ──────
# --cost-model cnc : real Zerodha delivery charges (legacy default overstates
#                    small positions with ₹20/side brokerage CNC doesn't incur)
# --no-dd-halt     : the 20% drawdown halt is ABSORBING — a run that trips it
#                    is void for the remainder; use this to measure the edge
# --events-db      : read the historical snapshot, never the live events DB
# --dump-trades    : trade ledger CSV for cost-drag / run-vs-run comparison
~/AutoShare/venv/bin/python3 scripts/run_backtest.py \
    --start 2025-01-01 --end 2026-06-30 --capital 50000 --strategy vcp_v2 \
    --cost-model cnc --dump-trades /tmp/oos.csv
~/AutoShare/venv/bin/python3 scripts/run_backtest.py \
    --start 2021-06-01 --end 2024-12-31 --capital 50000 --strategy vcp_v2 \
    --cost-model cnc --no-dd-halt --events-filter --events-db corporate_events_full.db

# Typical runtimes on the 2-core VPS: ~3.2 h per OOS window, ~6.7 h per dev
# window. Run two at a time (2 cores, ~135 MB RSS each; a 2 GB swapfile was
# added 2026-08-07). Always nohup/setsid — these outlive an SSH session.

# Run backtest (VPS) — 5-year diagnostic run
~/AutoShare/venv/bin/python3 scripts/run_backtest.py --start 2021-06-01 --end 2026-06-30 --capital 500000 --strategy vcp
~/AutoShare/venv/bin/python3 scripts/run_backtest.py --start 2021-06-01 --end 2026-06-30 --capital 500000 --strategy momentum
~/AutoShare/venv/bin/python3 scripts/run_backtest.py --start 2021-06-01 --end 2026-06-30 --capital 500000 --strategy breakout
~/AutoShare/venv/bin/python3 scripts/run_backtest.py --start 2021-06-01 --end 2026-06-30 --capital 500000 --strategy mean_reversion

# Manual TOTP login (VPS — get fresh token without waiting for 8 AM)
cd ~/AutoShare && ~/AutoShare/venv/bin/python3 -c "
from broker.auth_manager import AuthManager
auth = AuthManager()
print('Token obtained:', bool(auth.login_with_totp()))
"
```

---

## Known TODOs

- Monthly report (`reports/monthly_report.py`) — implemented but not exercised in real operation yet
- Screener.in reachability from the Oracle Cloud VPS is intermittent (bursts of errno-101 failures, but ~280/501 symbols refreshed within any 7-day window as of 2026-07-29) — market cap and pledge/D-E filters pass by default for symbols whose data is missing (logged at WARNING)
- TOTP auto-login is implemented but not recommended (Zerodha ToS risk)
- No CI/CD pipeline configured
- ~~Backtest does not model regime gate~~ **Fixed 2026-07-13:** `backtest/regime_sim.py` replays the live regime rules day-by-day (BEAR blocks entries, CAUTIOUS halves sizes, 2-day upgrade confirmation). VIX-less — EXTREME/HIGH_VOLATILITY can't trigger, so it remains slightly more permissive than live.
- ~~Backtest does not check breakout confirmation~~ **Fixed 2026-07-13:** VCP path now requires the scan-day candle to be a valid pivot breakout (`is_valid_breakout`: close > pivot, strong close, 2x volume) before entering next open.
- Backtest results produced **before 2026-07-13** (including the diagnostic table below) came from the engine without these gates — treat them as signal-frequency upper bounds only.
- ~~`scanner/stock_filter.py` corporate events filter... Backtest does not call this filter~~ **Fixed 2026-08-07:** backported behind `--events-filter` (buffers shared via `stock_filter.RESULTS_BUFFER_DAYS` / `OTHER_EVENT_BUFFER_DAYS`). **Validated 2026-08-08: KEEP** — ₹50k OOS PF 1.75 → 2.22, removed 10 trades worth −₹1,051 net. Caveat: one window, 10 trades; the dev-window pair was not run.
- **OPEN — live's events filter is half dead.** Its table held **zero `results` rows** before 2026-08-07 because `refresh_corporate_events` only queries the corporate-actions endpoint, which carries no results dates; the "results ±7d" half has never fired live. The validated run used board-meeting results dates. Fix = add the board-meetings endpoint to the daily job (logic already in `scripts/backfill_events.py`). **Not done** — it tightens live off a single-window result and needs sign-off.
- **OPEN — `DIVERGENCE_INVENTORY.md` A1: live-only RS ≥ 70 gate.** `rank_setups` drops any confirmed breakout below the 70th RS percentile; the backtest vcp_v2 path has no RS gate anywhere. Highest-frequency divergence — settle it the way the events filter was settled.
- **OPEN — `DIVERGENCE_INVENTORY.md` B1: live books partial profits, backtest never does.** Live sells 33% at +20% and 33% at +30%; `backtest_engine` imports `check_partial_profit` and never calls it. Every published PF assumes winners ride to the trailing stop — exit geometry live does not use.
- ROE filter (`MIN_ROE_PCT=15%`) is **DISABLED since 2026-07-29** (`config.ENFORCE_MIN_ROE=False`): it was never part of the validated vcp_v2 ruleset and removed ~160 of the Nifty 500 from the live scan pond. Re-enable only after a backtest variant with the gate passes both validation windows.
- **Mean reversion (Connors RSI-2): REJECTED and benched (2026-07-14).** The cost warning proved exact — 5-yr gated backtest PF 0.61 (−19.1%): 50% win rate but avg win +2.5% vs ~0.5% round-trip costs and −4% avg losses. Do not revive without a variant passing both backtest windows.
- **Breakout strategy: REJECTED and benched (2026-07-14).** 5-yr gated backtest PF 0.69 (−15.6%); the old +8%/PF 1.09 bull-period result did not survive the honest engine. Same revival bar.
- **Breakout exit geometry: RULED OUT and CLOSED (2026-08-10)** — `breakout_validation_plan.md`. A pre-registered 4-case A/B at ₹50k tested whether the fixed-target exit model was what killed breakout. `breakout_v2` (no target, trailing ratchet, technical-breakdown exit, 15-day/7% time stop — vcp_v2's full exit geometry on identical entries) **failed both windows**: dev PF 0.70 / boot 3.5%, OOS PF 0.68 / boot 10.1%, against a 1.50 / 80% bar. The control reproduced the known failure (dev PF 0.65 vs July's 0.69), validating the harness. The exit change was directionally right — avg win roughly doubled, best trade went +10.88% → +97.84%, MaxDD improved 18pp — but **stop-loss exits are 82% of variant trades in both windows**, so the entry signal is the cause of death, not the exits. Per the pre-registered Case 4 rule: **do not revisit breakout exit geometry.** Revival requires a new *entry* hypothesis that lowers the stop-out rate.

## Strategy Validation Campaign (2026-07-13 → 07-17)

Regime-gated backtester with honest costs (gap-through-stop fills at open, dust guard, real cost model). Dev window 2021-06→2024-12; **untouched validation window 2025-01→2026-06**; Nifty 500 PIT universe (survivorship caveat: only current membership known).

| Strategy | Dev PF | Validation PF | Verdict |
|---|---|---|---|
| momentum v1→v4 (best: v3) | 0.99 | **0.55** | REJECTED — dev gains were window-fitted |
| breakout | 0.69 | — | REJECTED |
| mean_reversion | 0.61 | — | REJECTED |
| VCP v1 | 0 trades | — | structurally broken (see below) |
| **vcp_v2 (pivot watch)** | **1.11** (224 trades) | **1.61** (43 trades, edge confirmed 85.6%, MaxDD −4.4%) | **VALIDATED — sole live engine** |

Structural bugs found and fixed during the campaign (each a commit):
1. `swing_highs`/`swing_lows` used `==` on the left window — an exact float-tie requirement that disabled swing detection since the project began; VCP had literally never fired (`5be5035`).
2. VCP v1 required setup + breakout on the same candle — impossible by construction; vcp_v2 splits the stages via a persistent pivot watch (`f4a9213`, live port `5c52a28`).
3. Trade ledger stamped every exit `vcp` regardless of strategy (`ed59ca6`); NIFTY50 index was silently never backfilled (`74b5ac3`); stop fills ignored gap-downs and dust positions hid fixed-cost losses (`843a912`).

### Sizing study (2026-07-17, in progress)

Same validated vcp_v2 trades at larger size — position cap binds before risk % at a 7% stop, so both dials move together (`--risk-pct/--max-pos-pct` on `run_backtest.py`):

| Profile | Dev CAGR / PF / MaxDD | Validation CAGR / PF / MaxDD |
|---|---|---|
| 1.5%/15% (baseline) | +2.9% / 1.11 / −18.4% | +3.3% / 1.61 / −4.4% |
| 2.0%/20% | **+7.5% / 1.23 / −19.0%** | **+4.8% / 1.73 / −5.0%** (edge confirmed) |
| 2.5%/25% | **DISQUALIFIED** — breached the 20% DD halt (Apr 2023) | — |

PF *improves* with size (fixed costs shrink proportionally; cash constraint drops marginal entries). Dev MaxDD −19.0% is brushing the 20% halt — likely the ceiling constraint. **Settled: live runs 2.0%/20%.**

### Pilot-scale re-validation (2026-08-08) — `VALIDATION_2026_08.md`

Same methodology at pilot capital, ₹6,000 floor, real CNC costs (`--cost-model cnc`), dev runs with `--no-dd-halt`:

| Capital | Dev PF / MaxDD | OOS PF / bootstrap / MaxDD |
|---|---|---|
| ₹50k | 1.19 / −19.78% (boot 73.4%) | **1.75 / 84.7% / −4.35%** |
| ₹1L | not run | **1.84 / 89.3% / −4.69%** |
| ₹5L (control) | not run | 1.77 / 88.6% / −4.92% |

₹50k OOS ≈ ₹5L OOS: cost drag at ₹6k positions costs ~0.02 PF. Dev-window MaxDD −19.78% brushes the 20% halt — an operational risk that argues for reaching ₹1L.

---

## Important Notes for Future AI Sessions

0. **`WHOLE_STRATEGY.md` is a living plain-language explainer the user hands to third parties for analysis. Whenever you change the strategy, filters, sizing, regime rules, universe, or live status — or a notable pilot event occurs — update it (including its funnel numbers, watchlist count, scoreboard, and changelog) in the same commit.**

1. **Start by running `python scripts/validate_setup.py`** — it confirms all dependencies, databases, and env vars are correctly configured.
2. **Check `git log` first** — recent commits tell you what was last changed.
3. **The `data/` directory is gitignored** — databases and logs are not in version control. On a fresh clone, run `setup_db.py` then `backfill_data.py`.
4. **Two separate processes run on VPS:** `main.py` (trading bot) and `broker/auth_server.py` (Flask OAuth callback). Both managed by supervisord.
5. **All Telegram messages in paper mode are prefixed `[PAPER]`** — this is enforced in `alerts/telegram_alert.py`.
6. **Fundamental data (Screener.in) may be unavailable from the VPS.** The filter passes silently in that case — this is intentional and logged.
7. **Never disable the regime check before enabling new trades.** The regime gate is the primary safety circuit.
8. **`config.py` has rationale comments on every constant.** Read them before changing any threshold.
9. **Never run a backfill into a live DB.** `scripts/backfill_events.py` writes `corporate_events_full.db` by default for exactly this reason — on 2026-08-07 a run into the live `corporate_events.db` silently activated the dormant "results ±7d" half of filter 5 and blocked 44 of 500 universe symbols. Backtests select it with `--events-db corporate_events_full.db`.
10. **Backtest runs that trip the 20% drawdown halt are void.** The halt is absorbing (blocks entries → equity can't grow → drawdown never shrinks → never lifts). Check for `Drawdown halt:` in the run log; re-run with `--no-dd-halt` to measure the underlying edge and report the halt-on run separately as the operational outcome. **The same trap exists in live.**
11. **Use `--cost-model cnc` for any decision-relevant backtest.** The `legacy` default charges ₹20/side brokerage that CNC delivery does not incur; it stays the default only so published numbers don't shift. The error is ~0.31pp of a ₹10k position and ~0.03pp of a ₹1L one, so it distorts small-capital questions specifically.

### Documentation map

| File | Purpose |
|---|---|
| `WHOLE_STRATEGY.md` | Plain-language explainer for third parties — keep current (see note 0) |
| `VALIDATION_2026_08.md` | Pilot-scale re-validation results (₹50k / ₹1L / ₹5L) |
| `DUST_FLOOR_DECISION.md` | Real CNC cost math behind the ₹6,000 minimum-position floor |
| `DIVERGENCE_INVENTORY.md` | Live-vs-backtest rule differences, severity-ranked (A1 RS gate + B1 partial profits are open) |
| `GO_LIVE.md` | Live pilot runbook |
| `docs/WINNING_STRATEGY.md` | Exit-plan study 2026-10-11: current exits vs my_strategy (sell 50% at +10%). Pre-registered portfolio test: **current exits stay**, my_strategy rejected (2025–26 PF 1.01 vs 1.75); 1%-risk/8-position setup earned ~0 |
| `docs/XYZ_TRADE_EXAMPLE.md` | Plain-language ₹100 walk-through of both exit plans |
| `TODO.md` / `ARCHITECTURE.md` / `DEPLOYMENT.md` | Backlog, design, VPS setup |
