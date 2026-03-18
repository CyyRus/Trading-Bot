"""
Tom Hougaard Trading Bot — Main Entry Point

Modes:
  live / paper  : real-time trading loop (default: paper)
  backtest      : run strategies against historical CSV data

Usage:
  python main.py                    # paper trading (live loop)
  python main.py --backtest         # backtesting mode
  python main.py --config my.yaml   # custom config file
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

# ── internal imports ──────────────────────────────────────────────────────
from broker.connector import BrokerBase, OrderResult, create_broker
from risk.position_sizer import (
    RiskParameters,
    SessionRiskTracker,
    find_swing_stop,
)
from strategies.breakout import BreakoutStrategy
from strategies.mind_the_gap import MindTheGapStrategy
from strategies.scenario import ScenarioStrategy
from strategies.trend_filter import TrendFilter
from utils.database import TradeDatabase, TradeRecord
from utils.logger import log_trade_signal, print_daily_summary, setup_logging

logger = logging.getLogger(__name__)

# ── globals ───────────────────────────────────────────────────────────────
_running = True


def _handle_sigint(sig, frame):
    global _running
    logger.info("Shutdown signal received.")
    _running = False


signal.signal(signal.SIGINT, _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigint)


# ═══════════════════════════════════════════════════════════════════════════
# Config loader
# ═══════════════════════════════════════════════════════════════════════════

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        # Fall back to sibling directory
        config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
# Active Trade Manager
# ═══════════════════════════════════════════════════════════════════════════

class ActiveTrade:
    """Tracks an open trade and manages trailing stops."""

    def __init__(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_price: float,
        target_price: Optional[float],
        position_size: float,
        risk_amount: float,
        strategy: str,
        db_id: int,
        risk_params: RiskParameters,
        trailing_stop_trigger_r: float = 1.5,
    ):
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.target_price = target_price
        self.position_size = position_size
        self.risk_amount = risk_amount
        self.strategy = strategy
        self.db_id = db_id
        self.risk_params = risk_params
        self.trailing_stop_trigger_r = trailing_stop_trigger_r
        self.trailing_active = False
        self.entry_time = datetime.utcnow()

    def update_trailing_stop(self, current_price: float, broker: BrokerBase) -> None:
        """Check and update trailing stop if conditions are met."""
        new_stop = self.risk_params.trailing_stop_price(current_price, self.trailing_stop_trigger_r)
        if new_stop is None:
            return

        # Only move stop in the favourable direction (never widen it)
        if self.direction == "buy":
            if new_stop > self.stop_price:
                self.stop_price = new_stop
                broker.update_stop_loss(self.symbol, new_stop)
                if not self.trailing_active:
                    self.trailing_active = True
                    logger.info(
                        "[TRAIL] %s LONG: trailing stop activated @ %.4f", self.symbol, new_stop
                    )
        else:
            if new_stop < self.stop_price:
                self.stop_price = new_stop
                broker.update_stop_loss(self.symbol, new_stop)
                if not self.trailing_active:
                    self.trailing_active = True
                    logger.info(
                        "[TRAIL] %s SHORT: trailing stop activated @ %.4f", self.symbol, new_stop
                    )

    def is_stopped_out(self, current_price: float) -> bool:
        """Check if the current price has hit the stop."""
        if self.direction == "buy":
            return current_price <= self.stop_price
        else:
            return current_price >= self.stop_price

    def is_target_hit(self, current_price: float) -> bool:
        """Check if the target has been reached."""
        if self.target_price is None:
            return False
        if self.direction == "buy":
            return current_price >= self.target_price
        else:
            return current_price <= self.target_price

    def calculate_outcome(self, exit_price: float) -> tuple[str, float, float]:
        """
        Returns (outcome, r_multiple, pnl).
        outcome: "win" | "loss" | "breakeven"
        """
        r = self.risk_params.r_multiple(exit_price)
        pnl = r * self.risk_amount
        if r > 0.05:
            outcome = "win"
        elif r < -0.05:
            outcome = "loss"
        else:
            outcome = "breakeven"
        return outcome, r, pnl


# ═══════════════════════════════════════════════════════════════════════════
# Bot Core
# ═══════════════════════════════════════════════════════════════════════════

class TradingBot:
    """
    Orchestrates all strategies, risk management, and order execution.
    """

    def __init__(self, config: dict):
        self.config = config
        self.broker_cfg = config["broker"]
        self.risk_cfg = config["risk"]
        self.account_cfg = config["account"]
        self.instruments = config["instruments"]

        # Setup broker
        self.broker: BrokerBase = create_broker(
            self.broker_cfg,
            initial_balance=self.account_cfg.get("initial_balance", 10000.0),
        )

        # Setup database
        self.db = TradeDatabase(config["database"]["path"])

        # Setup strategies
        self.trend_filter = TrendFilter(config["trend_filter"])
        self.breakout_strategy = BreakoutStrategy(
            config["breakout"], trend_filter=self.trend_filter
        )
        self.scenario_strategy = ScenarioStrategy(config["scenario"])
        self.gap_strategy = MindTheGapStrategy(config["mind_the_gap"])

        # Per-instrument risk trackers
        self.risk_trackers: dict[str, SessionRiskTracker] = {
            instr["symbol"]: SessionRiskTracker(
                symbol=instr["symbol"],
                max_trades_per_session=self.risk_cfg["max_trades_per_session"],
                daily_loss_limit_pct=self.risk_cfg["daily_loss_limit_pct"],
                skip_marginal_after_loss=self.risk_cfg.get("skip_marginal_after_loss", True),
            )
            for instr in self.instruments
        }

        # Active trades: symbol → ActiveTrade
        self.active_trades: dict[str, ActiveTrade] = {}

        # State
        self._session_started: dict[str, bool] = {}
        self._day_started = False
        self._starting_balance: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect to broker and initialise session."""
        if not self.broker.connect():
            logger.error("Failed to connect to broker. Exiting.")
            sys.exit(1)

        self._starting_balance = self.broker.get_account_balance()
        logger.info(
            "Bot started. Balance: $%.2f | Paper: %s",
            self._starting_balance,
            self.broker_cfg.get("paper_trading", True),
        )

        for instr in self.instruments:
            self.risk_trackers[instr["symbol"]].start_day(self._starting_balance)

    def shutdown(self) -> None:
        """Close any open positions and print summary."""
        logger.info("Shutting down...")
        for symbol in list(self.active_trades.keys()):
            logger.info("Closing open position: %s", symbol)
            self.broker.close_position(symbol)

        balance = self.broker.get_account_balance()
        all_stats = []
        for tracker in self.risk_trackers.values():
            stats = tracker.get_daily_stats()
            all_stats.extend([{"outcome": "win"}] * stats["wins"])
            all_stats.extend([{"outcome": "loss"}] * stats["losses"])

        print_daily_summary(logger, datetime.utcnow(), all_stats, self._starting_balance, balance)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Live trading loop. Polls each instrument at configurable intervals."""
        self.start()
        logger.info("Entering main loop. Press Ctrl+C to stop.")

        poll_interval = 30  # seconds

        while _running:
            try:
                self._tick()
            except Exception as exc:
                logger.error("Unhandled error in main loop: %s", exc, exc_info=True)

            time.sleep(poll_interval)

        self.shutdown()

    def _tick(self) -> None:
        """Single tick: process all instruments."""
        now_utc = datetime.now(timezone.utc)

        for instr in self.instruments:
            symbol = instr["symbol"]
            timeframe = self.config["data"]["timeframe"]
            lookback = self.config["data"]["lookback_candles"]

            try:
                df = self.broker.get_ohlcv(symbol, timeframe, lookback)
                if df.empty:
                    continue

                # Manage existing position
                if symbol in self.active_trades:
                    self._manage_position(symbol, df, instr)
                    continue

                # Check for new signals
                self._check_signals(symbol, df, instr, now_utc)

            except Exception as exc:
                logger.error("Error processing %s: %s", symbol, exc, exc_info=True)

    # ------------------------------------------------------------------
    # Signal checking
    # ------------------------------------------------------------------

    def _check_signals(
        self, symbol: str, df: pd.DataFrame, instr: dict, now_utc: datetime
    ) -> None:
        """Check all strategies for entry signals."""
        tracker = self.risk_trackers[symbol]
        balance = self.broker.get_account_balance()

        can_trade, reason = tracker.can_trade(balance)
        if not can_trade:
            logger.debug("[%s] Cannot trade: %s", symbol, reason)
            return

        tick_size = instr.get("tick_size", 1.0)
        point_value = instr.get("point_value", 1.0)

        # 1. Mind the Gap (first candle of session)
        session_open = self._get_session_open_time(instr, now_utc)
        if session_open and not self._session_started.get(symbol):
            gap_signal = self._check_gap(symbol, df, instr, session_open)
            if gap_signal:
                self._execute_signal(
                    symbol=symbol,
                    direction=gap_signal.direction,
                    entry=gap_signal.entry_price,
                    stop=gap_signal.stop_price,
                    target=gap_signal.take_profit,
                    strategy="mind_the_gap",
                    instr=instr,
                    balance=balance,
                )
                return

        # 2. Breakout strategy
        if session_open:
            breakout_signal = self.breakout_strategy.on_candle(symbol, df, session_open)
            if breakout_signal:
                stop = find_swing_stop(
                    df,
                    breakout_signal.direction,
                    lookback=10,
                    buffer_ticks=2.0,
                    tick_size=tick_size,
                )
                stop = stop or breakout_signal.stop_price
                self._execute_signal(
                    symbol=symbol,
                    direction=breakout_signal.direction,
                    entry=breakout_signal.entry_price,
                    stop=stop,
                    target=None,
                    strategy="breakout",
                    instr=instr,
                    balance=balance,
                )
                return

        # 3. Scenario strategy (daily candles)
        df_daily = self.broker.get_ohlcv(symbol, "1d", 10)
        if not df_daily.empty:
            scenario_signal = self.scenario_strategy.on_daily_candle(symbol, df_daily)
            if scenario_signal:
                stop = find_swing_stop(
                    df_daily,
                    scenario_signal.direction,
                    lookback=5,
                    buffer_ticks=3.0,
                    tick_size=tick_size,
                )
                stop = stop or scenario_signal.stop_price
                self._execute_signal(
                    symbol=symbol,
                    direction=scenario_signal.direction,
                    entry=scenario_signal.entry_price,
                    stop=stop,
                    target=None,
                    strategy=f"scenario_{scenario_signal.scenario}",
                    instr=instr,
                    balance=balance,
                )

    def _check_gap(self, symbol: str, df: pd.DataFrame, instr: dict, session_open: datetime):
        """Check for a gap at session open."""
        gap_result = self.gap_strategy.measure_gap(df, session_open)
        if gap_result is None:
            return None

        _, _, _ = gap_result

        # Get prev close and current open from data
        ts = pd.to_datetime(df["timestamp"])
        open_ts = pd.Timestamp(session_open)
        before = df[ts < open_ts]
        after = df[ts >= open_ts]

        if before.empty or after.empty:
            return None

        prev_close = float(before.iloc[-1]["close"])
        curr_open = float(after.iloc[0]["open"])
        curr_ts = after.iloc[0]["timestamp"]
        curr_ts_dt = pd.Timestamp(curr_ts).to_pydatetime()

        tick_size = instr.get("tick_size", 1.0)
        self.gap_strategy.tick_size = tick_size

        return self.gap_strategy.on_session_open(
            symbol=symbol,
            prev_session_close=prev_close,
            current_open=curr_open,
            current_candle_timestamp=curr_ts_dt,
        )

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def _execute_signal(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: Optional[float],
        strategy: str,
        instr: dict,
        balance: float,
    ) -> None:
        """Size and execute a trade signal."""
        risk_pct = self.risk_cfg["max_risk_per_trade_pct"]
        tick_size = instr.get("tick_size", 1.0)
        point_value = instr.get("point_value", 1.0)

        rp = RiskParameters(
            account_balance=balance,
            entry_price=entry,
            stop_price=stop,
            risk_pct=risk_pct,
            tick_size=tick_size,
            point_value=point_value,
        )

        size = rp.position_size()
        if size <= 0:
            logger.error("[%s] Position size calculated as 0 — skipping trade.", symbol)
            return

        log_trade_signal(logger, strategy, symbol, direction, entry, stop, target, size)

        order: Optional[OrderResult] = self.broker.place_market_order(
            symbol=symbol,
            side=direction,
            qty=size,
            stop_loss=stop,
            take_profit=target,
        )

        if order is None or order.status not in ("filled", "accepted", "new"):
            logger.error("[%s] Order failed or not filled.", symbol)
            return

        filled_entry = order.filled_price or entry

        # Save to database
        trade_rec = TradeRecord(
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            entry_price=filled_entry,
            stop_price=stop,
            target_price=target,
            position_size=size,
            risk_amount=rp.risk_amount,
            entry_time=datetime.utcnow(),
        )
        db_id = self.db.insert_trade(trade_rec)

        # Track active trade
        self.active_trades[symbol] = ActiveTrade(
            symbol=symbol,
            direction=direction,
            entry_price=filled_entry,
            stop_price=stop,
            target_price=target,
            position_size=size,
            risk_amount=rp.risk_amount,
            strategy=strategy,
            db_id=db_id,
            risk_params=rp,
            trailing_stop_trigger_r=self.risk_cfg.get("trailing_stop_trigger_r", 1.5),
        )

        self.risk_trackers[symbol].record_trade_entry()

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _manage_position(
        self, symbol: str, df: pd.DataFrame, instr: dict
    ) -> None:
        """Manage an existing open position: trailing stop, target, stop check."""
        trade = self.active_trades[symbol]
        current_price = float(df["close"].iloc[-1])

        # Update trailing stop
        trade.update_trailing_stop(current_price, self.broker)

        # Check stop
        if trade.is_stopped_out(current_price):
            self._close_trade(symbol, current_price, "stopped_out")
            return

        # Check target
        if trade.is_target_hit(current_price):
            self._close_trade(symbol, current_price, "target_hit")

    def _close_trade(self, symbol: str, exit_price: float, reason: str) -> None:
        """Close a position and record the outcome."""
        trade = self.active_trades.pop(symbol, None)
        if trade is None:
            return

        self.broker.close_position(symbol)
        outcome, r_multiple, pnl = trade.calculate_outcome(exit_price)

        logger.info(
            "[CLOSE] %s | %s | R: %+.2f | PnL: %+.2f | Reason: %s",
            symbol, outcome.upper(), r_multiple, pnl, reason,
        )

        # Update DB
        self.db.update_trade_exit(
            trade_id=trade.db_id,
            exit_time=datetime.utcnow(),
            exit_price=exit_price,
            outcome=outcome,
            r_multiple=r_multiple,
            pnl=pnl,
        )

        # Update risk tracker
        self.risk_trackers[symbol].record_trade_exit(outcome, r_multiple, pnl)

    # ------------------------------------------------------------------
    # Session time helpers
    # ------------------------------------------------------------------

    def _get_session_open_time(
        self, instr: dict, now_utc: datetime
    ) -> Optional[datetime]:
        """Return today's session open as a UTC-aware datetime, or None if not relevant."""
        import pytz

        tz_name = instr.get("timezone", "UTC")
        session_open_utc_str = instr.get("session_open_utc", "08:00")

        try:
            h, m = map(int, session_open_utc_str.split(":"))
            session_open = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
            return session_open
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# Backtesting Engine
# ═══════════════════════════════════════════════════════════════════════════

class Backtester:
    """
    Simple event-driven backtester.
    Feeds historical OHLCV data from CSV files through the strategies.
    """

    def __init__(self, config: dict):
        self.config = config
        self.bt_cfg = config["backtesting"]
        self.initial_balance = self.bt_cfg.get("initial_balance", 10000.0)
        self.commission = self.bt_cfg.get("commission_per_trade", 2.0)
        self.data_path = config["data"]["csv_data_path"]

        self.trend_filter = TrendFilter(config["trend_filter"])
        self.breakout_strategy = BreakoutStrategy(
            config["breakout"], trend_filter=self.trend_filter
        )
        self.scenario_strategy = ScenarioStrategy(config["scenario"])
        self.gap_strategy = MindTheGapStrategy(config["mind_the_gap"])

        self.db = TradeDatabase(config["database"]["path"].replace(".db", "_backtest.db"))
        self.results: list[dict] = []

    def run(self, symbol: str, csv_file: str) -> dict:
        """
        Run backtest for a single instrument.

        Parameters
        ----------
        symbol   : instrument name
        csv_file : path to OHLCV CSV (columns: timestamp,open,high,low,close,volume)
        """
        logger.info("Starting backtest for %s from %s", symbol, csv_file)

        df = pd.read_csv(csv_file, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Apply date filter
        start = pd.Timestamp(self.bt_cfg.get("start_date", "2020-01-01"))
        end = pd.Timestamp(self.bt_cfg.get("end_date", "2025-01-01"))
        df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]

        balance = self.initial_balance
        trades: list[dict] = []
        open_trade: Optional[dict] = None
        session_open_time = None

        # Find instrument config
        instr = next(
            (i for i in self.config["instruments"] if i["symbol"] == symbol),
            {"symbol": symbol, "tick_size": 1.0, "point_value": 1.0},
        )
        tick_size = instr.get("tick_size", 1.0)
        point_value = instr.get("point_value", 1.0)

        self.breakout_strategy.reset_session(symbol)
        self.gap_strategy.reset_session(symbol)

        for i in range(1, len(df)):
            candle = df.iloc[i]
            candle_time = pd.Timestamp(candle["timestamp"])
            df_so_far = df.iloc[: i + 1].copy()

            # Detect new day / new session
            prev_candle = df.iloc[i - 1]
            new_day = candle_time.date() != pd.Timestamp(prev_candle["timestamp"]).date()
            if new_day:
                # Save previous day's session open time from config
                session_open_str = instr.get("session_open_utc", "08:00")
                h, m = map(int, session_open_str.split(":"))
                session_open_time = candle_time.replace(
                    hour=h, minute=m, second=0, microsecond=0, tzinfo=timezone.utc
                )
                self.breakout_strategy.reset_session(symbol)
                self.gap_strategy.reset_session(symbol)

            # Manage open trade
            if open_trade:
                close = float(candle["close"])
                high = float(candle["high"])
                low = float(candle["low"])
                direction = open_trade["direction"]
                stop = open_trade["stop"]
                target = open_trade.get("target")

                stopped = (direction == "buy" and low <= stop) or \
                          (direction == "sell" and high >= stop)
                targeted = target is not None and (
                    (direction == "buy" and high >= target) or
                    (direction == "sell" and low <= target)
                )

                if stopped or targeted:
                    exit_price = stop if stopped else target
                    entry = open_trade["entry"]
                    risk = abs(entry - stop)
                    if risk > 0:
                        if direction == "buy":
                            r = (exit_price - entry) / risk
                        else:
                            r = (entry - exit_price) / risk
                    else:
                        r = 0.0

                    pnl = r * open_trade["risk_amount"] - self.commission
                    balance += pnl
                    outcome = "win" if r > 0 else ("loss" if r < 0 else "breakeven")
                    open_trade.update({
                        "exit_price": exit_price,
                        "exit_time": str(candle_time),
                        "outcome": outcome,
                        "r_multiple": r,
                        "pnl": pnl,
                        "balance_after": balance,
                    })
                    trades.append(open_trade)
                    logger.debug(
                        "[BT] CLOSE %s %s @ %.2f | R: %+.2f | PnL: %+.2f | Bal: %.2f",
                        symbol, outcome, exit_price, r, pnl, balance,
                    )
                    open_trade = None
                continue

            # Check strategies for signals
            signal = None

            # Gap strategy
            if session_open_time and i > 1:
                gap_sig = self.gap_strategy.on_session_open(
                    symbol=symbol,
                    prev_session_close=float(df.iloc[i - 1]["close"]),
                    current_open=float(candle["open"]),
                    current_candle_timestamp=candle_time.to_pydatetime(),
                )
                if gap_sig:
                    signal = {
                        "direction": gap_sig.direction,
                        "entry": gap_sig.entry_price,
                        "stop": gap_sig.stop_price,
                        "target": gap_sig.take_profit,
                        "strategy": "mind_the_gap",
                    }

            # Breakout strategy
            if signal is None and session_open_time:
                br_sig = self.breakout_strategy.on_candle(symbol, df_so_far, session_open_time)
                if br_sig:
                    signal = {
                        "direction": br_sig.direction,
                        "entry": br_sig.entry_price,
                        "stop": br_sig.stop_price,
                        "target": None,
                        "strategy": "breakout",
                    }

            # Scenario strategy (daily only — skip intraday bars)
            if signal is None and candle_time.hour == 23 and candle_time.minute >= 55:
                df_daily = df_so_far.resample("1D", on="timestamp").agg(
                    {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
                ).dropna().reset_index()
                sc_sig = self.scenario_strategy.on_daily_candle(symbol, df_daily)
                if sc_sig:
                    signal = {
                        "direction": sc_sig.direction,
                        "entry": sc_sig.entry_price,
                        "stop": sc_sig.stop_price,
                        "target": None,
                        "strategy": f"scenario_{sc_sig.scenario}",
                    }

            if signal:
                rp = RiskParameters(
                    account_balance=balance,
                    entry_price=signal["entry"],
                    stop_price=signal["stop"],
                    risk_pct=self.config["risk"]["max_risk_per_trade_pct"],
                    tick_size=tick_size,
                    point_value=point_value,
                )
                size = rp.position_size()
                risk_amount = rp.risk_amount

                open_trade = {
                    "symbol": symbol,
                    "direction": signal["direction"],
                    "entry": signal["entry"],
                    "stop": signal["stop"],
                    "target": signal.get("target"),
                    "size": size,
                    "risk_amount": risk_amount,
                    "strategy": signal["strategy"],
                    "entry_time": str(candle_time),
                }
                logger.debug(
                    "[BT] ENTER %s %s @ %.2f | SL: %.2f | Strategy: %s",
                    symbol, signal["direction"].upper(), signal["entry"],
                    signal["stop"], signal["strategy"],
                )

        # Print summary
        self._print_backtest_summary(symbol, trades, self.initial_balance, balance)
        self.results = trades
        return {
            "symbol": symbol,
            "initial_balance": self.initial_balance,
            "final_balance": balance,
            "total_trades": len(trades),
            "trades": trades,
        }

    def _print_backtest_summary(
        self,
        symbol: str,
        trades: list[dict],
        start_bal: float,
        end_bal: float,
    ) -> None:
        total = len(trades)
        wins = [t for t in trades if t.get("outcome") == "win"]
        losses = [t for t in trades if t.get("outcome") == "loss"]
        total_r = sum(t.get("r_multiple", 0) for t in trades)
        pnl = end_bal - start_bal

        print("\n" + "=" * 60)
        print(f"  BACKTEST RESULTS — {symbol}")
        print("=" * 60)
        print(f"  Period        : {self.bt_cfg.get('start_date')} → {self.bt_cfg.get('end_date')}")
        print(f"  Total Trades  : {total}")
        print(f"  Wins / Losses : {len(wins)} / {len(losses)}")
        wr = len(wins) / total * 100 if total > 0 else 0
        print(f"  Win Rate      : {wr:.1f}%")
        print(f"  Total R       : {total_r:+.2f}R")
        print(f"  P&L           : ${pnl:+.2f} ({pnl / start_bal * 100:+.2f}%)")
        print(f"  End Balance   : ${end_bal:.2f}")
        print("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Tom Hougaard Trading Bot")
    parser.add_argument(
        "--config", default="config.yaml", help="Path to config file"
    )
    parser.add_argument(
        "--backtest", action="store_true", help="Run in backtesting mode"
    )
    parser.add_argument(
        "--symbol", default=None, help="Symbol to backtest (overrides config)"
    )
    parser.add_argument(
        "--csv", default=None, help="CSV file for backtesting"
    )
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Setup logging
    setup_logging(
        config["logging"],
        use_colors=config["logging"].get("console_colors", True),
    )

    if args.backtest or config["backtesting"].get("enabled", False):
        # Backtesting mode
        backtester = Backtester(config)
        symbol = args.symbol or config["instruments"][0]["symbol"]
        csv_file = args.csv or os.path.join(
            config["data"]["csv_data_path"],
            f"{symbol}_ohlcv.csv",
        )
        if not os.path.exists(csv_file):
            logger.error("CSV file not found: %s", csv_file)
            sys.exit(1)
        backtester.run(symbol, csv_file)
    else:
        # Live / paper trading mode
        bot = TradingBot(config)
        bot.run()


if __name__ == "__main__":
    main()
