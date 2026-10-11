"""Guard: tests must never see production data paths.

2026-08-06 incident: pytest run in the VPS production checkout wrote regime
fixture rows into the real data/regime_history.db; the bot restarted into
EXTREME_VOLATILITY and lost the 2026-08-07 entry window. The autouse
isolate_data_paths fixture in conftest.py is the fix; this test pins it.
"""

from pathlib import Path


def test_db_path_is_isolated(tmp_path):
    from config.config import config
    assert config.DB_PATH != "data", "config.DB_PATH still points at the production data dir"
    assert Path(config.DB_PATH).is_absolute()
    assert "data" != Path(config.MANUAL_BLOCKLIST_PATH).parts[0]
    assert "data" != Path(config.MANUAL_EXIT_PATH).parts[0]


def test_regime_detector_writes_go_to_temp_dir():
    """Instantiating RegimeDetector (the 2026-08-06 vector) must not touch data/."""
    from config.config import config
    from regime.regime_detector import RegimeDetector

    detector = RegimeDetector(universe_ohlcv={}, nifty_ohlcv=None)
    assert Path(config.DB_PATH) in detector._db_path.parents
    assert Path("data").resolve() not in detector._db_path.resolve().parents


def test_state_file_constants_are_isolated():
    import utils.encryption as encryption
    import utils.health_check as health_check

    for p in (health_check._HEARTBEAT_FILE, health_check._HALT_FLAG_FILE, encryption._TOKEN_FILE):
        assert p.is_absolute(), f"{p} still relative — would resolve into the production checkout"
