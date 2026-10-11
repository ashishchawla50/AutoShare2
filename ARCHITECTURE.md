# ARCHITECTURE.md — AutoShare Trading Bot

> ⚠️ **Partially outdated (written in the two-strategy paper era).** Since
> 2026-07-16 the sole live engine is the two-stage VCP (`vcp_v2`: pivot
> watchlist → confirmed-breakout entry); momentum/breakout/mean-reversion are
> benched; the universe is Nifty 500; the deployment is LIVE (₹50k pilot).
> `CLAUDE.md` and `GO_LIVE.md` are authoritative where this file disagrees.
> The scheduler-flow, database, and process diagrams below remain accurate.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Ubuntu VPS (Oracle Cloud)                 │
│                        ubuntu@152.67.10.33                       │
│                                                                   │
│  ┌────────────────────────────┐  ┌──────────────────────────┐   │
│  │   trading_bot (main.py)    │  │  auth_flask              │   │
│  │   (Supervisord process 1)  │  │  (broker/auth_server.py) │   │
│  │                            │  │  (Supervisord process 2) │   │
│  │  TradingBot orchestrator   │  │  Flask :8080             │   │
│  │  APScheduler (IST cron)    │  │  /callback               │   │
│  │  HTTP health :8081         │  │  /status                 │   │
│  └────────────┬───────────────┘  └──────────┬───────────────┘   │
│               │                             │                     │
│               │         data/.access_token.enc (shared file)     │
│               └──────────────────────────────┘                   │
│                                                                   │
│  data/          6× SQLite databases                              │
│  logs/          7 domain-specific log files (JSON)               │
└─────────────────────────────────────────────────────────────────┘
         │ Kite Connect API             │ Telegram Bot API
         │ (market data + orders)       │ (alerts + reports)
         ▼                             ▼
   Zerodha Kite                   Telegram
         │
         │ NSE/BSE data
   ┌─────┴──────────────────────────────────────────┐
   │  External data sources                          │
   │  - NSE website (universe, surveillance)         │
   │  - Screener.in (fundamentals) [intermittent]   │
   └────────────────────────────────────────────────┘
```

---

## Folder Responsibilities

| Folder | Responsibility | Touches DB? |
|---|---|---|
| `config/` | Single source of truth for all constants and secrets; holiday calendar | No |
| `utils/` | Cross-cutting: logging, encryption, health check, indicator math, time | Heartbeat file |
| `broker/` | Zerodha API wrapper, session management, order/portfolio management | `portfolio_state.db` |
| `data_fetcher/` | Fetch and cache market data from external sources | `market_data.db`, `fundamentals.db`, `surveillance.db`, `corporate_events.db` |
| `strategy/` | Pure pattern-detection algorithms (VCP, Trend Template, Stage, Breakout, Momentum) | No |
| `scanner/` | Orchestrates filters + RS scoring + ranking; produces ranked `TradeSetup` list | No |
| `risk/` | Position sizing, drawdown tracking, entry approval | `portfolio_state.db` (loss_tracking) |
| `regime/` | 5-state market regime detection; persists upgrade state | `regime_history.db` |
| `exit/` | Stop loss, trailing ratchet, partial profit, time stop logic | No (calls portfolio_manager) |
| `portfolio/` | Portfolio state snapshots and P&L calculations | `portfolio_state.db` (portfolio_metrics) |
| `backtest/` | Event-driven backtester, cost model, performance metrics | `market_data.db` (reads) |
| `reports/` | Generate formatted Telegram daily/weekly/monthly reports | Reads `portfolio_state.db` |
| `alerts/` | All outbound Telegram messages; paper mode prefix injection | No |
| `scripts/` | One-off operational scripts (setup, backfill, validate, scan) | All |

---

## Daily Workflow (Request / Data Flow)

All times are IST (Asia/Kolkata). Only runs on NSE trading days.

```
05:30  universe_snapshot_refresh
         NSE → fetch_universe_symbols() (Nifty 500 since 2026-07-14) → save_universe_snapshot() → data in DB

06:00  fundamentals_refresh
         Screener.in → fundamental_fetcher → fundamentals.db
         (Reachability from Oracle Cloud is intermittent; filters pass by
          default for symbols with missing data. ROE gate disabled 2026-07-29.)

07:00  surveillance_refresh
         NSE → surveillance_fetcher → surveillance.db (ASM/ESM/T2T lists)

07:30  corporate_events_refresh
         NSE → event_fetcher → corporate_events.db (10-day lookahead)

08:00  auth_login_prompt
         AuthManager → generate login URL → Telegram
         [Manual: operator opens URL, logs in, Zerodha redirects to :8080/callback]
         [OR: TOTP auto-login if TOTP_AUTO_LOGIN_ENABLED=true]
         Flask callback → exchange request_token → encrypt → data/.access_token.enc

08:30  universe_ohlcv_refresh
         Kite API → OHLCVFetcher → bulk OHLCV for the full universe (Nifty 500) → market_data.db
         Loads into TradingBot._universe_ohlcv (in-memory dict)

08:45  pre_scan_filters
         _universe_ohlcv → 9 sequential filters → pruned set of candidates
         Filters: price, market cap, liquidity, listing age, pledge, debt,
                  circuit hits, missing data, ASM/ESM surveillance, upcoming events
         Corporate events buffer: 7 days before results, 3 days before ex-dividend/bonus/split
         (tightened from 21/21 on 2026-07-08 — was eliminating ~33% of stock-days unnecessarily)

09:00  strategy_scan (VCP)
         Filtered candidates → for each stock:
           multi_timeframe.analyse_stock()
             → stage_analysis.is_stage_2()
             → trend_template.passes_trend_template() [all 8 conditions]
             → vcp_detector.detect_vcp()
                 Range violations: max 1 allowed (tightened from 0 on 2026-07-08)
                 If 1 violation: ATR compression ratio < 0.7 required as objective gate
                 If 0 violations: passes regardless of ATR ratio
             → tightness_score must be > 50 (hard gate in multi_timeframe.py)
           → RS percentile filter (> 70th percentile vs Nifty)
           → build TradeSetup, add to candidates list
         rank_setups() → composite score ranking → _watchlist

09:05  momentum_scan
         Filtered candidates → for each stock:
           momentum_strategy.check_momentum() [4 conditions: EMA, volume, RSI, proximity to high]
           → build TradeSetup(strategy="momentum"), append to _watchlist

09:10  regime_check_premarket
         RegimeDetector(universe_ohlcv, nifty_ohlcv).detect()
           → breadth: fraction of universe above 200 EMA
           → Nifty close vs EMA200 + higher-highs check
           → India VIX (from Kite quote + NSE fallback)
           → 5-state classification with 2-day upgrade confirmation
           → persist to regime_history.db
           → send Telegram alert on regime change

09:15  rank_opportunities
         Regime gate: if BEAR or EXTREME_VOL → clear watchlist, no entries
         VCP slots: max 2 open VCP positions (config.MAX_POSITIONS)
         Momentum: top 10 by composite score (capital-limited at entry time)
         _watchlist = vcp_top + momentum_top

09:20  gtt_verify
         For every open position in DB → verify GTT exists in Kite
         Missing GTT → re-place automatically (live mode only)

09:20+  gtt_backup (every 60s, 09:00–15:30)
         GTTBackupMonitor.run_once() → safety net: re-place any missing GTTs

──── Market Open (09:15 NSE) ────────────────────────────────────────────

09:30–10:30  check_entries (every 5 minutes)
               Entry window only (09:30–10:30)
               For each setup in watchlist:
                 1. Check manual blocklist (data/manual_blocklist.txt)
                 2. Evaluate today's open vs pivot (gap check)
                 3. Calculate position size (risk-based: 2.0% of BOT trading
                    equity — starting capital + bot P&L, never the whole
                    Zerodha account value; see 2026-07-31 incident)
                 4. Regime multiplier (0.5× in CAUTIOUS, 1.0× in BULL)
                 5. Risk manager: sector cap (30%), concentration check
                 6. Place MARKET buy order (CNC, NSE)
                 7. Place GTT stop-loss (Kite GTT, survives restarts)
                 8. Record in portfolio_state.db (open_positions)
                 9. Send Telegram trade-entry alert

09:30–15:30  check_exits (every 5 minutes)
               For every open position:
                 VCP exits:
                   - Hard stop (below stop_loss) → MARKET sell
                   - Technical breakdown (close below consolidation low)
                   - Time stop (15 days with < 7% profit)
                   - Regime exit (BEAR/EXTREME_VOL with days_since_bear)
                   - Trailing ratchet stop (4 tiers: 10/15/20/30% profit)
                   - Partial profit (3 tiers: sell partial at defined thresholds)
                 Momentum exits:
                   - Hard stop (5% below entry) → MARKET sell
                   - Profit target (12% above entry) → LIMIT sell
                   - Time stop (12 trading days)

09:00–15:30  regime_intraday (every 30 minutes)
               Re-run regime detection intraday; immediate downgrade if triggered

09:00–15:30  heartbeat (every 30 minutes)
               record_heartbeat() → data/.last_heartbeat
               Stale heartbeat during market hours → Telegram dead man's switch alert

──── Market Close (15:30 NSE) ───────────────────────────────────────────

15:30  market_close_cleanup
         Cancel all pending OPEN/TRIGGER_PENDING orders (live mode only)

15:45  eod_report
         Portfolio snapshot → generate_daily_report() → Telegram
         Record portfolio_metrics row (daily)
         Check inactivity warning (>25 days without a trade → SEBI sweep alert)

16:30  database_backup
         All 6 SQLite DBs → data/backups/ (rotating)

Monday 07:00  weekly_report
         PortfolioTracker → weekly P&L, trades, regime → Telegram
```

---

## Authentication Flow

```
Daily 8 AM
    │
    ├─ TOTP_AUTO_LOGIN_ENABLED=false (default — ToS compliant)
    │       AuthManager.get_login_url()
    │       → Telegram: "Open this URL to log in"
    │       → Operator opens URL in browser
    │       → Zerodha redirects to http://VPS_IP:8080/callback?request_token=XXX
    │       → Flask auth_server.py: exchange_request_token(XXX)
    │       → Fernet encrypt → data/.access_token.enc
    │
    └─ TOTP_AUTO_LOGIN_ENABLED=true (opt-in — ToS risk accepted)
            AuthManager.login_with_totp()
            → POST https://kite.zerodha.com/api/login (user_id + password)
            → POST https://kite.zerodha.com/api/twofa (TOTP code from pyotp)
            → GET login_url (redirected to callback with request_token)
            → exchange_request_token() → same as above

TradingBot startup:
    AuthManager.get_access_token()
    → load_access_token() (Fernet decrypt data/.access_token.enc)
    → KiteClient.connect(token) → kite.profile() to validate
```

---

## Database Relationships

The 6 databases are **independent** — no foreign keys cross databases. All joins happen in application code.

```
market_data.db
  ohlcv_daily          (symbol, date) UNIQUE INDEX
  ohlcv_data_quality   (symbol, check_date)

fundamentals.db
  fundamentals         (symbol) UNIQUE
  shareholding_quarterly (symbol, quarter)

corporate_events.db
  corporate_events     (symbol, ex_date) INDEX

surveillance.db
  surveillance_list    (symbol, list_type, date_added)

portfolio_state.db
  trades               (id) — closed trade records
  open_positions       (symbol) UNIQUE — current holdings
  portfolio_metrics    (date) UNIQUE — daily snapshot
  loss_tracking        (date) — drawdown + consecutive loss state

regime_history.db
  regime_log           (timestamp) — every regime detection run
```

---

## Background Jobs (APScheduler)

All jobs use `CronTrigger`, `coalesce=True`, `max_instances=1`, `misfire_grace_time=300s`.
All wrapped in `_trading_day_only()` decorator (skips weekends/holidays) except reports and backup.

| Job ID | Cron (IST) | Method | Trading Day Only? |
|---|---|---|---|
| `universe_snapshot_refresh` | `30 5 * * *` | `refresh_universe_snapshot()` | Yes |
| `fundamentals_refresh` | `0 6 * * *` | `refresh_fundamentals()` | Yes |
| `surveillance_refresh` | `0 7 * * *` | `refresh_surveillance()` | Yes |
| `corporate_events_refresh` | `30 7 * * *` | `refresh_corporate_events()` | Yes |
| `auth_login_prompt` | `0 8 * * *` | `send_login_url()` | Yes |
| `universe_ohlcv_refresh` | `30 8 * * *` | `refresh_universe_ohlcv()` | Yes |
| `pre_scan_filters` | `45 8 * * *` | `run_pre_scan()` | Yes |
| `strategy_scan` | `0 9 * * *` | `run_strategy_scan()` | Yes |
| `momentum_scan` | `5 9 * * *` | `run_momentum_scan()` | Yes |
| `regime_check_premarket` | `10 9 * * *` | `check_regime()` | Yes |
| `rank_opportunities` | `15 9 * * *` | `rank_opportunities()` | Yes |
| `gtt_verify` | `20 9 * * *` | `verify_gtt_orders()` | Yes |
| `gtt_backup` | `* 9-15 * * *` | `_gtt_monitor.run_once()` | Yes |
| `exit_check` | `*/5 9-15 * * *` | `check_exits()` | Yes |
| `entry_window` | `*/5 9-10 * * *` | `check_entries()` | Yes |
| `regime_intraday` | `*/30 9-15 * * *` | `check_regime()` | Yes |
| `heartbeat` | `*/30 9-15 * * *` | `send_heartbeat()` | Yes |
| `market_close_cleanup` | `30 15 * * *` | `market_close()` | Yes |
| `eod_report` | `45 15 * * *` | `generate_eod_report()` | No |
| `database_backup` | `30 16 * * *` | `backup_databases()` | No |
| `weekly_report` | `0 7 * * 1` | `generate_weekly_report()` | No |

---

## Services

### KiteClient (`broker/kite_client.py`)
- Wraps `kiteconnect.KiteConnect`
- Two global `TokenBucket` rate limiters: 10 orders/sec, 200 historical/min
- Paper mode intercept: order/GTT placement returns fake IDs without touching Kite
- Market data reads (OHLCV, quotes) always go to real Kite even in paper mode
- Exponential backoff retry (up to 3 attempts) for network errors only

### OHLCVFetcher (`data_fetcher/ohlcv_fetcher.py`)
- Primary: Kite `historical_data()` API
- Cache: SQLite `market_data.db` (upsert by symbol+date)
- Bulk fetch: processes the entire universe (Nifty 500) in one sweep each morning

### RegimeDetector (`regime/regime_detector.py`)
- Runs on fresh Nifty OHLCV + universe OHLCV
- Breadth: fraction of universe stocks above their 200-day EMA
- VIX: Kite quote (primary) → NSE HTTP API (fallback)
- 2-day upgrade confirmation state persisted to `data/.regime_upgrade_state` (JSON)
- Every detection result written to `regime_history.db`

### GTTBackupMonitor (`broker/gtt_monitor.py`)
- Runs every 60 seconds during market hours (9:00–15:30)
- Verifies every open position in DB has an active GTT in Kite
- Re-places missing GTTs automatically — safety net for connectivity gaps

### AuthServer (`broker/auth_server.py`)
- Flask server on port 8080 (always-on via supervisord)
- `/callback` — receives Zerodha OAuth redirect, exchanges token, encrypts and stores
- `/status` — returns `{authenticated, paper_mode}` JSON
- `/logout` — invalidates session

### HealthCheck (`utils/health_check.py`)
- Heartbeat file: `data/.last_heartbeat` written every 30 min during market hours
- HTTP server on port 8081 — for external uptime monitoring (UptimeRobot etc.)
- Emergency halt flag: `data/emergency_halt.flag` — blocks all new entries when present

---

## Middleware / Cross-Cutting Concerns

| Concern | Implementation |
|---|---|
| Logging | `utils/logger.py` — JSON structured, domain-routed to 7 log files |
| Rate limiting | `TokenBucket` in `broker/kite_client.py` — shared global instances |
| Paper mode | Config flag checked in every order/GTT method in `KiteClient` |
| Holiday check | `config/holidays.py` — `_trading_day_only()` wrapper on every scheduler job |
| Token encryption | `utils/encryption.py` — Fernet symmetric encryption |
| IST timezone | `utils/time_utils.py` — all datetime ops use `pytz.timezone("Asia/Kolkata")` |

---

## External Integrations

| Service | Purpose | Failure Behaviour |
|---|---|---|
| **Zerodha Kite Connect** | OHLCV data, LTP quotes, order placement, GTT management, portfolio | Bot continues with cached data; orders fail gracefully |
| **Telegram Bot API** | All alerts, reports, login URL | Silent failure logged; bot continues |
| **NSE website** | Nifty 500 universe, ASM/ESM surveillance lists | Falls back to Nifty 200 CSV/API, then hardcoded ~176-name list; logged at WARNING |
| **Screener.in** | Fundamentals (market cap, D/E, pledge) | Reachability intermittent from the VPS; filters pass by default for symbols with missing data; logged at WARNING |
| **India VIX** | VIX from Kite quote (primary) + NSE API (fallback) | VIX treated as None; regime detection degrades gracefully |

---

## Design Patterns Used

| Pattern | Where |
|---|---|
| **Dataclass return types** | All multi-field results: `VCPResult`, `TrendTemplateResult`, `RegimeState`, `PositionSizeResult`, `TradeSetup`, `MomentumResult`, `FilterResult` |
| **Token bucket rate limiting** | `broker/kite_client.py` — shared global instances |
| **Decorator for day filtering** | `_trading_day_only()` in `scheduler.py` |
| **Strategy pattern** | Two strategies (`"vcp"` / `"momentum"`) share `TradeSetup` dataclass and the same entry/exit orchestration with strategy-specific branching |
| **State machine** | `RegimeDetector` — 5 states, 2-day upgrade confirmation with file-persisted pending state |
| **Repository pattern (light)** | `PortfolioManager`, `OHLCVFetcher` abstract SQLite reads/writes |
| **Dead man's switch** | `health_check.py` — heartbeat file + staleness check + Telegram alert |
| **Emergency halt** | File-based flag (`data/emergency_halt.flag`) checked on every entry attempt |
| **Manual blocklist** | File re-read on every entry cycle (hot-reload without restart) |
| **GTT ratchet stop** | Stop loss is monotonically increasing; any downward move is logged as WARNING |
| **Reconcile on startup** | `_reconcile_holdings()` — Kite holdings are authoritative; local DB positions not in Kite are marked closed |

---

## Areas That Need Refactoring / Known Weaknesses

| Area | Issue |
|---|---|
| `main.py` is large | `TradingBot` has ~15 public methods and ~900 lines. Could be split into `EntryManager`, `ExitManager`, `DataManager` |
| `check_entries()` is complex | ~100 lines inline; VCP and momentum branching could be separated into dedicated methods |
| Fundamental data reliability | Screener.in reachability from the Oracle Cloud VPS is intermittent (errno-101 bursts); no alternative source implemented |
| No async I/O | All Kite API calls are synchronous; OHLCV bulk fetch is serial (500 stocks × rate limiter delay ≈ several minutes) |
| nsepy unreliable for indices | Nifty OHLCV falls back to Kite token; nsepy dependency is present but not used for indices |
| No CI/CD | Tests run locally or manually; no GitHub Actions / automated pipeline |
| Monthly report | `reports/monthly_report.py` implemented but not battle-tested in production |
| `scanner/sector_regime.py` | Implemented but not yet called in the main workflow |
| ~~Backtest misses regime gate~~ | **Fixed 2026-07-13:** `backtest/regime_sim.py` replays the live regime rules day-by-day (VIX-less — slightly more permissive than live). |
| ~~Backtest skips breakout confirmation~~ | **Fixed 2026-07-13:** the vcp path requires a valid pivot breakout on the scan-day candle before entering next open (the vcp_v2 two-stage flow). |
| Live-only filters diverge from backtest | The 9 pre-scan stock filters (price, liquidity, corporate events, etc.) are not applied in the backtest — live trades a slightly stricter pond. Unvalidated ROE and share-count gates were removed 2026-07-29 to narrow this gap. |
| Backtest excludes events filter | `apply_all_filters()` is not called during backtesting; corporate events blocking only applies in live mode. Live frequency will be modestly lower than backtest. |
