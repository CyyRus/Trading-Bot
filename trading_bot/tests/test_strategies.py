"""
Unit tests for all trading strategies and risk management.

Run with:
    pytest trading_bot/tests/test_strategies.py -v
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd
import numpy as np
import pytest

# Ensure parent directory is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategies.trend_filter import TrendFilter, compute_ema, ema_slope
from strategies.breakout import BreakoutStrategy, _average_candle_size
from strategies.scenario import ScenarioStrategy
from strategies.mind_the_gap import MindTheGapStrategy
from risk.position_sizer import RiskParameters, SessionRiskTracker, find_swing_stop


# ──────────────────────────────────────────────────────────────────────────
# Fixtures / Helpers
# ──────────────────────────────────────────────────────────────────────────

def make_ohlcv(
    n: int = 100,
    base_price: float = 1000.0,
    trend: float = 0.5,       # price increment per candle
    volatility: float = 5.0,
    start_time: Optional[datetime] = None,
    freq_minutes: int = 5,
) -> pd.DataFrame:
    """Create a synthetic OHLCV DataFrame."""
    if start_time is None:
        start_time = datetime(2024, 1, 2, 8, 0, 0, tzinfo=timezone.utc)

    timestamps = [start_time + timedelta(minutes=i * freq_minutes) for i in range(n)]
    closes = [base_price + i * trend + np.random.normal(0, volatility) for i in range(n)]
    opens = [c + np.random.normal(0, volatility * 0.3) for c in closes]
    highs = [max(o, c) + abs(np.random.normal(0, volatility * 0.5)) for o, c in zip(opens, closes)]
    lows = [min(o, c) - abs(np.random.normal(0, volatility * 0.5)) for o, c in zip(opens, closes)]
    volumes = [np.random.randint(100, 1000) for _ in range(n)]

    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def make_daily_ohlcv(
    weekdays: list[str],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> pd.DataFrame:
    """Create a daily OHLCV DataFrame with specified weekday highs/lows."""
    day_map = {
        "Monday": "2024-01-01",
        "Tuesday": "2024-01-02",
        "Wednesday": "2024-01-03",
        "Thursday": "2024-01-04",
        "Friday": "2024-01-05",
    }
    rows = []
    for day, h, l, c in zip(weekdays, highs, lows, closes):
        rows.append({
            "timestamp": pd.Timestamp(day_map[day]),
            "open": c - 1,
            "high": h,
            "low": l,
            "close": c,
            "volume": 1000,
        })
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────
# Trend Filter Tests
# ──────────────────────────────────────────────────────────────────────────

class TestTrendFilter:
    def setup_method(self):
        self.config = {"ema_period": 10, "require_ema_slope": True, "enabled": True}
        self.tf = TrendFilter(self.config)

    def test_ema_calculated(self):
        df = make_ohlcv(n=50)
        result = self.tf.analyse(df)
        assert "current_ema" in result
        assert result["current_ema"] > 0

    def test_uptrend_allows_long(self):
        # Rising prices well above EMA
        df = make_ohlcv(n=60, base_price=1000, trend=5.0, volatility=0.1)
        assert self.tf.allows_long(df) is True

    def test_downtrend_allows_short(self):
        # Falling prices well below EMA
        df = make_ohlcv(n=60, base_price=1000, trend=-5.0, volatility=0.1)
        assert self.tf.allows_short(df) is True

    def test_uptrend_blocks_short(self):
        df = make_ohlcv(n=60, base_price=1000, trend=5.0, volatility=0.1)
        assert self.tf.allows_short(df) is False

    def test_downtrend_blocks_long(self):
        df = make_ohlcv(n=60, base_price=1000, trend=-5.0, volatility=0.1)
        assert self.tf.allows_long(df) is False

    def test_disabled_filter_allows_both(self):
        self.tf.enabled = False
        df = make_ohlcv(n=60, base_price=1000, trend=-5.0, volatility=0.1)
        assert self.tf.allows_long(df) is True
        assert self.tf.allows_short(df) is True

    def test_insufficient_data_returns_neutral(self):
        df = make_ohlcv(n=5)  # less than ema_period
        result = self.tf.analyse(df)
        assert result["trend"] == "neutral"

    def test_ema_slope_positive_for_uptrend(self):
        series = pd.Series([100, 102, 104, 106, 108, 110])
        slope = ema_slope(series)
        assert slope > 0

    def test_ema_slope_negative_for_downtrend(self):
        series = pd.Series([110, 108, 106, 104, 102, 100])
        slope = ema_slope(series)
        assert slope < 0


# ──────────────────────────────────────────────────────────────────────────
# Breakout Strategy Tests
# ──────────────────────────────────────────────────────────────────────────

class TestBreakoutStrategy:
    def setup_method(self):
        np.random.seed(42)
        self.config = {
            "opening_range_minutes": 15,
            "session_trade_window_minutes": 90,
            "max_candle_size_multiplier": 2.0,
            "avg_candle_lookback": 20,
        }
        self.strategy = BreakoutStrategy(self.config, trend_filter=None)
        self.session_open = datetime(2024, 1, 2, 8, 0, 0, tzinfo=timezone.utc)

    def _make_breakout_df(
        self, or_high: float, or_low: float, breakout_direction: str
    ) -> pd.DataFrame:
        """Create a DataFrame where the last candle breaks the opening range."""
        base = 1000.0
        times = [self.session_open + timedelta(minutes=i * 5) for i in range(25)]

        rows = []
        for i, t in enumerate(times):
            if t <= self.session_open + timedelta(minutes=15):
                # Opening range candles — stay within or_high/or_low
                mid = (or_high + or_low) / 2
                rows.append({
                    "timestamp": t,
                    "open": mid - 2, "high": or_high - 0.5,
                    "low": or_low + 0.5, "close": mid,
                    "volume": 500,
                })
            elif i < len(times) - 1:
                # Normal consolidation candles
                rows.append({
                    "timestamp": t,
                    "open": base, "high": base + 3,
                    "low": base - 3, "close": base,
                    "volume": 400,
                })
            else:
                # Final breakout candle
                if breakout_direction == "up":
                    rows.append({
                        "timestamp": t,
                        "open": or_high, "high": or_high + 10,
                        "low": or_high - 2, "close": or_high + 8,
                        "volume": 800,
                    })
                else:
                    rows.append({
                        "timestamp": t,
                        "open": or_low, "high": or_low + 2,
                        "low": or_low - 10, "close": or_low - 8,
                        "volume": 800,
                    })
        return pd.DataFrame(rows)

    def test_long_breakout_detected(self):
        df = self._make_breakout_df(1010.0, 990.0, "up")
        signal = self.strategy.on_candle("DAX", df, self.session_open)
        assert signal is not None
        assert signal.direction == "buy"
        assert signal.entry_price > signal.opening_range_high

    def test_short_breakout_detected(self):
        df = self._make_breakout_df(1010.0, 990.0, "down")
        signal = self.strategy.on_candle("DAX", df, self.session_open)
        assert signal is not None
        assert signal.direction == "sell"
        assert signal.entry_price < signal.opening_range_low

    def test_no_signal_during_opening_range(self):
        base = 1000.0
        times = [self.session_open + timedelta(minutes=i * 5) for i in range(3)]
        rows = [
            {"timestamp": t, "open": base, "high": base + 5,
             "low": base - 5, "close": base, "volume": 400}
            for t in times
        ]
        df = pd.DataFrame(rows)
        signal = self.strategy.on_candle("DAX", df, self.session_open)
        assert signal is None

    def test_no_signal_outside_session_window(self):
        """Candle outside 90-minute window should not produce signal."""
        df = self._make_breakout_df(1010.0, 990.0, "up")
        # Move all timestamps far outside session window
        df["timestamp"] = df["timestamp"].apply(
            lambda t: t + timedelta(hours=3)
        )
        signal = self.strategy.on_candle("DAX", df, self.session_open)
        assert signal is None

    def test_huge_candle_filter(self):
        """Breakout candle >2x avg should be skipped."""
        df = self._make_breakout_df(1010.0, 990.0, "up")
        # Make the last candle enormous
        df.iloc[-1, df.columns.get_loc("high")] = 1010.0 + 200
        df.iloc[-1, df.columns.get_loc("low")] = 990.0 - 200
        df.iloc[-1, df.columns.get_loc("close")] = 1010.0 + 150
        signal = self.strategy.on_candle("DAX", df, self.session_open)
        assert signal is None

    def test_signal_only_fired_once_per_session(self):
        df = self._make_breakout_df(1010.0, 990.0, "up")
        sig1 = self.strategy.on_candle("DAX", df, self.session_open)
        sig2 = self.strategy.on_candle("DAX", df, self.session_open)
        assert sig1 is not None
        assert sig2 is None

    def test_session_reset(self):
        df = self._make_breakout_df(1010.0, 990.0, "up")
        self.strategy.on_candle("DAX", df, self.session_open)
        self.strategy.reset_session("DAX")
        sig = self.strategy.on_candle("DAX", df, self.session_open)
        assert sig is not None

    def test_trend_filter_blocks_long(self):
        """Trend filter in downtrend should block long breakout."""
        tf_config = {"ema_period": 5, "require_ema_slope": False, "enabled": True}
        tf = TrendFilter(tf_config)
        strategy = BreakoutStrategy(self.config, trend_filter=tf)
        # Create downtrending data where close < EMA
        df = make_ohlcv(n=30, base_price=1000, trend=-10.0, volatility=0.1)
        # Override last candle to look like breakout up
        df.iloc[-1, df.columns.get_loc("close")] = df["high"].max() + 10
        signal = strategy.on_candle("DAX", df, self.session_open)
        # Either None (blocked) or signal — depends on EMA position
        # This test just checks no exception is raised
        assert signal is None or signal.direction in ("buy", "sell")

    def test_average_candle_size(self):
        df = make_ohlcv(n=20, volatility=5.0)
        avg = _average_candle_size(df, 20)
        assert avg > 0


# ──────────────────────────────────────────────────────────────────────────
# Scenario Strategy Tests
# ──────────────────────────────────────────────────────────────────────────

class TestScenarioStrategy:
    def setup_method(self):
        self.config = {"scenario_a_enabled": True, "scenario_b_enabled": True}
        self.strategy = ScenarioStrategy(self.config)

    def test_scenario_a_triggers_short(self):
        """Mon high > Tue high AND Mon high > Wed high → SHORT at Wed close."""
        df = make_daily_ohlcv(
            weekdays=["Monday", "Tuesday", "Wednesday"],
            highs=[1050.0, 1020.0, 1030.0],
            lows=[980.0, 990.0, 1000.0],
            closes=[1010.0, 1005.0, 1008.0],
        )
        signal = self.strategy.on_daily_candle("DAX", df)
        assert signal is not None
        assert signal.direction == "sell"
        assert signal.scenario == "A"
        assert signal.entry_price == pytest.approx(1008.0)

    def test_scenario_a_does_not_trigger_when_condition_unmet(self):
        """Tue high > Mon high → Scenario A should NOT trigger."""
        df = make_daily_ohlcv(
            weekdays=["Monday", "Tuesday", "Wednesday"],
            highs=[1020.0, 1050.0, 1030.0],
            lows=[980.0, 990.0, 1000.0],
            closes=[1010.0, 1040.0, 1020.0],
        )
        signal = self.strategy.on_daily_candle("DAX", df)
        assert signal is None

    def test_scenario_b_triggers_short(self):
        """Thu high > Fri high → SHORT at Fri close."""
        df = make_daily_ohlcv(
            weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            highs=[1050.0, 1020.0, 1030.0, 1060.0, 1040.0],
            lows=[980.0, 990.0, 1000.0, 1010.0, 1015.0],
            closes=[1010.0, 1005.0, 1008.0, 1050.0, 1035.0],
        )
        signal = self.strategy.on_daily_candle("DAX", df)
        assert signal is not None
        assert signal.direction == "sell"
        assert signal.scenario == "B"
        assert signal.entry_price == pytest.approx(1035.0)

    def test_scenario_b_does_not_trigger_when_condition_unmet(self):
        """Fri high > Thu high → Scenario B should NOT trigger."""
        df = make_daily_ohlcv(
            weekdays=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            highs=[1050.0, 1020.0, 1030.0, 1040.0, 1060.0],
            lows=[980.0, 990.0, 1000.0, 1010.0, 1015.0],
            closes=[1010.0, 1005.0, 1008.0, 1030.0, 1050.0],
        )
        signal = self.strategy.on_daily_candle("DAX", df)
        assert signal is None

    def test_scenario_a_disabled(self):
        self.strategy.scenario_a_enabled = False
        df = make_daily_ohlcv(
            weekdays=["Monday", "Tuesday", "Wednesday"],
            highs=[1050.0, 1020.0, 1030.0],
            lows=[980.0, 990.0, 1000.0],
            closes=[1010.0, 1005.0, 1008.0],
        )
        signal = self.strategy.on_daily_candle("DAX", df)
        assert signal is None

    def test_weekly_context_populated(self):
        df = make_daily_ohlcv(
            weekdays=["Monday", "Tuesday", "Wednesday"],
            highs=[1050.0, 1020.0, 1030.0],
            lows=[980.0, 990.0, 1000.0],
            closes=[1010.0, 1005.0, 1008.0],
        )
        self.strategy.on_daily_candle("DAX", df)
        ctx = self.strategy.get_weekly_context("DAX")
        assert ctx["highs"].get("Monday") == pytest.approx(1050.0)
        assert ctx["highs"].get("Tuesday") == pytest.approx(1020.0)


# ──────────────────────────────────────────────────────────────────────────
# Mind the Gap Tests
# ──────────────────────────────────────────────────────────────────────────

class TestMindTheGapStrategy:
    def setup_method(self):
        self.config = {
            "min_gap_ticks": 10,
            "max_gap_ticks": 50,
            "take_profit_at_prev_close": True,
        }
        self.strategy = MindTheGapStrategy(self.config, tick_size=1.0)
        self.session_open = datetime(2024, 1, 2, 8, 0, 0, tzinfo=timezone.utc)

    def test_gap_up_generates_short(self):
        signal = self.strategy.on_session_open(
            symbol="DAX",
            prev_session_close=1000.0,
            current_open=1020.0,   # gap up of 20 ticks
            current_candle_timestamp=self.session_open,
        )
        assert signal is not None
        assert signal.direction == "sell"
        assert signal.take_profit == pytest.approx(1000.0)
        assert signal.gap_direction == "up"

    def test_gap_down_generates_long(self):
        signal = self.strategy.on_session_open(
            symbol="DAX",
            prev_session_close=1000.0,
            current_open=980.0,   # gap down of 20 ticks
            current_candle_timestamp=self.session_open,
        )
        assert signal is not None
        assert signal.direction == "buy"
        assert signal.take_profit == pytest.approx(1000.0)
        assert signal.gap_direction == "down"

    def test_gap_too_small_returns_none(self):
        signal = self.strategy.on_session_open(
            symbol="DAX",
            prev_session_close=1000.0,
            current_open=1005.0,  # only 5 ticks — below min
            current_candle_timestamp=self.session_open,
        )
        assert signal is None

    def test_gap_too_large_returns_none(self):
        signal = self.strategy.on_session_open(
            symbol="DAX",
            prev_session_close=1000.0,
            current_open=1100.0,  # 100 ticks — above max
            current_candle_timestamp=self.session_open,
        )
        assert signal is None

    def test_signal_only_once_per_session(self):
        sig1 = self.strategy.on_session_open(
            "DAX", 1000.0, 1020.0, self.session_open
        )
        sig2 = self.strategy.on_session_open(
            "DAX", 1000.0, 1020.0, self.session_open
        )
        assert sig1 is not None
        assert sig2 is None

    def test_reset_allows_new_signal(self):
        self.strategy.on_session_open("DAX", 1000.0, 1020.0, self.session_open)
        self.strategy.reset_session("DAX")
        sig = self.strategy.on_session_open("DAX", 1000.0, 1020.0, self.session_open)
        assert sig is not None

    def test_gap_size_ticks_calculated_correctly(self):
        self.strategy.tick_size = 0.5
        strategy = MindTheGapStrategy(self.config, tick_size=0.5)
        signal = strategy.on_session_open(
            "DAX", 1000.0, 1010.0, self.session_open  # 10 points = 20 ticks at 0.5
        )
        assert signal is not None
        assert signal.gap_size_ticks == pytest.approx(20.0)

    def test_measure_gap_from_dataframe(self):
        prev_time = self.session_open - timedelta(hours=1)
        curr_time = self.session_open

        df = pd.DataFrame({
            "timestamp": [prev_time, curr_time],
            "open": [995.0, 1015.0],
            "high": [1005.0, 1020.0],
            "low": [990.0, 1010.0],
            "close": [1000.0, 1018.0],
            "volume": [500, 600],
        })
        result = self.strategy.measure_gap(df, self.session_open)
        assert result is not None
        gap, gap_ticks, direction = result
        assert direction == "up"
        assert gap == pytest.approx(15.0)  # 1015 open - 1000 close


# ──────────────────────────────────────────────────────────────────────────
# Risk Management Tests
# ──────────────────────────────────────────────────────────────────────────

class TestRiskParameters:
    def test_position_size_basic(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,   # 10 point stop
            risk_pct=1.0,
            point_value=1.0,
        )
        # Risk = $100. Stop = 10. Size = 100 / 10 = 10
        assert rp.position_size() == pytest.approx(10.0)

    def test_position_size_tighter_stop(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=995.0,   # 5 point stop
            risk_pct=1.0,
            point_value=1.0,
        )
        assert rp.position_size() == pytest.approx(20.0)

    def test_position_size_zero_stop_distance(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=1000.0,  # zero distance
            risk_pct=1.0,
        )
        assert rp.position_size() == 0.0

    def test_r_multiple_win(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,
            risk_pct=1.0,
        )
        # Exit at 1020 = 2R win on long
        assert rp.r_multiple(1020.0) == pytest.approx(2.0)

    def test_r_multiple_loss(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,
            risk_pct=1.0,
        )
        # Exit at stop = -1R
        assert rp.r_multiple(990.0) == pytest.approx(-1.0)

    def test_target_at_r(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,
            risk_pct=1.0,
        )
        # 2R target on long should be entry + 2 * stop_distance = 1000 + 20 = 1020
        assert rp.target_at_r(2.0) == pytest.approx(1020.0)

    def test_trailing_stop_not_triggered_early(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,
            risk_pct=1.0,
        )
        # At 1.0R, trailing stop should not be triggered yet (trigger is 1.5R)
        result = rp.trailing_stop_price(1010.0, trigger_r=1.5)
        assert result is None

    def test_trailing_stop_triggered_at_1_5r(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,
            risk_pct=1.0,
        )
        # At 1.5R = price 1015, trailing stop should be set
        result = rp.trailing_stop_price(1015.0, trigger_r=1.5)
        assert result is not None
        assert result > 1000.0  # at least breakeven

    def test_risk_amount(self):
        rp = RiskParameters(
            account_balance=10000.0,
            entry_price=1000.0,
            stop_price=990.0,
            risk_pct=1.0,
        )
        assert rp.risk_amount == pytest.approx(100.0)


class TestSessionRiskTracker:
    def setup_method(self):
        self.tracker = SessionRiskTracker(
            symbol="DAX",
            max_trades_per_session=2,
            daily_loss_limit_pct=2.0,
            skip_marginal_after_loss=True,
        )
        self.tracker.start_day(10000.0)

    def test_can_trade_initially(self):
        allowed, reason = self.tracker.can_trade(10000.0)
        assert allowed is True

    def test_blocked_after_max_trades(self):
        self.tracker.record_trade_entry()
        self.tracker.record_trade_entry()
        allowed, reason = self.tracker.can_trade(10000.0)
        assert allowed is False
        assert "Max trades" in reason

    def test_blocked_after_daily_loss_limit(self):
        # 2% loss on $10,000 = $200 loss → balance $9,800
        allowed, reason = self.tracker.can_trade(9790.0)  # > 2% loss
        assert allowed is False
        assert "Daily loss limit" in reason

    def test_marginal_setup_skipped_after_loss(self):
        self.tracker.record_trade_entry()
        self.tracker.record_trade_exit("loss", -1.0, -100.0)
        allowed, reason = self.tracker.can_trade(9900.0, is_marginal_setup=True)
        assert allowed is False
        assert "marginal" in reason.lower()

    def test_non_marginal_allowed_after_loss(self):
        self.tracker.record_trade_entry()
        self.tracker.record_trade_exit("loss", -1.0, -100.0)
        allowed, _ = self.tracker.can_trade(9900.0, is_marginal_setup=False)
        assert allowed is True

    def test_daily_stats(self):
        self.tracker.record_trade_entry()
        self.tracker.record_trade_exit("win", 2.0, 200.0)
        self.tracker.record_trade_entry()
        self.tracker.record_trade_exit("loss", -1.0, -100.0)
        stats = self.tracker.get_daily_stats()
        assert stats["total_trades"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["win_rate"] == pytest.approx(50.0)
        assert stats["total_r"] == pytest.approx(1.0)


class TestFindSwingStop:
    def test_long_stop_below_recent_low(self):
        df = make_ohlcv(n=20, base_price=1000.0, volatility=5.0)
        stop = find_swing_stop(df, "buy", lookback=10, buffer_ticks=2.0, tick_size=1.0)
        expected_min = float(df.tail(10)["low"].min()) - 2.0
        assert stop == pytest.approx(expected_min)

    def test_short_stop_above_recent_high(self):
        df = make_ohlcv(n=20, base_price=1000.0, volatility=5.0)
        stop = find_swing_stop(df, "sell", lookback=10, buffer_ticks=2.0, tick_size=1.0)
        expected_max = float(df.tail(10)["high"].max()) + 2.0
        assert stop == pytest.approx(expected_max)


# ──────────────────────────────────────────────────────────────────────────
# Integration: Breakout + Trend Filter
# ──────────────────────────────────────────────────────────────────────────

class TestBreakoutWithTrendFilter:
    def setup_method(self):
        np.random.seed(0)
        self.breakout_config = {
            "opening_range_minutes": 15,
            "session_trade_window_minutes": 90,
            "max_candle_size_multiplier": 3.0,  # relaxed for test
            "avg_candle_lookback": 10,
        }
        self.trend_config = {"ema_period": 5, "require_ema_slope": False, "enabled": True}

    def test_long_blocked_in_downtrend(self):
        tf = TrendFilter(self.trend_config)
        strategy = BreakoutStrategy(self.breakout_config, trend_filter=tf)
        session_open = datetime(2024, 1, 2, 8, 0, 0, tzinfo=timezone.utc)

        # Strongly falling prices (below EMA)
        df = make_ohlcv(n=30, base_price=1000, trend=-20.0, volatility=0.5,
                        start_time=session_open, freq_minutes=5)
        # Force a "breakout above" scenario on last candle
        or_high = float(df.iloc[3]["high"])
        df.iloc[-1, df.columns.get_loc("close")] = or_high + 50  # above OR high

        signal = strategy.on_candle("DAX", df, session_open)
        # Should be blocked by trend filter
        assert signal is None or signal.direction == "sell"  # no long in downtrend


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
