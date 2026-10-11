"""Tests for the persistent pivot watchlist (two-stage VCP entry)."""

from __future__ import annotations

from datetime import date, timedelta

from config.config import config
from strategy import pivot_watch


def _use_tmp_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(pivot_watch, "_watch_path", lambda: tmp_path / ".pivot_watchlist.json")


def test_round_trip_persistence(monkeypatch, tmp_path):
    _use_tmp_dir(monkeypatch, tmp_path)
    watch: dict = {}
    added = pivot_watch.add_symbol(
        watch, "NYKAA", pivot=340.0, vcp_low=310.0,
        rs_percentile=75.6, tightness=80.0, today=date(2026, 7, 16),
    )
    assert added
    pivot_watch.save_watch(watch)

    loaded = pivot_watch.load_watch()
    assert loaded["NYKAA"]["pivot"] == 340.0
    assert loaded["NYKAA"]["expires"] == (
        date(2026, 7, 16) + timedelta(days=config.VCP2_PIVOT_WATCH_DAYS)
    ).isoformat()


def test_duplicate_add_is_ignored(monkeypatch, tmp_path):
    _use_tmp_dir(monkeypatch, tmp_path)
    watch: dict = {}
    assert pivot_watch.add_symbol(watch, "TITAN", 3400.0, 3200.0, 60.0, 55.0, date(2026, 7, 16))
    # second detection while already watching must not reset the expiry
    assert not pivot_watch.add_symbol(watch, "titan", 3500.0, 3300.0, 61.0, 56.0, date(2026, 7, 20))
    assert watch["TITAN"]["pivot"] == 3400.0


def test_purge_expired(monkeypatch, tmp_path):
    _use_tmp_dir(monkeypatch, tmp_path)
    watch: dict = {}
    pivot_watch.add_symbol(watch, "OLD", 100.0, 90.0, 50.0, 50.0, date(2026, 6, 1))
    pivot_watch.add_symbol(watch, "FRESH", 200.0, 180.0, 50.0, 50.0, date(2026, 7, 15))
    removed = pivot_watch.purge_expired(watch, date(2026, 7, 16))
    assert removed == 1
    assert "OLD" not in watch and "FRESH" in watch


def test_load_missing_file_returns_empty(monkeypatch, tmp_path):
    _use_tmp_dir(monkeypatch, tmp_path)
    assert pivot_watch.load_watch() == {}
