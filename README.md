# AutoShare — Automated NSE Swing Trading Bot

AutoShare is a fully automated equity swing trading bot for NSE (India) stocks, built on Zerodha Kite Connect. Its validated engine is a **two-stage Minervini VCP** (tight consolidation → pivot watchlist → buy the confirmed breakout), gated by a 5-state market regime detector that blocks entries in adverse conditions and scales position sizes.

**Target:** 18–25% CAGR, maximum 15% drawdown, NSE CNC equity only.

**Status:** **LIVE** since 2026-07-20 (₹50k pilot) after a full validation campaign — vcp_v2 confirmed out-of-sample (PF 1.61); three earlier strategies (momentum, breakout, mean reversion) statistically rejected and benched. Re-validated **at pilot capital** on 2026-08-08: ₹50k OOS PF **1.75** / bootstrap 84.7%, versus 1.77 at ₹5L (`VALIDATION_2026_08.md`). See `GO_LIVE.md` and `CLAUDE.md` for the authoritative current state.

---

## Features

- **Validated two-stage VCP entry (vcp_v2):**
  - 8-condition Minervini Trend Template + VCP pattern detection puts pivots on a persistent 30-day watchlist
  - Entry only on a confirmed breakout through the stored pivot (strong close, 1.5× volume since 2026-10-11; 2× before)
  - Benched (rejected in validation, no new entries): EMA momentum, 20-day breakout, Connors RSI-2

- **9-filter stock scanner** — price, market cap, liquidity, listing age, promoter pledge, debt/equity, circuit hits, data quality, ASM/ESM surveillance + corporate events

- **5-state market regime detection** — BULL / CAUTIOUS / BEAR / HIGH_VOLATILITY / EXTREME_VOLATILITY based on Nifty breadth, EMA200, and India VIX; 2-day upgrade confirmation prevents whipsaws

- **Systematic risk management** — 2.0% risk per trade / 20% max position (chosen by drawdown-mapped sizing study), 30% sector cap, daily/weekly/monthly drawdown limits, anti-revenge trading pauses

- **GTT stop-loss orders** — placed on Zerodha; survive bot restarts and internet outages

- **Trailing stop ratchet** — 4-tier ratchet (breakeven at +10%, +5% at +15%, +10% at +20%, +20% at +30%)

- **Partial profit booking** — automated partial sells at configurable profit tiers

- **Daily APScheduler workflow** — pre-market data refresh, strategy scan, entry/exit checks every 5 minutes during market hours, EOD report

- **Telegram alerts** — all events: trade entry/exit, regime changes, daily/weekly reports, heartbeat dead man's switch

- **Paper trading mode by default** — `[PAPER]` prefix on all Telegram messages; no real orders placed

- **Full backtest engine** — event-driven with point-in-time universe (no look-ahead bias)

---

## Installation

### Requirements

- Python 3.12+
- Zerodha Kite Connect account with API access
- Telegram Bot token
- Ubuntu 22.04 LTS VPS (for production; development works locally)

### Local Development Setup

```bash
# Clone the repository
git clone <repo-url>
cd AutoShare

# Create and activate virtualenv
python3.12 -m venv venv
source venv/bin/activate        # Linux/Mac
# venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt

# Initialize databases
python scripts/setup_db.py

# Validate setup
python scripts/validate_setup.py
```

### VPS Production Setup

```bash
# SSH to VPS as root, then:
bash setup.sh
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full production deployment guide.

---

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required environment variables:

| Variable | Description |
|---|---|
| `ZERODHA_API_KEY` | Kite Connect API key |
| `ZERODHA_API_SECRET` | Kite Connect API secret |
| `ZERODHA_USER_ID` | Zerodha login ID |
| `TOKEN_ENCRYPTION_KEY` | Fernet key (generate below) |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat/channel ID |

Generate the encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Optional:
```bash
PAPER_TRADING_MODE=false          # default is true (safe mode)
TOTP_AUTO_LOGIN_ENABLED=true      # automated login (ToS risk — off by default)
```

**Never commit `.env` to git.** It is gitignored.

---

## Running Locally

```bash
# Activate virtualenv
source venv/bin/activate

# Paper mode (default — safe, no real orders)
python main.py

# Explicit paper mode
python main.py --paper
```

The bot starts APScheduler in the background and enters a monitoring loop. It expects a Kite access token to be available for market data even in paper mode. At 8 AM IST, it sends a Telegram login URL — open it, log in, and Zerodha redirects to the Flask callback server.

The Flask auth server must be running separately (in production, supervisord manages this):
```bash
python broker/auth_server.py     # runs on port 8080
```

---

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for:
- VPS initial setup with `setup.sh`
- Supervisord process management
- Code update procedure
- Port/firewall configuration
- Going live checklist

Quick reference:
```bash
# SSH to VPS
ssh -i ssh-key-2026-06-28.key ubuntu@152.67.10.33

# Bot status
sudo supervisorctl -c /etc/supervisor/supervisord.conf status

# Deploy + restart (run from ~/AutoShare on VPS)
cd ~/AutoShare && git pull && sudo supervisorctl -c /etc/supervisor/supervisord.conf restart autoshare
```

---

## Testing

```bash
# Run all tests (paper mode forced, no real API calls)
pytest

# Run with coverage report
pytest --cov=. --cov-report=term-missing

# Run a specific test file
pytest tests/test_regime.py -v

# Run type checks
mypy . --ignore-missing-imports
```

Tests are located in `tests/`. All tests force paper mode and disable Telegram via `conftest.py` — no live API calls are made.

Current status: **163/163 tests passing** (as of 2026-08-08).

---

## Folder Structure

```
AutoShare/
├── main.py              # TradingBot orchestrator
├── scheduler.py         # APScheduler daily workflow jobs
├── config/              # TradingConfig + NSE holiday calendar
├── utils/               # Logger, encryption, health check, indicators, time
├── broker/              # Kite wrapper, auth, orders, portfolio, GTT monitor
├── data_fetcher/        # OHLCV, universe, fundamentals, surveillance, events
├── strategy/            # VCP, Trend Template, Stage Analysis, Momentum, Breakout
├── scanner/             # 9 filters, RS scoring, opportunity ranking
├── risk/                # Position sizing, drawdown monitor, entry approval
├── regime/              # 5-state regime detector + VIX monitor
├── exit/                # Stop loss, trailing ratchet, partial profit, time stop
├── portfolio/           # P&L tracker, daily metrics
├── backtest/            # Event-driven backtester + performance analyzer
├── reports/             # Daily/weekly/monthly Telegram reports
├── alerts/              # All Telegram alert functions
├── scripts/             # setup_db, validate_setup, backfill_data, manual_scan, run_backtest
├── tests/               # pytest test suite (74 tests)
├── data/                # Runtime SQLite databases (gitignored)
├── logs/                # JSON log files (gitignored)
├── setup.sh             # Ubuntu VPS one-shot setup
├── supervisord.conf      # Supervisord process definitions
├── requirements.txt
└── .env.example         # Environment variable template
```

---

## Troubleshooting

### Bot not starting

```bash
# Check supervisord status
sudo supervisorctl -c /etc/supervisor/supervisord.conf status

# Check stderr for startup errors
tail -20 ~/AutoShare/logs/supervisor_stderr.log

# Validate setup
python scripts/validate_setup.py
```

### No Telegram alerts arriving

1. Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
2. Check `logs/errors.log` for Telegram-related errors
3. Test manually: `python -c "from alerts.telegram_alert import send_text; send_text('test')"`

### Regime shows UNKNOWN / Kite not connected

TOTP auto-login is active (`TOTP_AUTO_LOGIN_ENABLED=true`). Check what happened at 8 AM:

```bash
grep "TOTP\|Login URL\|Kite connected\|auto-login" ~/AutoShare/logs/trading.log | tail -10
```

- `"TOTP auto-login successful"` → Kite connected — look elsewhere for the issue
- `"TOTP auto-login failed"` → TOTP failed; a manual URL was sent to Telegram as fallback — open it
- `"Login URL sent"` → TOTP was skipped; open the Telegram URL manually

Also verify `auth_flask` is running and callback URL in Zerodha app settings is `http://152.67.10.33:8080/callback`.

### Bot running on stale data

The morning OHLCV refresh runs at 8:30 AM IST. If it fails silently:
1. Check `logs/data.log` for fetch errors
2. Verify Kite is connected (check `logs/trading.log` for "Kite connected" message)
3. Manually run: `python scripts/backfill_data.py`

### Emergency stop (no restart needed)

```bash
# Create halt flag — bot stops taking new entries immediately
touch ~/AutoShare/data/emergency_halt.flag

# Remove to resume
rm ~/AutoShare/data/emergency_halt.flag
```

### Exclude a symbol from trading (no restart needed)

```bash
echo "SYMBOLNAME" >> ~/AutoShare/data/manual_blocklist.txt
```

Lines starting with `#` are treated as comments. Changes take effect on the next entry check cycle.

### Check current positions

```bash
# In Python (on VPS):
from broker.portfolio_manager import PortfolioManager
pm = PortfolioManager()
for p in pm.get_open_positions():
    print(p)
```

---

## Architecture Documentation

- [CLAUDE.md](CLAUDE.md) — Full project context for AI assistants and developers
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design, data flow, job schedule, design patterns
- [TODO.md](TODO.md) — Completed features, remaining work, bugs, technical debt
- [DEPLOYMENT.md](DEPLOYMENT.md) — VPS setup, process management, going-live checklist

---

## Contribution Guidelines

This is a personal trading bot project. If contributing:

1. All tests must pass: `pytest`
2. Paper mode must remain the default (`PAPER_TRADING_MODE=true`)
3. No secrets or real credentials in any committed file
4. Do not change risk constants (`RISK_PER_TRADE_PCT`, `MAX_DRAWDOWN_PCT`) without updating `config.py` comments explaining the rationale
5. All strategy changes require corresponding tests
6. Do not disable the regime gate before enabling new entries
7. GTT orders must be placed for every live position — never bypass this

---

## Disclaimer

This software is for personal use. Trading in equity markets involves financial risk. The bot does not guarantee profits. Use at your own risk. Paper trading mode exists for a reason — run it for at least 2 months before switching to live mode.
