"""Shared pytest fixtures and configuration."""

import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture(autouse=True)
def set_paper_mode(monkeypatch):
    """All tests run in paper trading mode — never touch real APIs."""
    import config.config as cfg_module
    monkeypatch.setattr(cfg_module.config, "PAPER_TRADING_MODE", True)
    monkeypatch.setattr(cfg_module.config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(cfg_module.config, "TELEGRAM_CHAT_ID", "")


@pytest.fixture(autouse=True)
def isolate_data_paths(monkeypatch, tmp_path):
    """Redirect every data path to a per-test temp dir.

    2026-08-06 incident: pytest run in the VPS production checkout wrote
    regime fixture rows into the real data/regime_history.db; the bot then
    restarted into EXTREME_VOLATILITY and lost the next entry window. Tests
    must never see production databases or state files.
    """
    import config.config as cfg_module
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(cfg_module.config, "DB_PATH", str(data_dir))
    monkeypatch.setattr(cfg_module.config, "MANUAL_BLOCKLIST_PATH", str(data_dir / "manual_blocklist.txt"))
    monkeypatch.setattr(cfg_module.config, "MANUAL_EXIT_PATH", str(data_dir / "manual_exit.txt"))
    monkeypatch.setattr(cfg_module.config, "DATA_BACKUP_PATH", str(data_dir / "backups"))

    # Module-level Path constants that bypass config.DB_PATH
    import utils.health_check as health_check
    monkeypatch.setattr(health_check, "_HEARTBEAT_FILE", data_dir / ".last_heartbeat")
    monkeypatch.setattr(health_check, "_HALT_FLAG_FILE", data_dir / "emergency_halt.flag")
    import utils.encryption as encryption
    monkeypatch.setattr(encryption, "_TOKEN_FILE", data_dir / ".access_token.enc")
    import utils.backup as backup
    monkeypatch.setattr(backup, "_BACKUP_DIR", data_dir / "backups")
    monkeypatch.setattr(backup, "_DB_FILES", [str(data_dir / "market_data.db")])
