"""
Breakout Strategy — primary strategy (Tom Hougaard methodology).

Rules:
1. Monitor DAX (09:00 CET) and US30 (09:30 EST)
2. Detect the high/low range formed in the first 15 minutes after open
3. Enter LONG if price breaks above the opening range high
4. Enter SHORT if price breaks below the opening range low
5. Only trade within the first 90 minutes of each session
6. Skip if the breakout candle is >2x the average candle size (false breakout filter)
7. 62 EMA trend filter is applied before entry (handled in main)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional

import pandas as pd

from .trend_filter import TrendFilter

logger = logging.getLogger(__name__)


@dataclass
class BreakoutSignal:
    symbol: str
    direction: str          # "buy" | "sell"
    entry_price: float
    stop_price: float
    opening_range_high: float
    opening_range_low: float
    candle_timestamp: datetime
    candle_size: float
    avg_candle_size: float
    strategy: str = "breakout"


class BreakoutStrategy:
    """
    Opening range breakout detector.

    Tracks the high/low of the first N minutes after session open,
    then emits a signal when price breaks out in either direction,
    subject to candle-size and trend-filter checks.
    """

    def __init__(self, config: dict, trend_filter: Optional[TrendFilter] = None):
        self.opening_range_minutes: int = config.get("opening_range_minutes", 15)
        self.session_window_minutes: int = config.get("session_trade_window_minutes", 90)
        self.max_candle_multiplier: float = config.get("max_candle_size_multiplier", 2.0)
        self.avg_candle_lookback: int = config.get("avg_candle_lookback", 20)
        self.trend_filter = trend_filter

        # State per instrument
        self._state: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_candle(
        self,
        symbol: str,
        df: pd.DataFrame,
        session_open_time: datetime,
    ) -> Optional[BreakoutSignal]:
        """
        Called on every new closed candle.
        Returns a BreakoutSignal if entry conditions are met, else None.

        Parameters
        ----------
        symbol           : instrument symbol
        df               : OHLCV DataFrame (columns: timestamp, open, high, low, close, volume)
        session_open_time: datetime of today's session open (timezone-aware)
        """
        if df.empty or len(df) < 2:
            return None

        latest = df.iloc[-1]
        candle_time = _ensure_tz(latest["timestamp"])

        # Only act within session trade window
        session_end = session_open_time + timedelta(minutes=self.session_window_minutes)
        if candle_time < session_open_time or candle_time > session_end:
            return None

        # Build opening range from candles within first N minutes
        or_end = session_open_time + timedelta(minutes=self.opening_range_minutes)
        or_mask = (
            (df["timestamp"].apply(_ensure_tz) >= session_open_time)
            & (df["timestamp"].apply(_ensure_tz) <= or_end)
        )
        opening_range_df = df[or_mask]

        if opening_range_df.empty:
            logger.debug("[%s] No candles in opening range window yet.", symbol)
            return None

        or_high = float(opening_range_df["high"].max())
        or_low = float(opening_range_df["low"].min())

        # Only act AFTER the opening range has closed
        if candle_time <= or_end:
            logger.debug("[%s] Still forming opening range.", symbol)
            return None

        # Candle size filter
        avg_size = _average_candle_size(df, self.avg_candle_lookback)
        candle_size = float(latest["high"]) - float(latest["low"])

        if candle_size > self.max_candle_multiplier * avg_size:
            logger.info(
                "[%s] Breakout skipped — candle size %.4f > %.1fx avg %.4f",
                symbol, candle_size, self.max_candle_multiplier, avg_size,
            )
            return None

        close = float(latest["close"])
        state = self._get_state(symbol)

        # Prevent re-entering after a signal on same session
        if state.get("signal_fired"):
            return None

        # Long breakout
        if close > or_high:
            # Trend filter check
            if self.trend_filter and not self.trend_filter.allows_long(df):
                logger.info(
                    "[%s] LONG breakout rejected by trend filter (62 EMA).", symbol
                )
                return None

            stop = or_low  # Stop below the opening range low
            self._get_state(symbol)["signal_fired"] = True
            signal = BreakoutSignal(
                symbol=symbol,
                direction="buy",
                entry_price=close,
                stop_price=stop,
                opening_range_high=or_high,
                opening_range_low=or_low,
                candle_timestamp=candle_time,
                candle_size=candle_size,
                avg_candle_size=avg_size,
            )
            logger.info(
                "[BREAKOUT] LONG %s | Entry: %.4f | SL: %.4f | OR: %.4f-%.4f",
                symbol, close, stop, or_low, or_high,
            )
            return signal

        # Short breakout
        if close < or_low:
            if self.trend_filter and not self.trend_filter.allows_short(df):
                logger.info(
                    "[%s] SHORT breakout rejected by trend filter (62 EMA).", symbol
                )
                return None

            stop = or_high  # Stop above the opening range high
            self._get_state(symbol)["signal_fired"] = True
            signal = BreakoutSignal(
                symbol=symbol,
                direction="sell",
                entry_price=close,
                stop_price=stop,
                opening_range_high=or_high,
                opening_range_low=or_low,
                candle_timestamp=candle_time,
                candle_size=candle_size,
                avg_candle_size=avg_size,
            )
            logger.info(
                "[BREAKOUT] SHORT %s | Entry: %.4f | SL: %.4f | OR: %.4f-%.4f",
                symbol, close, stop, or_low, or_high,
            )
            return signal

        return None

    def reset_session(self, symbol: str) -> None:
        """Reset strategy state for a new session."""
        self._state[symbol] = {"signal_fired": False}
        logger.debug("[%s] Breakout strategy session reset.", symbol)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_state(self, symbol: str) -> dict:
        if symbol not in self._state:
            self._state[symbol] = {"signal_fired": False}
        return self._state[symbol]

    def get_opening_range(
        self, df: pd.DataFrame, session_open_time: datetime
    ) -> Optional[tuple[float, float]]:
        """
        Returns (range_high, range_low) for the opening range period,
        or None if not enough data.
        """
        or_end = session_open_time + timedelta(minutes=self.opening_range_minutes)
        mask = (
            (df["timestamp"].apply(_ensure_tz) >= session_open_time)
            & (df["timestamp"].apply(_ensure_tz) <= or_end)
        )
        df_or = df[mask]
        if df_or.empty:
            return None
        return float(df_or["high"].max()), float(df_or["low"].min())


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _average_candle_size(df: pd.DataFrame, lookback: int) -> float:
    recent = df.tail(lookback)
    sizes = recent["high"] - recent["low"]
    return float(sizes.mean()) if len(sizes) > 0 else 1.0


def _ensure_tz(dt) -> datetime:
    """Ensure a datetime is timezone-aware (UTC if naive)."""
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
