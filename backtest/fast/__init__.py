"""Fast research backtester — array-based twin of backtest_engine (vcp_v2).

    data.py     reconstructed.db → flat numpy arrays, cached on disk
    signals.py  per-row trend-template + VCP verdicts (vectorised + numba), cached
    engine.py   portfolio simulation over the precomputed arrays

The reference engine (backtest/backtest_engine.py) stays the correctness oracle;
tests/test_fast_engine.py checks this engine against its frozen fixtures.
"""
