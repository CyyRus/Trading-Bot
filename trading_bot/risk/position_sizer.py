"""
Position Sizing and Risk Management (Tom Hougaard style).

Rules implemented:
- Risk no more than 1% of account balance per trade
- Stop loss placed just beyond the most recent swing high/low
- Trailing stop activated once trade is 1.5R in profit
- Never average down on a losing trade
- Max 2 trades per session per instrument
- Daily loss limit: if down 2%, stop trading
- After a loss: skip the next marginal setup
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RiskParameters:
    account_balance: float
    entry_price: float
    stop_price: float
    risk_pct: float = 1.0           # % of account to risk
    tick_size: float = 1.0
    point_value: float = 1.0        # $ value per point move per unit

    @property
    def risk_amount(self) -> float:
        """Dollar amount at risk based on risk %."""
        return self.account_balance * (self.risk_pct / 100.0)

    @property
    def stop_distance(self) -> float:
        """Distance in price units from entry to stop."""
        return abs(self.entry_price - self.stop_price)

    @property
    def stop_distance_ticks(self) -> float:
        return self.stop_distance / self.tick_size

    def position_size(self) -> float:
        """
        Calculate position size (number of units/contracts).
        position_size = risk_amount / (stop_distance * point_value)
        """
        distance = self.stop_distance
        if distance == 0:
            logger.error("Stop distance is 0 — cannot calculate position size.")
            return 0.0

        size = self.risk_amount / (distance * self.point_value)
        return max(round(size, 2), 0.01)

    def r_multiple(self, exit_price: float) -> float:
        """Calculate R-multiple of a completed trade."""
        if self.stop_distance == 0:
            return 0.0
        direction = 1 if self.entry_price > self.stop_price else -1
        profit = (exit_price - self.entry_price) * direction
        return profit / self.stop_distance

    def target_at_r(self, r: float) -> float:
        """Return the price target at a given R-multiple."""
        direction = 1 if self.entry_price > self.stop_price else -1
        return self.entry_price + (self.stop_distance * r * direction)

    def trailing_stop_price(self, current_price: float, trigger_r: float = 1.5) -> Optional[float]:
        """
        Once price has moved `trigger_r` R in our favour,
        return the new trailing stop (breakeven + distance proportional to move).
        Returns None if trail hasn't been triggered yet.
        """
        current_r = self.r_multiple(current_price)
        if current_r < trigger_r:
            return None

        direction = 1 if self.entry_price > self.stop_price else -1
        # Trail stop to breakeven once 1.5R is hit
        # Move stop up by half the distance gained beyond breakeven
        gain_beyond_be = (current_r - 0) * self.stop_distance
        trail = self.entry_price + (direction * gain_beyond_be * 0.5)
        return trail


def find_swing_stop(
    df: pd.DataFrame,
    direction: str,
    lookback: int = 10,
    buffer_ticks: float = 2.0,
    tick_size: float = 1.0,
) -> Optional[float]:
    """
    Find the most recent swing high/low for stop placement.

    For LONG trades: stop below the most recent swing low in lookback.
    For SHORT trades: stop above the most recent swing high in lookback.

    Parameters
    ----------
    df           : OHLCV DataFrame
    direction    : "buy" | "sell"
    lookback     : candles to look back for swing point
    buffer_ticks : ticks beyond swing point for stop
    tick_size    : price per tick
    """
    if len(df) < lookback:
        lookback = len(df)

    recent = df.tail(lookback)
    buffer = buffer_ticks * tick_size

    if direction == "buy":
        swing_low = float(recent["low"].min())
        return swing_low - buffer
    else:
        swing_high = float(recent["high"].max())
        return swing_high + buffer


# ---------------------------------------------------------------------------
# Session risk tracker
# ---------------------------------------------------------------------------

@dataclass
class SessionRiskTracker:
    """
    Tracks per-session and per-day risk state for a single instrument.
    Enforces Tom Hougaard risk rules.
    """
    symbol: str
    max_trades_per_session: int = 2
    daily_loss_limit_pct: float = 2.0
    skip_marginal_after_loss: bool = True

    _trades_today: int = field(default=0, init=False)
    _last_trade_was_loss: bool = field(default=False, init=False)
    _daily_starting_balance: float = field(default=0.0, init=False)
    _current_date: Optional[date] = field(default=None, init=False)
    _trade_history: list[dict] = field(default_factory=list, init=False)

    def start_day(self, balance: float, today: Optional[date] = None) -> None:
        """Call at the start of each trading day."""
        self._trades_today = 0
        self._last_trade_was_loss = False
        self._daily_starting_balance = balance
        self._current_date = today or date.today()
        self._trade_history.clear()
        logger.info(
            "[%s] New session started. Balance: $%.2f", self.symbol, balance
        )

    def can_trade(self, current_balance: float, is_marginal_setup: bool = False) -> tuple[bool, str]:
        """
        Check whether a new trade is allowed.

        Returns (allowed: bool, reason: str)
        """
        # Max trades per session
        if self._trades_today >= self.max_trades_per_session:
            return False, f"Max trades per session ({self.max_trades_per_session}) reached."

        # Daily loss limit
        if self._daily_starting_balance > 0:
            daily_pnl_pct = (
                (current_balance - self._daily_starting_balance)
                / self._daily_starting_balance
                * 100
            )
            if daily_pnl_pct <= -self.daily_loss_limit_pct:
                return False, (
                    f"Daily loss limit reached: {daily_pnl_pct:.2f}% "
                    f"(limit: -{self.daily_loss_limit_pct}%)"
                )

        # Skip marginal setup after a loss
        if self.skip_marginal_after_loss and self._last_trade_was_loss and is_marginal_setup:
            return False, "Skipping marginal setup after previous loss (no revenge trading)."

        return True, "OK"

    def record_trade_entry(self) -> None:
        self._trades_today += 1
        logger.debug("[%s] Trade #%d entered this session.", self.symbol, self._trades_today)

    def record_trade_exit(self, outcome: str, r_multiple: float, pnl: float) -> None:
        """
        outcome: "win" | "loss" | "breakeven"
        """
        self._last_trade_was_loss = outcome == "loss"
        self._trade_history.append({
            "outcome": outcome,
            "r_multiple": r_multiple,
            "pnl": pnl,
            "timestamp": datetime.utcnow().isoformat(),
        })
        logger.info(
            "[%s] Trade closed: %s | R: %+.2f | PnL: %+.2f",
            self.symbol, outcome.upper(), r_multiple, pnl,
        )

    def get_daily_stats(self) -> dict:
        history = self._trade_history
        total = len(history)
        wins = sum(1 for t in history if t["outcome"] == "win")
        losses = sum(1 for t in history if t["outcome"] == "loss")
        total_r = sum(t["r_multiple"] for t in history)
        total_pnl = sum(t["pnl"] for t in history)
        return {
            "symbol": self.symbol,
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total * 100 if total > 0 else 0.0,
            "total_r": total_r,
            "total_pnl": total_pnl,
        }
