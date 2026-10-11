"""Daily SQLite database backup utility.

Creates timestamped compressed copies in data/backups/.
Keeps the last 30 daily backups; older ones are auto-purged.
"""

from __future__ import annotations

import gzip
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from utils.time_utils import IST, now_ist

logger = logging.getLogger("data")

_DB_FILES = [
    "data/market_data.db",
    "data/fundamentals.db",
    "data/corporate_events.db",
    "data/surveillance.db",
    "data/portfolio_state.db",
    "data/regime_history.db",
]

_BACKUP_DIR = Path("data/backups")
_KEEP_DAYS = 30


def backup_databases() -> list[str]:
    """Compress and copy all database files to the backup directory.

    Returns:
        List of backup file paths created.
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today_str = now_ist().strftime("%Y%m%d_%H%M")
    created: list[str] = []

    for db_path_str in _DB_FILES:
        db_path = Path(db_path_str)
        if not db_path.exists():
            logger.debug("Skipping backup for %s — file not found", db_path)
            continue

        backup_name = f"{db_path.stem}_{today_str}.db.gz"
        backup_path = _BACKUP_DIR / backup_name

        try:
            with db_path.open("rb") as f_in, gzip.open(backup_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            created.append(str(backup_path))
            logger.info("Backed up %s → %s", db_path, backup_path)
        except OSError as exc:
            logger.error("Failed to backup %s: %s", db_path, exc)

    _purge_old_backups()
    return created


def _purge_old_backups() -> None:
    """Delete backup files older than _KEEP_DAYS days."""
    cutoff = now_ist() - timedelta(days=_KEEP_DAYS)
    removed = 0
    for f in _BACKUP_DIR.glob("*.db.gz"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=IST)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Purged %d old backup file(s)", removed)
