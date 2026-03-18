"""
Mind the Gap Strategy (Tom Hougaard methodology).

Rules:
- On each new session open, measure the gap between the previous
  session's last candle close and the current session's first candle open.
- If gap is above min_gap_ticks and below max_gap_ticks:
    - Gap UP  → enter SHORT (fade the gap), TP at previous close level
    - Gap DOWN → enter LONG  (fade the gap), TP at previous close level
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class GapSignal:
    symbol: str
    direction: str          # "buy" | "sell"
    entry_price: float
    stop_price: float
    take_profit: float      # previous close level
    gap_size: float         # in price units
    gap_size_ticks: float
    gap_direction: str      # "up" | "down"
    prev_close: float
    current_open: float
    candle_timestamp: datetime
    strategy: str = "mind_the_gap"


class MindTheGapStrategy:
    """
    Gap-fade strategy.
    Detects opening gaps and enters counter-trend to fade them back
    toward the previous session's close.
    """

    def __init__(self, config: dict, tick_size: float = 1.0):
        self.min_gap_ticks: float = config.get("min_gap_ticks", 10)
        self.max_gap_ticks: float = config.get("max_gap_ticks", 50)
        self.tp_at_prev_close: bool = config.get("take_profit_at_prev_close", True)
        self.tick_size = tick_size
        self._fired: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def on_session_open(
        self,
        symbol: str,
        prev_session_close: float,
        current_open: float,
        current_candle_timestamp: datetime,
        swing_stop_buffer_ticks: float = 5.0,
    ) -> Optional[GapSignal]:
        """
        Called at the start of each new session with the first candle open.

        Parameters
        ----------
        symbol                    : instrument symbol
        prev_session_close        : last close price of the previous session
        current_open              : first open price of the current session
        current_candle_timestamp  : timestamp of the first candle
        swing_stop_buffer_ticks   : ticks beyond gap extreme for stop loss
        """
        if self._fired.get(symbol):
            return None

        gap = current_open - prev_session_close
        gap_ticks = abs(gap) / self.tick_size

        if gap_ticks < self.min_gap_ticks:
            logger.debug(
                "[%s] Gap %.2f ticks below minimum %d — skip.",
                symbol, gap_ticks, self.min_gap_ticks,
            )
            return None

        if gap_ticks > self.max_gap_ticks:
            logger.info(
                "[%s] Gap %.2f ticks exceeds maximum %d — skip (news/event gap).",
                symbol, gap_ticks, self.max_gap_ticks,
            )
            return None

        tp = prev_session_close  # fade back to previous close
        stop_buffer = swing_stop_buffer_ticks * self.tick_size

        if gap > 0:
            # Gap UP → SHORT (fade back down to prev close)
            direction = "sell"
            gap_dir = "up"
            stop = current_open + stop_buffer  # stop above gap-up open
            logger.info(
                "[GAP] %s | GAP UP %.2f ticks | SHORT @ %.4f | SL: %.4f | TP: %.4f",
                symbol, gap_ticks, current_open, stop, tp,
            )
        else:
            # Gap DOWN → LONG (fade back up to prev close)
            direction = "buy"
            gap_dir = "down"
            stop = current_open - stop_buffer  # stop below gap-down open
            logger.info(
                "[GAP] %s | GAP DOWN %.2f ticks | LONG @ %.4f | SL: %.4f | TP: %.4f",
                symbol, gap_ticks, current_open, stop, tp,
            )

        self._fired[symbol] = True

        return GapSignal(
            symbol=symbol,
            direction=direction,
            entry_price=current_open,
            stop_price=stop,
            take_profit=tp,
            gap_size=abs(gap),
            gap_size_ticks=gap_ticks,
            gap_direction=gap_dir,
            prev_close=prev_session_close,
            current_open=current_open,
            candle_timestamp=current_candle_timestamp,
        )

    def reset_session(self, symbol: str) -> None:
        """Allow a new gap check for the next session."""
        self._fired[symbol] = False

    def measure_gap(
        self,
        df: pd.DataFrame,
        session_open_time: datetime,
    ) -> Optional[tuple[float, float, str]]:
        """
        Utility: measure the gap from the last candle before session_open
        to the first candle at/after session_open.

        Returns (gap_price, gap_ticks, direction) or None.
        """
        if df.empty:
            return None

        ts = pd.to_datetime(df["timestamp"])
        open_ts = pd.Timestamp(session_open_time)

        before = df[ts < open_ts]
        after = df[ts >= open_ts]

        if before.empty or after.empty:
            return None

        prev_close = float(before.iloc[-1]["close"])
        curr_open = float(after.iloc[0]["open"])
        gap = curr_open - prev_close
        gap_ticks = abs(gap) / self.tick_size
        direction = "up" if gap > 0 else "down"

        return gap, gap_ticks, direction
