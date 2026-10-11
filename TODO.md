# TODO.md — AutoShare Trading Bot

Last updated: 2026-08-10. Authoritative current state lives in `CLAUDE.md`.

---

## 2026-08-06 → 08-08: Fidelity work — floors, costs, isolation, re-validation

- [x] Live order-path hardening (`c062ac4`): fill-confirmed exits, mandatory GTT protection, real fill prices in the ledger
- [x] **Dust floor decided from real CNC cost math** (`DUST_FLOOR_DECISION.md`): the backtest's ₹30k floor was never cost-justified (round-trip crosses 0.5% only below ~₹5,700); replaced by a shared `config.MIN_POSITION_VALUE = ₹6,000` enforced in **both** engines — live previously had no floor at all
- [x] **Test isolation fixed** (`tests/conftest.py` + `test_conftest_isolation.py`): all data paths redirect to `tmp_path`; the 2026-08-06 regime-poisoning vector is closed and the 3 poisoned rows purged. 163/163 pass
- [x] **Events filter backported** to the backtest (`--events-filter`), historical events backfilled to `corporate_events_full.db` (69,648 rows), live events DB kept separate
- [x] **Backtest bug: absorbing drawdown halt** — fired 2022-02-14 then all 737 remaining days, voiding 3 of 3.5 dev years. `--no-dd-halt` added
- [x] **Backtest bug: phantom brokerage** — ₹20/side intraday rate charged on free CNC delivery; `--cost-model cnc` added (legacy stays default so published numbers don't shift)
- [x] **Pilot-scale re-validation PASSED** (`VALIDATION_2026_08.md`): ₹50k OOS PF 1.75 / bootstrap 84.7% vs 1.77 at ₹5L; ₹1L OOS 1.84 / 89.3%
- [x] **Events filter verdict: KEEP** — ₹50k OOS PF 1.75 → 2.22
- [x] Divergence inventory (`DIVERGENCE_INVENTORY.md`) — 16 live-only gates catalogued

**Open, in priority order:**

- [ ] **A1 — live-only RS ≥ 70 gate** (`rank_setups`): fires on every candidate, exists nowhere in the validated engine. Run both windows with it on/off, then keep in both or delete from live
- [ ] **B1 — partial profit booking**: live sells 33% at +20% / 33% at +30%; the backtest never calls `check_partial_profit`. Every published PF assumes winners ride to the trailing stop
- [ ] **Live events filter is half dead** — no `results` rows ever reached it; add the NSE board-meetings endpoint to `refresh_corporate_events`. Needs sign-off (tightens live off a single-window result)
- [ ] Events filter on the **dev** window (`c50k_dev_ev_nh`) — dropped for runtime, would confirm the KEEP verdict
- [ ] Consider making the 20% drawdown halt recoverable in **live** — currently unrecoverable without manual intervention, and the ₹50k dev window comes within 0.2pp of it

---

## 2026-07-27 → 08-03: Live pilot — first weeks (summary)

- [x] EOD daily report crash fixed (`8167e8b`) — was failing nightly since go-live
- [x] Nightly backup purge TypeError fixed (`4a3ec5e`) — backups created but never purged
- [x] Scan pond restored to validated width (`5816651`): ROE gate disabled, share-count liquidity floor removed → pre-scan ~141 → ~449 of the Nifty 500 (entry rules untouched)
- [x] **2026-07-31 incident:** first live signal (SONACOMS) lost — sizing used whole Zerodha account (~₹5L incl. personal holdings) instead of bot's ₹50k trading equity; cash gate blocked the oversized order and the pivot was dropped
- [x] Fix deployed (`a8e5d90`): `get_portfolio_value()` = bot trading equity (mode-filtered P&L); risk-blocked vcp entries restore their pivot; SONACOMS pivot manually restored (watch until 08-31)
- [ ] First live trade — machinery verified end-to-end through the risk gate; waiting on a valid 2x-volume pivot breakout (15 pivots on watch as of 08-03)
- [ ] Zerodha 30-day inactivity sweep deadline ~2026-08-19 if still no trade (bot warns via Telegram; harmless)

---

## 2026-07-13 → 07-19: Validation campaign & go-live (summary)

- [x] Regime-gated, honest-cost backtester (regime sim, gap-fills, dust guard)
- [x] All four launch strategies tested: momentum/breakout/mean-reversion REJECTED (benched); VCP v1 found structurally unable to trade
- [x] Root-cause fixes: swing-detection `==` bug, two-stage pivot watch (vcp_v2), ledger strategy attribution, NIFTY50 backfill lookup
- [x] vcp_v2 validated out-of-sample (PF 1.61, edge confirmed) and ported to the live bot
- [x] Sizing study → 2.0% risk / 20% cap (2.5%/25% breached the DD halt — disqualified)
- [x] Live-mode hardening: daily reconcile (T+1-aware, partial sells), GTT hygiene, `manual_exit.txt` override
- [x] **LIVE 2026-07-20** — ₹50k plumbing pilot (`GO_LIVE.md`)
- [ ] Pilot: first-trade watch protocol on first live entry
- [ ] Promotion gate to ₹1L: ≥3 months / ≥30 live trades, behavior matches backtest logic

---

## Completed Features

### Core Infrastructure
- [x] `TradingConfig` dataclass — all constants with rationale comments
- [x] NSE holiday calendar (`config/holidays.py`)
- [x] JSON structured logging, domain-specific log files, 90-day rotation
- [x] Fernet encryption for Zerodha access token storage
- [x] Heartbeat + dead man's switch + HTTP health server (port 8081)
- [x] IST timezone utilities (`utils/time_utils.py`)
- [x] Manual blocklist hot-reload (`data/manual_blocklist.txt`)
- [x] Emergency halt flag (`data/emergency_halt.flag`)

### Broker / Auth
- [x] `KiteClient` — token bucket rate limiter (10 orders/sec, 200 historical/min)
- [x] Paper mode intercept on all order/GTT calls
- [x] Exponential backoff retry for network errors
- [x] `AuthManager` — OAuth flow (manual URL via Telegram)
- [x] `AuthManager` — TOTP automated login (active on VPS: `TOTP_AUTO_LOGIN_ENABLED=true`)
- [x] Flask auth server (`auth_server.py`) on port 8080
- [x] `OrderManager` — buy CNC, sell CNC, GTT placement/update/delete
- [x] `PortfolioManager` — open/close positions, cash tracking, inactivity warning
- [x] `GTTBackupMonitor` — re-place missing GTTs every 60 seconds
- [x] Startup reconciliation — Kite holdings vs local DB (handles offline GTT fires)

### Data Fetching
- [x] OHLCV fetch via Kite + SQLite cache (`market_data.db`)
- [x] Bulk OHLCV for full Nifty 200 universe
- [x] Nifty 200 universe from NSE with snapshot versioning
- [x] ASM/ESM/T2T surveillance from NSE (`surveillance.db`)
- [x] Fundamentals from Screener.in (market cap, D/E, pledge, sector)
- [x] Corporate events (results, dividends, bonus, splits) with 10-day lookahead

### Strategy
- [x] Weinstein Stage Analysis (Stage 1/2/3/4 classification)
- [x] Minervini Trend Template (all 8 conditions required)
- [x] VCP detector (contraction count, volume contraction, tightness score, ATR compression)
- [x] Breakout detector + gap evaluation (skip/proceed decision)
- [x] Multi-timeframe analysis combining Stage + Trend Template + VCP → `is_high_confidence`
- [x] Momentum strategy (4-condition EMA crossover, 12% target, 5% stop, 12-day time stop)

### Scanner
- [x] 9 sequential stock filters: price, market cap, liquidity, listing age, promoter pledge, debt/equity, circuit hits, missing data, ASM/ESM + upcoming events
- [x] O'Neil RS percentile scoring vs Nifty 50 (70th+ percentile required)
- [x] Opportunity ranker — composite score, `TradeSetup` dataclass
- [x] `select_top_setups()` — VCP slot management (max 2 VCP positions)

### Risk
- [x] Risk-based position sizing (1.5% risk / 15% max position / 20% cash reserve)
- [x] Regime multiplier (0.5× in CAUTIOUS/HIGH_VOL, 0.0 in BEAR/EXTREME_VOL)
- [x] Daily / weekly / monthly drawdown monitoring
- [x] Maximum drawdown halt (20% from peak)
- [x] Anti-revenge trading pause (3 consecutive losses → 1 day, 5 → 3 days)
- [x] Sector exposure cap (30%)

### Regime
- [x] 5-state regime: BULL / CAUTIOUS / BEAR / HIGH_VOLATILITY / EXTREME_VOLATILITY
- [x] Breadth calculation (fraction of Nifty 200 above 200 EMA)
- [x] India VIX fetching (Kite primary + NSE fallback)
- [x] 2-day upgrade confirmation (prevents whipsaws)
- [x] Regime state persisted to `regime_history.db`
- [x] Pending upgrade state persisted across restarts (`data/.regime_upgrade_state`)
- [x] Regime change Telegram alert

### Exit
- [x] GTT hard stop (7% below entry for VCP, 5% for momentum)
- [x] Trailing stop ratchet (4 tiers: 10/15/20/30% profit → stop to 0/+5/+10/+20%)
- [x] Dynamic floor stop: max(ratchet, 10-day EMA × 0.97)
- [x] Partial profit booking (3 tiers)
- [x] Time stop (15 days with < 7% profit for VCP; 12 trading days for momentum)
- [x] Technical breakdown exit (close below consolidation low)
- [x] Regime-triggered exit (staged exit in BEAR/EXTREME_VOL based on days since bear start)

### Backtest
- [x] Event-driven backtest engine
- [x] Performance analyzer (CAGR, Sharpe, max drawdown, win rate, profit factor)
- [x] Point-in-time universe (no look-ahead bias)
- [x] Cost model (brokerage + STT + exchange charges)
- [x] Per-gate kill-rate instrumentation: VCP path (fail_tt / fail_vcp / fail_tightness / fail_other), Momentum path (mom_fail_ema / mom_fail_volume / mom_fail_proximity / mom_fail_rsi)

### Reports & Alerts
- [x] All Telegram alert types: trade entry, exit, partial profit, trailing stop update, regime change, heartbeat/error alerts, login URL, inactivity warning
- [x] Paper mode prefix `[PAPER]` on all Telegram messages
- [x] Daily EOD report via Telegram
- [x] Weekly report (Monday 7 AM) via Telegram
- [x] Monthly report — code complete (not yet battle-tested in production)

### Operations
- [x] `setup.sh` — Ubuntu 22.04 one-shot VPS setup script
- [x] `supervisord.conf` — two-process process management
- [x] `scripts/setup_db.py` — initialize all 6 SQLite databases
- [x] `scripts/validate_setup.py` — pre-launch checklist (8 check categories)
- [x] `scripts/backfill_data.py` — seed historical OHLCV from Kite
- [x] `scripts/manual_scan.py` — one-off scan without running the full bot
- [x] Database backup utility

### Testing
- [x] 90 pytest tests, all passing (as of 2026-07-08)
- [x] `conftest.py` — paper mode forced on all tests, Telegram disabled
- [x] Tests for: strategy (VCP, trend template), filters, risk, regime, backtest, momentum, VCP detector
- [x] ATR gate unit tests: zero-violation passes without ATR compression; one-violation requires ATR < 0.7; one-violation+ATR passes

---

## Features Currently In Progress

- [ ] **Paper trading validation** — started ~June 24, 2026. Must run ≥2 months before live mode. ETA: September 2026.
- [x] **Backtest diagnostic run** — 5-year VCP + momentum backtests run on VPS 2026-07-08 (`--start 2021-06-01 --end 2026-06-30 --capital 500000`). Kill-rate instrumentation active. Use for frequency diagnosis only — not valid for win rate / drawdown (backtest has no regime gate and no breakout confirmation).

---

## Backtest Kill-Rate Findings (2026-07-08)

### Momentum — COMPLETE

```
data_ok=4668  analysed=4668
momentum killed: ema=3856 (82.6%)  volume=699 (15.0%)  rsi=18 (0.4%)  proximity=0
high_conf=95  gap_filtered=5  size_invalid=1  entered=89
```

**Primary bottleneck: EMA stack (82.6%).** The 4-condition EMA alignment check rejects 5 in 6 candidates. Next action if frequency is too low: loosen EMA conditions or add a second momentum tier.

### VCP — RESOLVED (2026-07-14 → 07-16)

That run returned **zero trades** — not a frequency problem but a structural
contradiction: the breakout candle expands the range and invalidates the
same-day tightness requirement. The fix became **vcp_v2** (two-stage pivot
watch), which passed out-of-sample validation (PF 1.61, edge confirmed) and
is now the sole live engine. See `CLAUDE.md` § Strategy Validation Campaign.

---

## Remaining Work Before Going Live — SUPERSEDED (went live 2026-07-20)

The 2-month paper plan was replaced by the 2026-07 validation campaign
(5-yr regime-gated backtests + untouched OOS window) — a stronger gate than
calendar paper time. Everything else on this list was executed for the flip;
see `GO_LIVE.md` for the checklist as actually run and the pilot log.

- [x] ~~Complete 2-month paper trading validation~~ → validation campaign 2026-07-13 → 07-17
- [x] Backtest run and validated (vcp_v2 OOS PF 1.61; CAGR target NOT met at pilot sizing — accepted, pilot judges plumbing fidelity, not returns)
- [x] `PAPER_TRADING_MODE=false` (2026-07-19)
- [x] `validate_setup.py` green with live credentials
- [x] Callback URL confirmed
- [x] ₹50,101 funded and verified via bot session
- [x] Restarted under supervisord (program name `autoshare`)
- [ ] Monitor first 5 live trades manually (0 so far — first-trade watch protocol pending)

---

## Bugs Found During Inspection

| Severity | Location | Issue |
|---|---|---|
| Low | `stock_filter.py:filter_market_cap()` | Screener.in reachability from the Oracle Cloud VPS is intermittent (update 2026-07-29: ~280/501 symbols refresh within 7 days, so data mostly exists) — market cap filter passes by default only for symbols with missing data. A fallback source (NSE bhavcopy, Kite instrument master) would still harden this. |
| Low | `main.py:check_exits()` | `current_price` used is the prior day's close from `_universe_ohlcv`, not a real-time intraday price. During market hours, this is stale by up to the last cache refresh (8:30 AM). Intraday LTP polling would improve exit accuracy. |
| Low | `scanner/sector_regime.py` | `SectorRegime` is implemented but never called from `main.py` or the scanner workflow. Dead code. |
| ~~Low~~ FIXED | `backtest/backtest_engine.py` | ~~Regime gate is not applied~~ **Fixed 2026-07-13:** `backtest/regime_sim.py` replays live regime rules day-by-day (VIX-less). |
| ~~Low~~ FIXED | `backtest/backtest_engine.py` | ~~Breakout confirmation not checked~~ **Fixed 2026-07-13:** vcp path requires a valid scan-day pivot breakout before entering next open. |
| Very Low | `scheduler.py:_trading_day_only()` | The `"report"` string check to skip the trading-day wrapper is a fragile string match. A job named `"regime_report"` would accidentally skip the wrapper. A dedicated boolean parameter would be more robust. |

---

## Technical Debt

| Area | Debt |
|---|---|
| `main.py` size | `TradingBot` is ~900 lines with 15+ public methods. Should split into `EntryManager`, `ExitManager`, `DataManager`. |
| Synchronous OHLCV bulk fetch | 200 stocks fetched serially with rate limiting. Takes several minutes each morning. Async/concurrent fetch with the existing rate limiter would be faster. |
| TOTP uses unofficial Zerodha endpoints | `auth_manager.py` hits `/api/login` and `/api/twofa` (internal Zerodha endpoints). These are not part of the Kite Connect specification and could break at any time. |
| No CI/CD pipeline | No GitHub Actions or automated test runner on push. Tests must be run manually. |
| `nsepy` dependency | `nsepy==0.8` is in `requirements.txt` but the Nifty index OHLCV uses Kite instead (nsepy was unreliable for indices). The dependency may be unused or partially used. |
| Monthly report untested | `reports/monthly_report.py` is implemented but has not been exercised in a real production run. |
| No alerting on stale data | If the morning OHLCV fetch fails silently, the bot runs on yesterday's data. No explicit data-staleness check before the scan. |

---

## Suggested Priorities (Post-Paper-Validation)

### High Priority
1. Complete paper trading period; document results
2. Run backtest to validate strategy performance vs targets
3. Fix Screener.in fallback (use NSE bhavcopy or Kite instrument master for market cap)
4. Add real-time LTP polling during market hours for exit checks

### Medium Priority
5. Add GitHub Actions CI (run `pytest` on push)
6. Refactor `main.py` — split `TradingBot` into sub-managers
7. Delete or wire up `scanner/sector_regime.py`
8. Add explicit data-staleness check before morning scan

### Low Priority
9. Async OHLCV bulk fetch (concurrent with rate limiter)
10. Replace TOTP unofficial login with official Kite 2FA API (when/if Zerodha offers one)
11. Battle-test monthly report with 1 full month of data
