"""Heartbeat and dead man's switch for the trading bot.

If the bot doesn't call record_heartbeat() within HEARTBEAT_TIMEOUT_MINUTES
during market hours, an alert is sent and the heartbeat flag is marked stale.

A simple HTTP endpoint is also exposed so external uptime monitors (UptimeRobot etc.)
can verify the process is alive.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from config.config import config
from config.holidays import is_trading_day
from utils.time_utils import is_market_open, now_ist

logger = logging.getLogger("trading.health")

_HEARTBEAT_FILE = Path("data/.last_heartbeat")
_HALT_FLAG_FILE = Path("data/emergency_halt.flag")


def record_heartbeat() -> None:
    """Write the current timestamp as the last known-good heartbeat."""
    _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HEARTBEAT_FILE.write_text(now_ist().isoformat())


def get_last_heartbeat() -> Optional[datetime.datetime]:
    """Return the last recorded heartbeat datetime, or None if never recorded."""
    if not _HEARTBEAT_FILE.exists():
        return None
    try:
        from zoneinfo import ZoneInfo
        raw = _HEARTBEAT_FILE.read_text().strip()
        dt = datetime.datetime.fromisoformat(raw)
        return dt
    except (ValueError, OSError):
        return None


def is_heartbeat_stale() -> bool:
    """Return True if the heartbeat has not been updated within the timeout window.

    Only meaningful during market hours; always returns False outside market hours.
    """
    if not is_market_open():
        return False
    last = get_last_heartbeat()
    if last is None:
        return True
    elapsed_minutes = (now_ist() - last).total_seconds() / 60
    return elapsed_minutes > config.HEARTBEAT_TIMEOUT_MINUTES


def is_emergency_halt_active() -> bool:
    """Return True if the emergency halt flag file is present on disk."""
    return _HALT_FLAG_FILE.exists()


def set_emergency_halt(reason: str) -> None:
    """Write the emergency halt flag file with a reason string.

    The bot will refuse to place new trades until this file is manually deleted.
    """
    _HALT_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = f"{now_ist().isoformat()} — {reason}\n"
    _HALT_FLAG_FILE.write_text(content)
    logger.critical("EMERGENCY HALT activated: %s", reason)


def clear_emergency_halt() -> None:
    """Remove the emergency halt flag (operator action)."""
    if _HALT_FLAG_FILE.exists():
        _HALT_FLAG_FILE.unlink()
        logger.info("Emergency halt flag cleared")


def start_http_health_server(port: int = 8081) -> threading.Thread:
    """Start a lightweight HTTP server on port 8081 for uptime monitoring.

    GET /health → 200 OK with JSON status
    GET /halt   → 200 OK indicating halt status

    Runs in a daemon thread; dies when the main process exits.
    """
    import http.server
    import json

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/health", "/"):
                stale = is_heartbeat_stale()
                halt = is_emergency_halt_active()
                status = "ok" if not stale and not halt else "degraded"
                body = json.dumps(
                    {
                        "status": status,
                        "heartbeat_stale": stale,
                        "emergency_halt": halt,
                        "last_heartbeat": str(get_last_heartbeat()),
                        "checked_at": now_ist().isoformat(),
                    }
                ).encode()
                self.send_response(200 if status == "ok" else 503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            pass  # suppress default HTTP access logs

    class _ReuseServer(http.server.HTTPServer):
        allow_reuse_address = True

    server = _ReuseServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check HTTP server started on port %d", port)
    return thread
