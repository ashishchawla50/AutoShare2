#!/bin/bash
# ── Trading Bot Setup Script ─────────────────────────────────────────────────
# Tested on Ubuntu 22.04 LTS (VPS)
# Run as root once, then use the 'trader' user

set -euo pipefail

echo "=== Trading Bot Initial Setup ==="
PROJECT_DIR="/home/trader/trading_bot"

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -q
apt-get install -y -q python3.12 python3.12-venv python3.12-dev \
    git build-essential libssl-dev supervisor ufw

# ── Firewall ──────────────────────────────────────────────────────────────────
ufw allow 22/tcp        # SSH
ufw allow 8080/tcp      # Kite auth callback
ufw allow 8081/tcp      # Health check (internal only ideally)
ufw --force enable

# ── User setup ────────────────────────────────────────────────────────────────
useradd -m -s /bin/bash trader 2>/dev/null || echo "User trader already exists"
mkdir -p "$PROJECT_DIR"
chown -R trader:trader "$PROJECT_DIR"

# ── Python virtualenv ─────────────────────────────────────────────────────────
su - trader -c "
    cd $PROJECT_DIR
    python3.12 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip wheel
    pip install -r requirements.txt
    echo 'Python environment ready'
"

# ── Directory structure ───────────────────────────────────────────────────────
su - trader -c "
    mkdir -p $PROJECT_DIR/data/backups
    mkdir -p $PROJECT_DIR/logs
    cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env 2>/dev/null || true
    echo 'Directories created'
"

# ── Database initialisation ───────────────────────────────────────────────────
su - trader -c "
    cd $PROJECT_DIR
    source venv/bin/activate
    python scripts/setup_db.py
    echo 'Databases initialised'
"

# ── Supervisord ───────────────────────────────────────────────────────────────
cp "$PROJECT_DIR/supervisord.conf" /etc/supervisor/conf.d/trading_bot.conf
supervisorctl reread
supervisorctl update

echo ""
echo "=== Setup Complete ==="
echo "1. Edit $PROJECT_DIR/.env with your credentials"
echo "2. Run: python scripts/validate_setup.py"
echo "3. Run: python scripts/backfill_data.py"
echo "4. Start: supervisorctl start trading:*"
