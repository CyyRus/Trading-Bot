"""
Scenario-Based Swing Strategy (Tom Hougaard methodology).

Scenario A:
  If Monday high > Tuesday high AND Monday high > Wednesday high
  → Anticipate SHORT on Thursday (enter at end of Wednesday close)

Scenario B:
  If Thursday high > Friday high
  → Anticipate gap-down on Monday (enter SHORT at Friday close)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ScenarioSignal:
    symbol: str
    direction: str          # "buy" | "sell"
    entry_price: float
    stop_price: float
    scenario: str           # "A" | "B"
    rationale: str
    candle_timestamp: datetime
    strategy: str = "scenario"


class ScenarioStrategy:
    """
    Weekly scenario-based swing trade detector.

    Analyses the daily high history across the week to identify
    setups that historically precede directional moves.
    """

    def __init__(self, config: dict):
        self.scenario_a_enabled: bool = config.get("scenario_a_enabled", True)
        self.scenario_b_enabled: bool = config.get("scenario_b_enabled", True)
        self._daily_highs: dict[str, dict[str, float]] = {}  # symbol → {weekday: high}
        self._daily_lows: dict[str, dict[str, float]] = {}
        self._daily_closes: dict[str, dict[str, float]] = {}
        self._fired: dict[str, set[str]] = {}  # symbol → set of scenario keys fired this week

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_daily_candle(
        self,
        symbol: str,
        df_daily: pd.DataFrame,
    ) -> Optional[ScenarioSignal]:
        """
        Called once per day with the full daily OHLCV history.
        Returns a ScenarioSignal if a setup is detected, else None.

        Parameters
        ----------
        symbol   : instrument symbol
        df_daily : daily OHLCV DataFrame (at least last 5 trading days)
        """
        if len(df_daily) < 4:
            return None

        # Build weekday → OHLC mapping for the last 5 trading days
        recent = df_daily.tail(5).copy()
        recent["weekday"] = pd.to_datetime(recent["timestamp"]).dt.day_name()

        highs: dict[str, float] = {}
        lows: dict[str, float] = {}
        closes: dict[str, float] = {}
        for _, row in recent.iterrows():
            day = row["weekday"]
            highs[day] = float(row["high"])
            lows[day] = float(row["low"])
            closes[day] = float(row["close"])

        self._daily_highs[symbol] = highs
        self._daily_lows[symbol] = lows
        self._daily_closes[symbol] = closes

        today = pd.to_datetime(df_daily["timestamp"].iloc[-1]).day_name()
        fired = self._fired.setdefault(symbol, set())

        # --- Scenario A check (fires at Wednesday close) ---
        if self.scenario_a_enabled and today == "Wednesday":
            sig = self._check_scenario_a(symbol, highs, closes, df_daily)
            if sig and "A" not in fired:
                fired.add("A")
                return sig

        # --- Scenario B check (fires at Friday close) ---
        if self.scenario_b_enabled and today == "Friday":
            sig = self._check_scenario_b(symbol, highs, closes, df_daily)
            if sig and "B" not in fired:
                fired.add("B")
                return sig

        # Reset fired set on Monday
        if today == "Monday":
            self._fired[symbol] = set()

        return None

    def get_weekly_context(self, symbol: str) -> dict:
        """Return the current week's daily highs/lows/closes for a symbol."""
        return {
            "highs": self._daily_highs.get(symbol, {}),
            "lows": self._daily_lows.get(symbol, {}),
            "closes": self._daily_closes.get(symbol, {}),
        }

    # ------------------------------------------------------------------
    # Scenario checks
    # ------------------------------------------------------------------

    def _check_scenario_a(
        self,
        symbol: str,
        highs: dict[str, float],
        closes: dict[str, float],
        df_daily: pd.DataFrame,
    ) -> Optional[ScenarioSignal]:
        """
        Scenario A:
        Monday high > Tuesday high AND Monday high > Wednesday high
        → SHORT at Wednesday close, targeting prior Monday low
        """
        mon_high = highs.get("Monday")
        tue_high = highs.get("Tuesday")
        wed_high = highs.get("Wednesday")
        wed_close = closes.get("Wednesday")
        mon_low = self._daily_lows.get(symbol, {}).get("Monday")

        if None in (mon_high, tue_high, wed_high, wed_close):
            logger.debug("[%s] Scenario A: missing daily data.", symbol)
            return None

        if mon_high > tue_high and mon_high > wed_high:  # type: ignore[operator]
            # Stop above Monday high (swing high)
            stop = mon_high * 1.001  # small buffer

            rationale = (
                f"Scenario A: Mon high ({mon_high:.2f}) > Tue high ({tue_high:.2f}) "
                f"AND > Wed high ({wed_high:.2f}). SHORT at Wed close."
            )
            logger.info("[SCENARIO A] %s | %s", symbol, rationale)

            return ScenarioSignal(
                symbol=symbol,
                direction="sell",
                entry_price=wed_close,  # type: ignore[arg-type]
                stop_price=stop,
                scenario="A",
                rationale=rationale,
                candle_timestamp=pd.to_datetime(df_daily["timestamp"].iloc[-1]).to_pydatetime(),
            )
        return None

    def _check_scenario_b(
        self,
        symbol: str,
        highs: dict[str, float],
        closes: dict[str, float],
        df_daily: pd.DataFrame,
    ) -> Optional[ScenarioSignal]:
        """
        Scenario B:
        Thursday high > Friday high
        → SHORT at Friday close (anticipate gap-down Monday)
        """
        thu_high = highs.get("Thursday")
        fri_high = highs.get("Friday")
        fri_close = closes.get("Friday")

        if None in (thu_high, fri_high, fri_close):
            logger.debug("[%s] Scenario B: missing daily data.", symbol)
            return None

        if thu_high > fri_high:  # type: ignore[operator]
            stop = thu_high * 1.001  # above Thursday swing high

            rationale = (
                f"Scenario B: Thu high ({thu_high:.2f}) > Fri high ({fri_high:.2f}). "
                f"SHORT at Fri close — anticipate Monday gap-down."
            )
            logger.info("[SCENARIO B] %s | %s", symbol, rationale)

            return ScenarioSignal(
                symbol=symbol,
                direction="sell",
                entry_price=fri_close,  # type: ignore[arg-type]
                stop_price=stop,
                scenario="B",
                rationale=rationale,
                candle_timestamp=pd.to_datetime(df_daily["timestamp"].iloc[-1]).to_pydatetime(),
            )
        return None
