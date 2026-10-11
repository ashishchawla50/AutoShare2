# DEPLOYMENT.md — AutoShare Trading Bot

---

## Server Requirements

| Requirement | Value |
|---|---|
| OS | Ubuntu 22.04 LTS |
| Python | 3.12+ (required; `python3.12-venv`, `python3.12-dev`) |
| RAM | 1 GB minimum (2 GB recommended) |
| Disk | 5 GB minimum (for SQLite databases and 90-day logs) |
| Cloud Provider | Oracle Cloud (inferred from IP range) |
| VM Name | Unknown |
| Public IP | `152.67.10.33` |
| SSH Host | `ubuntu@152.67.10.33` |
| SSH Key | `ssh-key-2026-06-28.key` (in project root, gitignored) |
| Public URL | Unknown (no domain configured — accessed by IP) |
| Region | Unknown (India West inferred from IST timezone targeting) |

---

## Connecting to the VPS

```bash
# From project root
ssh -i ssh-key-2026-06-28.key ubuntu@152.67.10.33
```

Keep `ssh-key-2026-06-28.key` secure — it is gitignored and must not be committed.

---

## Directory Structure on VPS

```
~/AutoShare/         # Application root (git repo)
~/AutoShare/data/    # SQLite databases (not in git)
~/AutoShare/logs/    # JSON log files (not in git)
~/AutoShare/.env     # Secrets (not in git)
/etc/supervisor/supervisord.conf   # Supervisord config
```

---

## Build Process

No build step — Python project, no compilation.

**Virtualenv location:** `~/AutoShare/venv/`
Always use the venv Python for manual commands — system Python 3.10 does not have the required packages.

```bash
# Install/update packages
~/AutoShare/venv/bin/pip install -r requirements.txt
```

---

## First-Time Deployment (Ubuntu VPS)

```bash
# 1. SSH into VPS as root
ssh root@152.67.10.33

# 2. Clone or copy the project to ~/AutoShare/
# (git clone, scp, or rsync)

# 3. Run the one-shot setup script (as root)
bash ~/AutoShare/setup.sh
```

`setup.sh` performs:
1. `apt-get install python3.12 python3.12-venv python3.12-dev git build-essential libssl-dev supervisor ufw`
2. UFW firewall: open ports 22 (SSH), 8080 (Kite callback), 8081 (health check)
3. Create `trader` user
4. Create Python 3.12 virtualenv at `~/venv/`
5. `pip install -r requirements.txt`
6. Create `data/backups/` and `logs/` directories
7. Copy `.env.example` → `.env`
8. Run `python scripts/setup_db.py` (initialize all 6 SQLite databases)
9. Copy `supervisord.conf` → `/etc/supervisor/conf.d/trading_bot.conf`
10. `supervisorctl reread && supervisorctl update`

---

## Post-Setup Configuration

```bash
# As trader user on VPS:
nano ~/AutoShare/.env
```

Fill in all secrets (see Environment Variables section below).

```bash
# Validate configuration
cd /home/trader/trading_bot
source ~/venv/bin/activate
python scripts/validate_setup.py
```

All 8 check categories must pass before running the bot.

---

## Historical Data Backfill

Must be done once after initial setup and after obtaining a Kite access token:

```bash
# Get a Kite access token first (manual login via Telegram URL)
# Then:
python scripts/backfill_data.py           # default: 730 days
python scripts/backfill_data.py --days 500  # override if needed
```

---

## Process Management (Supervisord)

Two supervised processes run always:

| Process | Command | Port | Auto-restart |
|---|---|---|---|
| `trading_bot` | `python main.py` | 8081 (health) | Yes, up to 3 retries |
| `auth_flask` | `python broker/auth_server.py` | 8080 (OAuth) | Yes, up to 3 retries |

Config location on VPS: `/etc/supervisor/conf.d/trading_bot.conf`

```bash
# Check status (always use -c flag — required on this VPS)
sudo supervisorctl -c /etc/supervisor/supervisord.conf status

# Start all processes
sudo supervisorctl -c /etc/supervisor/supervisord.conf start all

# Stop all processes
sudo supervisorctl -c /etc/supervisor/supervisord.conf stop all

# Restart trading bot only (after code update)
sudo supervisorctl -c /etc/supervisor/supervisord.conf restart autoshare

# Restart auth server only
sudo supervisorctl -c /etc/supervisor/supervisord.conf restart auth_flask

# Read supervisord stdout log
tail -f ~/AutoShare/logs/supervisor_stdout.log

# Read supervisord stderr log
tail -f ~/AutoShare/logs/supervisor_stderr.log
```

> **Note:** The program names on the VPS are `autoshare` (trading bot) and `auth_flask`. There is no `trading:*` group. Always pass `-c /etc/supervisor/supervisord.conf` — omitting it causes a permission error.

---

## Common Manual Commands (always use venv Python)

```bash
# TOTP login (get a fresh Kite token manually)
cd ~/AutoShare && ~/AutoShare/venv/bin/python3 -c "
from broker.auth_manager import AuthManager
auth = AuthManager()
token = auth.login_with_totp()
print('Token obtained:', bool(token))
"

# Validate setup
cd ~/AutoShare && ~/AutoShare/venv/bin/python3 scripts/validate_setup.py

# Backfill historical data (needs valid Kite token first)
cd ~/AutoShare && ~/AutoShare/venv/bin/python3 scripts/backfill_data.py --days 730

# Run backtest
cd ~/AutoShare && ~/AutoShare/venv/bin/python3 scripts/run_backtest.py --start 2025-07-01 --end 2026-07-07 --capital 50000

# Manual one-off scan
cd ~/AutoShare && ~/AutoShare/venv/bin/python3 scripts/manual_scan.py
```

---

## Deploying Code Updates

```bash
# Standard deploy (from VPS):
cd ~/AutoShare && git pull && sudo supervisorctl -c /etc/supervisor/supervisord.conf restart autoshare
```

If `requirements.txt` changed, also run:
```bash
pip install -r requirements.txt
```

If database schema changes are needed:
```bash
python scripts/setup_db.py              # safe to re-run (CREATE TABLE IF NOT EXISTS)
```

---

## Docker

**Not used.** The project uses `supervisord` directly on Ubuntu. Docker is not configured.

---

## CI/CD

**Not configured.** No GitHub Actions, no automated pipeline. Tests are run manually.

```bash
# Run locally before pushing:
pytest
mypy . --ignore-missing-imports
```

---

## Environment Variables

All secrets loaded from `~/AutoShare/.env`. Copy from `.env.example`.

| Variable | Required For Live | Description |
|---|---|---|
| `ZERODHA_API_KEY` | Yes | Kite Connect API key |
| `ZERODHA_API_SECRET` | Yes | Kite Connect API secret |
| `ZERODHA_USER_ID` | Yes | Zerodha login ID |
| `ZERODHA_PASSWORD` | TOTP only | Used only if `TOTP_AUTO_LOGIN_ENABLED=true` |
| `ZERODHA_TOTP_SECRET` | TOTP only | TOTP secret for automated 2FA |
| `TOKEN_ENCRYPTION_KEY` | Yes | Fernet key for encrypting access token on disk |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat ID for alerts |
| `PAPER_TRADING_MODE` | — | `false` to go live; defaults to `true` |
| `TOTP_AUTO_LOGIN_ENABLED` | — | `true` to enable headless TOTP login |

Generate `TOKEN_ENCRYPTION_KEY`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Storage Locations

| Path | Contents | In Git? |
|---|---|---|
| `data/market_data.db` | OHLCV daily candles for all universe stocks | No |
| `data/fundamentals.db` | Market cap, D/E, sector, pledge data | No |
| `data/corporate_events.db` | Earnings, dividends, bonus, splits | No |
| `data/surveillance.db` | ASM/ESM/T2T lists | No |
| `data/portfolio_state.db` | **Critical** — all trades, open positions, metrics | No |
| `data/regime_history.db` | Regime detection history | No |
| `data/.access_token.enc` | Encrypted Zerodha session token (refreshed daily) | No |
| `data/.last_heartbeat` | Bot heartbeat timestamp | No |
| `data/.regime_upgrade_state` | Pending regime upgrade persistence | No |
| `data/manual_blocklist.txt` | Symbols to never trade | No |
| `data/backups/` | Rotating SQLite backups | No |
| `logs/` | All log files | No |

**Critical backup:** `portfolio_state.db` is the only stateful database that cannot be regenerated automatically. Back up regularly. The `backup_databases()` job runs at 16:30 daily to `data/backups/`.

---

## Logging

**Format:** JSON (machine-parseable), one object per line.

**Domain routing:**

| Log File | Content | Rotation |
|---|---|---|
| `logs/trading.log` | Main bot events, startup, entry/exit decisions | Daily midnight, 90 backups |
| `logs/orders.log` | Every API order call, GTT placement | Daily midnight, 90 backups |
| `logs/scanner.log` | Filter decisions, VCP results, RS scoring | Daily midnight, 90 backups |
| `logs/risk.log` | Position sizing, drawdown checks, entry approval | Daily midnight, 90 backups |
| `logs/regime.log` | Regime detection, state transitions | Daily midnight, 90 backups |
| `logs/data.log` | OHLCV fetching, cache hits/misses | Daily midnight, 90 backups |
| `logs/errors.log` | All WARNING+ from every module | Daily midnight, 90 backups |
| `logs/supervisor_stdout.log` | Supervisord stdout | 50 MB max, 5 backups |
| `logs/supervisor_stderr.log` | Supervisord stderr | 50 MB max, 5 backups |
| `logs/auth_server.log` | Flask auth server stdout | 50 MB max |
| `logs/auth_server_err.log` | Flask auth server stderr | 50 MB max |

```bash
# Tail and pretty-print live trading log:
tail -f ~/AutoShare/logs/trading.log | python -m json.tool

# Watch errors only:
tail -f ~/AutoShare/logs/errors.log
```

---

## Ports & Firewall

| Port | Service | Purpose | UFW Rule |
|---|---|---|---|
| 22 | SSH | Server access | `ufw allow 22/tcp` |
| 8080 | Flask auth server | Zerodha OAuth redirect callback | `ufw allow 8080/tcp` |
| 8081 | HTTP health server | External uptime monitoring | `ufw allow 8081/tcp` |

**Kite Connect callback URL to configure in Zerodha app settings:**
```
http://152.67.10.33:8080/callback
```

---

## Cron Jobs

All scheduling is handled internally by APScheduler (not system cron). See `scheduler.py` for the full schedule.

APScheduler runs as a background thread inside `main.py`. No system crontab entries are needed.

---

## SSL / Reverse Proxy

**Not configured.** The bot is accessed only by the operator (SSH) and by Zerodha's OAuth redirect. No public-facing web UI. SSL is not required for the current use case.

If HTTPS is needed in the future, nginx + Let's Encrypt can be placed in front of port 8080.

---

## Health Monitoring

Internal:
- Heartbeat file written every 30 min during market hours
- Dead man's switch: if heartbeat is stale during market hours → Telegram alert
- HTTP endpoint at `:8081` returns `200 OK` when bot process is alive

External:
- Port 8081 can be monitored by UptimeRobot or any HTTP uptime service
- Alert if the HTTP endpoint does not respond within a set interval

---

## Switching from Paper to Live Mode

1. Ensure ≥2 months paper trading completed with satisfactory results
2. Run backtest; confirm 18–25% CAGR target plausible
3. Fund Zerodha account with starting capital (₹50,000)
4. Set `PAPER_TRADING_MODE=false` in `.env`
5. Run `python scripts/validate_setup.py` — all checks must pass
6. `supervisorctl restart trading:trading_bot`
7. At 8:00 AM IST: respond to login URL Telegram message
8. Confirm first `[LIVE] Trading bot started` alert arrives on Telegram (no `[PAPER]` prefix)
9. Monitor first 3–5 trades manually
