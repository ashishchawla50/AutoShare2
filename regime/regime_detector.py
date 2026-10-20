"""Market regime detection and state machine.

Regime classification (precise rules from the spec):

BULL — Full trading mode:
  - Nifty above 200 EMA
  - Breadth > 60% stocks above 200 EMA
  - Nifty making higher highs (above 20-day high from 4 weeks ago)
  - VIX < 20
  All 4 conditions required.

CAUTIOUS — Reduced mode (50% position sizes):
  - Nifty within 5% of 200 EMA (either side), OR
  - Breadth 40–60%, OR
  - VIX 20–25

BEAR — Cash mode (no new longs):
  - Nifty below 200 EMA, OR
  - Breadth < 40%

HIGH_VOLATILITY — VIX 25–35: halve all position sizes
EXTREME_VOLATILITY — VIX > 35: no new trades, exit all within 2 days

Regime DOWNGRADES are IMMEDIATE.
Regime UPGRADES require 2 consecutive days of confirmation (prevents whipsaws).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text

from config.config import config
from regime.vix_monitor import get_india_vix, position_size_multiplier
from utils.indicator_utils import ema, sma

logger = logging.getLogger("regime")


class Regime(str, Enum):
    BULL = "BULL"
    CAUTIOUS = "CAUTIOUS"
    BEAR = "BEAR"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    EXTREME_VOLATILITY = "EXTREME_VOLATILITY"
    UNKNOWN = "UNKNOWN"


@dataclass
class RegimeState:
    regime: Regime
    nifty_close: float
    nifty_ema200: float
    breadth_pct: float
    india_vix: Optional[float]
    position_size_multiplier: float
    allow_new_trades: bool
    checked_at: datetime


class RegimeDetector:
    """Classifies the market regime using Nifty + breadth + VIX signals.

    Args:
        universe_ohlcv: Dict mapping symbol → daily OHLCV DataFrame.
        nifty_ohlcv:    Nifty 50 daily OHLCV DataFrame (for breadth calculation).
    """

    def __init__(
        self,
        universe_ohlcv: dict[str, pd.DataFrame],
        nifty_ohlcv: pd.DataFrame,
    ) -> None:
        self._universe_ohlcv = universe_ohlcv
        self._nifty_ohlcv = nifty_ohlcv
        self._db_path = Path(config.DB_PATH) / "regime_history.db"
        self._engine = create_engine(f"sqlite:///{self._db_path}")
        self._upgrade_state_path = Path(config.DB_PATH) / ".regime_upgrade_state"
        self._previous_regime: Optional[Regime] = self._load_last_regime()
        # Date-based pending upgrade state (persisted across restarts)
        self._upgrade_pending_since: Optional[date] = None
        self._pending_upgrade_to: Optional[Regime] = None
        self._load_upgrade_state()

    def detect(self) -> RegimeState:
        """Run full regime detection and return current RegimeState.

        Regime downgrades are immediate.
        Regime upgrades require 2 consecutive days of confirmation.
        """
        nifty_close = float(self._nifty_ohlcv["close"].iloc[-1])
        nifty_ema200 = float(ema(self._nifty_ohlcv["close"], config.REGIME_EMA_DAYS).iloc[-1])
        breadth = self._calculate_breadth()
        vix = get_india_vix()

        raw_regime = self._classify(nifty_close, nifty_ema200, breadth, vix)
        final_regime = self._apply_upgrade_confirmation(raw_regime)
        self._save_upgrade_state()

        multiplier = self._position_multiplier(final_regime, vix)
        allow_new = final_regime not in (
            Regime.BEAR, Regime.EXTREME_VOLATILITY, Regime.UNKNOWN
        )

        state = RegimeState(
            regime=final_regime,
            nifty_close=nifty_close,
            nifty_ema200=nifty_ema200,
            breadth_pct=breadth,
            india_vix=vix,
            position_size_multiplier=multiplier,
            allow_new_trades=allow_new,
            checked_at=datetime.now(),
        )

        self._log_regime(state)

        if self._previous_regime != final_regime:
            logger.info(
                "Regime changed: %s → %s (breadth=%.1f%%, VIX=%s)",
                self._previous_regime,
                final_regime,
                breadth * 100,
                f"{vix:.1f}" if vix else "N/A",
            )
            self._previous_regime = final_regime

        return state

    def _classify(
        self,
        nifty_close: float,
        nifty_ema200: float,
        breadth: float,
        vix: Optional[float],
    ) -> Regime:
        """Apply regime classification rules. No hysteresis here — that's in _apply_upgrade_confirmation.

        Precedence (most to least restrictive):
          1. EXTREME_VOLATILITY (VIX > 35) — triggers forced exits, overrides even BEAR
          2. BEAR (Nifty below 200 EMA or breadth < 40%) — no new trades
          3. HIGH_VOLATILITY (VIX 25–35) — half position sizes, new trades allowed
          4. BULL / CAUTIOUS — normal / reduced operation

        BEAR must beat HIGH_VOLATILITY: a market below its 200 EMA with VIX at 28
        should NOT allow new entries at 50% size.  Only EXTREME_VOLATILITY outranks
        BEAR because it changes exit behaviour (force-close within 2 days), which
        matters even when already in cash (BEAR) mode.
        """
        return classify_regime(nifty_close, nifty_ema200, breadth, vix,
                               trending_up=self._is_nifty_trending_up)

    def _apply_upgrade_confirmation(self, raw_regime: Regime) -> Regime:
        """Apply the 2-calendar-day confirmation rule for regime upgrades.

        Downgrades (to worse regime) are always immediate.
        Upgrades (to better regime) require the same signal on 2 different
        calendar days.  The pending state is persisted to disk so it survives
        bot restarts between day 1 and day 2.
        """
        if self._previous_regime is None:
            return raw_regime

        prev = self._previous_regime
        is_upgrade = regime_rank(raw_regime) > regime_rank(prev)

        if is_upgrade:
            today = date.today()

            if (
                self._upgrade_pending_since is not None
                and self._pending_upgrade_to == raw_regime
                and self._upgrade_pending_since < today
            ):
                # Same upgrade target seen on a previous calendar day → confirm
                logger.info(
                    "Regime upgrade %s → %s CONFIRMED (pending since %s)",
                    prev, raw_regime, self._upgrade_pending_since,
                )
                self._upgrade_pending_since = None
                self._pending_upgrade_to = None
                return raw_regime

            # First time seeing this upgrade today (or different target)
            if self._pending_upgrade_to != raw_regime or self._upgrade_pending_since is None:
                self._upgrade_pending_since = today
                self._pending_upgrade_to = raw_regime
            logger.debug(
                "Regime upgrade %s → %s pending 2nd-day confirmation (pending since %s)",
                prev, raw_regime, self._upgrade_pending_since,
            )
            return prev  # hold previous regime until confirmed

        else:
            # Downgrade or unchanged — immediate; clear any pending upgrade
            if self._upgrade_pending_since is not None:
                self._upgrade_pending_since = None
                self._pending_upgrade_to = None
            return raw_regime

    def _calculate_breadth(self) -> float:
        """Return the fraction of universe stocks above their 200-day EMA."""
        above = 0
        total = 0
        for symbol, df in self._universe_ohlcv.items():
            if len(df) < config.REGIME_EMA_DAYS:
                continue
            ema200 = ema(df["close"], config.REGIME_EMA_DAYS).iloc[-1]
            current = df["close"].iloc[-1]
            total += 1
            if current > ema200:
                above += 1
        if total == 0:
            return config.REGIME_BREADTH_NEUTRAL  # neutral when no data
        return above / total

    def _is_nifty_trending_up(self) -> bool:
        """Return True if Nifty is making higher highs (20-day high above that of 4 weeks ago)."""
        close = self._nifty_ohlcv["close"]
        hd = config.REGIME_TREND_HIGH_DAYS
        if len(close) < 2 * hd:
            return False
        current_20d_high = close.tail(hd).max()
        prior_20d_high = close.iloc[-2 * hd:-hd].max()
        return float(current_20d_high) > float(prior_20d_high)

    def _position_multiplier(self, regime: Regime, vix: Optional[float]) -> float:
        """Return position size multiplier for the detected regime."""
        return regime_position_multiplier(regime, vix)

    def _log_regime(self, state: RegimeState) -> None:
        """Persist regime state to the history database."""
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO regime_log "
                        "(timestamp, regime, nifty_close, nifty_ema200, breadth_pct, india_vix, action_taken) "
                        "VALUES (:ts, :reg, :nc, :ema, :brd, :vix, :act)"
                    ),
                    {
                        "ts": state.checked_at,
                        "reg": state.regime.value,
                        "nc": state.nifty_close,
                        "ema": state.nifty_ema200,
                        "brd": state.breadth_pct,
                        "vix": state.india_vix,
                        "act": "" if state.allow_new_trades else "no_new_trades",
                    },
                )
        except Exception as exc:
            logger.error("Failed to log regime: %s", exc)

    def _load_upgrade_state(self) -> None:
        """Restore pending upgrade state from disk (survives restarts)."""
        try:
            if self._upgrade_state_path.exists():
                data = json.loads(self._upgrade_state_path.read_text())
                pending_since_str = data.get("pending_since")
                pending_regime_str = data.get("pending_regime")
                if pending_since_str and pending_regime_str:
                    self._upgrade_pending_since = date.fromisoformat(pending_since_str)
                    self._pending_upgrade_to = Regime(pending_regime_str)
        except Exception as exc:
            logger.warning("Could not load upgrade state: %s", exc)

    def _save_upgrade_state(self) -> None:
        """Persist current pending upgrade state to disk."""
        try:
            if self._upgrade_pending_since is not None and self._pending_upgrade_to is not None:
                data = {
                    "pending_since": self._upgrade_pending_since.isoformat(),
                    "pending_regime": self._pending_upgrade_to.value,
                }
            else:
                data = {}
            self._upgrade_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._upgrade_state_path.write_text(json.dumps(data))
        except Exception as exc:
            logger.warning("Could not save upgrade state: %s", exc)

    def _load_last_regime(self) -> Optional[Regime]:
        """Load the most recent regime from the history DB."""
        if not self._db_path.exists():
            return None
        try:
            df = pd.read_sql(
                "SELECT regime FROM regime_log ORDER BY timestamp DESC LIMIT 1",
                self._engine,
            )
            if not df.empty:
                return Regime(df["regime"].iloc[0])
        except Exception:
            pass
        return None


def classify_regime(
    nifty_close: float,
    nifty_ema200: float,
    breadth: float,
    vix: Optional[float],
    trending_up,
) -> Regime:
    """Regime classification rules, shared by RegimeDetector and backtest/regime_sim.

    trending_up is a bool, or a zero-argument callable evaluated only when the BULL
    test is reached (the live detector passes its method). No hysteresis here.
    """
    # EXTREME_VOLATILITY overrides everything — triggers forced exits
    if vix is not None and vix > config.VIX_HALT_LEVEL:
        return Regime.EXTREME_VOLATILITY

    # BEAR: breadth collapse is always BEAR regardless of price.
    # Price below EMA200 is only BEAR if the gap > 3% — a marginal miss
    # (e.g. 0.9% below) with healthy breadth and low VIX is CAUTIOUS, not BEAR.
    breadth_weak = breadth < config.BEAR_BREADTH_THRESHOLD
    price_gap_pct = (nifty_close - nifty_ema200) / nifty_ema200  # negative = below
    price_clearly_below_ema = price_gap_pct < -config.REGIME_BEAR_BELOW_EMA_PCT
    if breadth_weak or price_clearly_below_ema:
        return Regime.BEAR

    # HIGH_VOLATILITY (25–35): only reached when market structure is NOT bearish
    if vix is not None and vix > config.VIX_REDUCE_LEVEL:
        return Regime.HIGH_VOLATILITY

    # Check for Bull conditions (all 4 must be met)
    price_above_ema = nifty_close > nifty_ema200
    breadth_strong = breadth >= config.BULL_BREADTH_THRESHOLD
    ema200_trending = (trending_up() if callable(trending_up) else trending_up)
    vix_calm = vix is None or vix < config.VIX_CAUTION_LEVEL

    if price_above_ema and breadth_strong and ema200_trending and vix_calm:
        return Regime.BULL

    # Nifty within 5% above OR within 3% below the 200 EMA → CAUTIOUS
    near_ema = abs(price_gap_pct) <= max(config.NIFTY_EMA_NEAR_PCT, config.REGIME_NEAR_EMA_MIN_PCT)
    if near_ema or (config.BEAR_BREADTH_THRESHOLD <= breadth < config.BULL_BREADTH_THRESHOLD):
        return Regime.CAUTIOUS

    # VIX in caution zone
    if vix is not None and vix >= config.VIX_CAUTION_LEVEL:
        return Regime.CAUTIOUS

    return Regime.CAUTIOUS


def regime_position_multiplier(regime: Regime, vix: Optional[float]) -> float:
    """Position size multiplier for a regime (shared with backtest/regime_sim)."""
    if regime in (Regime.BEAR, Regime.EXTREME_VOLATILITY, Regime.HIGH_VOLATILITY,
                  Regime.CAUTIOUS):
        return config.REGIME_SIZE_MULTIPLIER[regime.name]
    vix_mult = position_size_multiplier(vix)
    return min(config.REGIME_SIZE_MULTIPLIER[Regime.BULL.name], vix_mult)


def regime_rank(regime: Regime) -> int:
    """Higher rank = better (more bullish) regime. Used for upgrade detection."""
    ranking = {
        Regime.UNKNOWN: 0,
        Regime.EXTREME_VOLATILITY: 1,
        Regime.BEAR: 2,
        Regime.HIGH_VOLATILITY: 3,
        Regime.CAUTIOUS: 4,
        Regime.BULL: 5,
    }
    return ranking.get(regime, 0)
