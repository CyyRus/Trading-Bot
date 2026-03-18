"""
Trend Filter — 62-period EMA (Lost Momentum filter).

Rules (Tom Hougaard methodology):
- Only take LONG setups when price is above the 62 EMA
- Only take SHORT setups when price is below the 62 EMA
- EMA must be sloping in the direction of the trade
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

EMA_PERIOD_DEFAULT = 62


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Return the EMA of a price series."""
    return series.ewm(span=period, adjust=False).mean()


def ema_slope(ema_series: pd.Series, lookback: int = 3) -> float:
    """
    Return the slope of the EMA over the last `lookback` candles.
    Positive = trending up, Negative = trending down.
    """
    if len(ema_series) < lookback:
        return 0.0
    recent = ema_series.iloc[-lookback:]
    # Linear regression slope
    x = np.arange(len(recent))
    slope = float(np.polyfit(x, recent.values, 1)[0])
    return slope


class TrendFilter:
    """
    62-period EMA trend filter.
    Determines whether a directional trade is aligned with the prevailing trend.
    """

    def __init__(self, config: dict):
        self.ema_period: int = config.get("ema_period", EMA_PERIOD_DEFAULT)
        self.require_slope: bool = config.get("require_ema_slope", True)
        self.enabled: bool = config.get("enabled", True)

    def analyse(self, df: pd.DataFrame) -> dict:
        """
        Compute EMA and determine trend direction.

        Parameters
        ----------
        df : DataFrame with at least a 'close' column.

        Returns
        -------
        {
            "ema": pd.Series,
            "current_ema": float,
            "current_price": float,
            "trend": "up" | "down" | "neutral",
            "slope": float,
        }
        """
        if len(df) < self.ema_period:
            logger.warning(
                "Not enough data for %d-period EMA (have %d candles).",
                self.ema_period,
                len(df),
            )
            return {"trend": "neutral", "slope": 0.0, "current_ema": 0.0}

        ema = compute_ema(df["close"], self.ema_period)
        current_price = float(df["close"].iloc[-1])
        current_ema = float(ema.iloc[-1])
        slope = ema_slope(ema)

        if current_price > current_ema:
            trend = "up"
        elif current_price < current_ema:
            trend = "down"
        else:
            trend = "neutral"

        return {
            "ema": ema,
            "current_ema": current_ema,
            "current_price": current_price,
            "trend": trend,
            "slope": slope,
        }

    def allows_long(self, df: pd.DataFrame) -> bool:
        """
        Returns True if conditions allow a LONG trade.
        Price must be above 62 EMA and EMA must be sloping up.
        """
        if not self.enabled:
            return True

        result = self.analyse(df)
        price_condition = result["trend"] == "up"
        slope_condition = (result["slope"] >= 0) if self.require_slope else True

        allowed = price_condition and slope_condition
        logger.debug(
            "TrendFilter LONG check: price_above_ema=%s, slope_up=%s → %s",
            price_condition, slope_condition, allowed,
        )
        return allowed

    def allows_short(self, df: pd.DataFrame) -> bool:
        """
        Returns True if conditions allow a SHORT trade.
        Price must be below 62 EMA and EMA must be sloping down.
        """
        if not self.enabled:
            return True

        result = self.analyse(df)
        price_condition = result["trend"] == "down"
        slope_condition = (result["slope"] <= 0) if self.require_slope else True

        allowed = price_condition and slope_condition
        logger.debug(
            "TrendFilter SHORT check: price_below_ema=%s, slope_down=%s → %s",
            price_condition, slope_condition, allowed,
        )
        return allowed
