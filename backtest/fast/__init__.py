"""Fast research backtester — array-based twin of backtest_engine (vcp_v2).

    data.py     reconstructed.db → flat numpy arrays, cached on disk
    signals.py  per-row trend-template + VCP verdicts (vectorised + numba), cached
    engine.py   portfolio simulation over the precomputed arrays
    rules.py / rule_engine.py   the same for mean_reversion, momentum*, breakout*, all
    kernels.py  numba code, imported only when a cache is (re)built

The reference engine (backtest/backtest_engine.py) stays the correctness oracle;
tests/test_fast_engine.py checks this engine against its frozen fixtures.
"""
