"""
Tom Hougaard Trading Bot — MT5 Gold (XAUUSD) Runner
====================================================

Standalone script that connects to MetaTrader 5, runs all four
Tom Hougaard strategies on XAUUSD, and manages positions with
the full risk-management rule set.

Usage
-----
Paper / demo (default):
    python run_mt5_gold.py

Live account (set paper_trading: false in config_gold.yaml first!):
    python run_mt5_gold.py --live

Backtest from CSV:
    python run_mt5_gold.py --backtest --csv data/XAUUSD_M5.csv

Custom config file:
    python run_mt5_gold.py --config my_config.yaml

Requirements
------------
    pip install MetaTrader5 pandas numpy PyYAML ta pytz
    MT5 terminal must be running on the same Windows machine.
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

# ── path setup so we can import sibling packages ──────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from broker.connector import BrokerBase, OrderResult, Position
from broker.mt5_connector import MT5Connector
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

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
SYMBOL = "XAUUSD"          # change if your broker uses GOLD / XAUUSDm
POLL_INTERVAL_SECONDS = 30  # how often to check for new candles

_running = True


def _sighandler(sig, frame):
    global _running
    logger.info("Shutdown signal received — finishing current tick.")
    _running = False


signal.signal(signal.SIGINT, _sighandler)
signal.signal(signal.SIGTERM, _sighandler)


# ══════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════

def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        p = Path(__file__).parent / path
    if not p.exists():
        p = Path(__file__).parent / "config_gold.yaml"
    with open(p) as f:
        return yaml.safe_load(f)


# ══════════════════════════════════════════════════════════════════════
# Gold-specific helpers
# ══════════════════════════════════════════════════════════════════════

def gold_point_value(lot_size: float = 1.0) -> float:
    """
    For XAUUSD standard lots:
    1 lot = 100 oz.  A $1 move = $100 P&L per lot.
    Returns dollar value of 1 point (0.01) per lot.
    """
    return 100.0 * lot_size * 0.01  # = $1 per lot per $0.01 move


def lots_for_risk(
    balance: float,
    risk_pct: float,
    stop_distance_usd: float,
) -> float:
    """
    Calculate Gold lot size so that hitting the stop loses exactly risk_pct% of balance.

    lot_size = (balance × risk_pct%) / (stop_distance_usd × 100)
    (100 = ounces per standard lot)
    """
    if stop_distance_usd <= 0:
        return 0.0
    risk_usd = balance * risk_pct / 100.0
    return round(risk_usd / (stop_distance_usd * 100.0), 2)


def session_open_today(open_utc_str: str) -> datetime:
    """Return today's session open as a UTC-aware datetime."""
    h, m = map(int, open_utc_str.split(":"))
    now = datetime.now(timezone.utc)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


# ══════════════════════════════════════════════════════════════════════
# Active trade tracker
# ══════════════════════════════════════════════════════════════════════

class GoldTrade:
    """Tracks a single open Gold trade and manages trailing stop."""

    def __init__(
        self,
        direction: str,
        entry_price: float,
        stop_price: float,
        target_price: Optional[float],
        lot_size: float,
        risk_usd: float,
        strategy: str,
        db_id: int,
        trailing_trigger_r: float = 1.5,
    ):
        self.direction = direction
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.target_price = target_price
        self.lot_size = lot_size
        self.risk_usd = risk_usd
        self.strategy = strategy
        self.db_id = db_id
        self.trailing_trigger_r = trailing_trigger_r
        self.trailing_active = False
        self.entry_time = datetime.now(timezone.utc)

        # R-distance in price
        self.r_distance = abs(entry_price - stop_price)

    def current_r(self, price: float) -> float:
        if self.r_distance == 0:
            return 0.0
        if self.direction == "buy":
            return (price - self.entry_price) / self.r_distance
        else:
            return (self.entry_price - price) / self.r_distance

    def should_trail(self, price: float) -> bool:
        return self.current_r(price) >= self.trailing_trigger_r

    def new_trailing_stop(self, price: float) -> Optional[float]:
        """
        Once at 1.5R, trail stop to lock in 0.5R minimum.
        Continues moving as price extends.
        """
        r = self.current_r(price)
        if r < self.trailing_trigger_r:
            return None

        # Lock in: move stop to entry + 0.5 × R-distance (0.5R profit lock)
        lock_r = max(0.0, r - 1.0)  # keep 1R buffer from current price
        if self.direction == "buy":
            return self.entry_price + lock_r * self.r_distance
        else:
            return self.entry_price - lock_r * self.r_distance

    def is_stopped(self, bid: float, ask: float) -> bool:
        if self.direction == "buy":
            return bid <= self.stop_price
        else:
            return ask >= self.stop_price

    def is_target_hit(self, bid: float, ask: float) -> bool:
        if self.target_price is None:
            return False
        if self.direction == "buy":
            return bid >= self.target_price
        else:
            return ask <= self.target_price

    def pnl(self, exit_price: float) -> tuple[str, float, float]:
        """Returns (outcome, r_multiple, pnl_usd)."""
        r = self.current_r(exit_price)
        pnl_usd = r * self.risk_usd
        outcome = "win" if r > 0.05 else ("loss" if r < -0.05 else "breakeven")
        return outcome, r, pnl_usd


# ══════════════════════════════════════════════════════════════════════
# Main Bot
# ══════════════════════════════════════════════════════════════════════

class GoldBot:
    """
    Tom Hougaard bot wired for XAUUSD on MetaTrader 5.
    Runs all four strategies with full risk management.
    """

    def __init__(self, config: dict):
        self.config = config
        self.symbol = config["instruments"][0]["symbol"]
        self.instr = config["instruments"][0]
        self.risk_cfg = config["risk"]
        self.data_cfg = config["data"]

        # Broker
        self.broker = MT5Connector(config["broker"])

        # Database
        self.db = TradeDatabase(config["database"]["path"])

        # Strategies
        self.trend_filter = TrendFilter(config["trend_filter"])
        self.breakout = BreakoutStrategy(
            config["breakout"], trend_filter=self.trend_filter
        )
        self.scenario = ScenarioStrategy(config["scenario"])
        self.gap = MindTheGapStrategy(
            config["mind_the_gap"],
            tick_size=self.instr.get("tick_size", 0.01),
        )

        # Risk tracker
        self.tracker = SessionRiskTracker(
            symbol=self.symbol,
            max_trades_per_session=self.risk_cfg["max_trades_per_session"],
            daily_loss_limit_pct=self.risk_cfg["daily_loss_limit_pct"],
            skip_marginal_after_loss=self.risk_cfg.get("skip_marginal_after_loss", True),
        )

        # State
        self.active_trade: Optional[GoldTrade] = None
        self._session_open: Optional[datetime] = None
        self._gap_checked = False
        self._last_daily_check: Optional[datetime] = None
        self._starting_balance: float = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if not self.broker.connect():
            logger.error("Cannot connect to MT5. Is the terminal running?")
            sys.exit(1)

        self._starting_balance = self.broker.get_account_balance()
        logger.info(
            "GoldBot started | Symbol: %s | Balance: $%.2f | Paper: %s",
            self.symbol,
            self._starting_balance,
            self.config["broker"].get("paper_trading", True),
        )

        self._log_symbol_info()
        self.tracker.start_day(self._starting_balance)

    def _log_symbol_info(self) -> None:
        info = self.broker.get_symbol_info(self.symbol)
        if info:
            logger.info(
                "%s | Digits: %d | Tick: %.4f | Contract: %.0f oz | Spread: %d pts",
                self.symbol,
                info.get("digits", 2),
                info.get("tick_size", 0.01),
                info.get("contract_size", 100),
                info.get("spread", 0),
            )

    def shutdown(self) -> None:
        if self.active_trade:
            logger.info("Closing open Gold position before shutdown.")
            result = self.broker.close_position(self.symbol)
            if result:
                self._record_close(result.filled_price, "shutdown")

        balance = self.broker.get_account_balance()
        stats = self.tracker.get_daily_stats()
        all_trades = (
            [{"outcome": "win"}] * stats["wins"] +
            [{"outcome": "loss"}] * stats["losses"]
        )
        print_daily_summary(
            logger, datetime.now(timezone.utc),
            all_trades, self._starting_balance, balance,
        )
        self.broker.disconnect()

    # ── main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        self.start()
        logger.info("Running Gold bot. Press Ctrl+C to stop.")

        while _running:
            try:
                self._tick()
            except Exception as exc:
                logger.error("Tick error: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL_SECONDS)

        self.shutdown()

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        # ── reset at start of new trading day ──────────────────────
        if self._is_new_day(now):
            balance = self.broker.get_account_balance()
            self.tracker.start_day(balance)
            self.breakout.reset_session(self.symbol)
            self.gap.reset_session(self.symbol)
            self._gap_checked = False
            self._session_open = None
            logger.info("New trading day — session reset.")

        # ── determine session open for today ───────────────────────
        if self._session_open is None:
            open_utc = self.instr.get("session_open_utc", "08:00")
            self._session_open = session_open_today(open_utc)

        # ── fetch candles ──────────────────────────────────────────
        tf = self.data_cfg["timeframe"]
        lookback = self.data_cfg["lookback_candles"]
        df = self.broker.get_ohlcv(self.symbol, tf, lookback)
        if df.empty:
            logger.warning("No OHLCV data received for %s.", self.symbol)
            return

        # ── manage existing position ───────────────────────────────
        if self.active_trade is not None:
            self._manage_position(now)
            return  # one trade at a time

        # ── check if we can trade ──────────────────────────────────
        balance = self.broker.get_account_balance()
        can_trade, reason = self.tracker.can_trade(balance)
        if not can_trade:
            logger.debug("Cannot trade: %s", reason)
            return

        # ── strategy checks ────────────────────────────────────────
        self._check_gap(df, now)
        if self.active_trade:
            return

        self._check_breakout(df, now)
        if self.active_trade:
            return

        self._check_scenario(df, now)

    # ── gap strategy ─────────────────────────────────────────────────

    def _check_gap(self, df: pd.DataFrame, now: datetime) -> None:
        if not self.config["mind_the_gap"].get("enabled", True):
            return
        if self._gap_checked:
            return

        # Only check gap within 5 minutes of session open
        session_open = self._session_open
        if session_open is None:
            return
        if now < session_open or now > session_open + timedelta(minutes=5):
            return

        # Measure gap
        gap_result = self.gap.measure_gap(df, session_open)
        if gap_result is None:
            return

        _, gap_ticks, _ = gap_result
        ts = pd.to_datetime(df["timestamp"])
        before = df[ts < pd.Timestamp(session_open)]
        after = df[ts >= pd.Timestamp(session_open)]

        if before.empty or after.empty:
            return

        prev_close = float(before.iloc[-1]["close"])
        curr_open = float(after.iloc[0]["open"])
        curr_ts = pd.Timestamp(after.iloc[0]["timestamp"]).to_pydatetime()

        signal = self.gap.on_session_open(
            symbol=self.symbol,
            prev_session_close=prev_close,
            current_open=curr_open,
            current_candle_timestamp=curr_ts,
        )
        self._gap_checked = True

        if signal:
            self._enter_trade(
                direction=signal.direction,
                entry=signal.entry_price,
                stop=signal.stop_price,
                target=signal.take_profit,
                strategy="mind_the_gap",
            )

    # ── breakout strategy ────────────────────────────────────────────

    def _check_breakout(self, df: pd.DataFrame, now: datetime) -> None:
        if not self.config["breakout"].get("enabled", True):
            return
        if self._session_open is None:
            return

        signal = self.breakout.on_candle(self.symbol, df, self._session_open)
        if signal:
            # Refine stop to actual swing point
            stop = find_swing_stop(
                df, signal.direction,
                lookback=10,
                buffer_ticks=3.0,
                tick_size=self.instr.get("tick_size", 0.01),
            ) or signal.stop_price

            self._enter_trade(
                direction=signal.direction,
                entry=signal.entry_price,
                stop=stop,
                target=None,   # trail-managed
                strategy="breakout",
            )

    # ── scenario strategy ────────────────────────────────────────────

    def _check_scenario(self, df: pd.DataFrame, now: datetime) -> None:
        if not self.config["scenario"].get("enabled", True):
            return

        # Only check at daily close (23:55+ UTC)
        if now.hour != 23 or now.minute < 50:
            return
        if self._last_daily_check and self._last_daily_check.date() == now.date():
            return

        daily_tf = self.data_cfg.get("daily_timeframe", "1d")
        df_daily = self.broker.get_ohlcv(self.symbol, daily_tf, 10)
        if df_daily.empty:
            return

        self._last_daily_check = now
        signal = self.scenario.on_daily_candle(self.symbol, df_daily)
        if signal:
            stop = find_swing_stop(
                df_daily, signal.direction,
                lookback=5,
                buffer_ticks=5.0,
                tick_size=self.instr.get("tick_size", 0.01),
            ) or signal.stop_price

            self._enter_trade(
                direction=signal.direction,
                entry=signal.entry_price,
                stop=stop,
                target=None,
                strategy=f"scenario_{signal.scenario}",
            )

    # ── order execution ───────────────────────────────────────────────

    def _enter_trade(
        self,
        direction: str,
        entry: float,
        stop: float,
        target: Optional[float],
        strategy: str,
    ) -> None:
        balance = self.broker.get_account_balance()
        risk_pct = self.risk_cfg["max_risk_per_trade_pct"]
        stop_distance = abs(entry - stop)

        # Gold lot sizing
        lot_size = lots_for_risk(balance, risk_pct, stop_distance)
        info = self.broker.get_symbol_info(self.symbol)
        min_lot = info.get("min_lot", 0.01)
        lot_step = info.get("lot_step", 0.01)
        lot_size = max(lot_size, min_lot)
        lot_size = round(round(lot_size / lot_step) * lot_step, 2)

        risk_usd = balance * risk_pct / 100.0

        log_trade_signal(
            logger, strategy, self.symbol, direction,
            entry, stop, target, lot_size,
        )
        logger.info(
            "  Lot size: %.2f | Risk: $%.2f | Stop distance: $%.2f",
            lot_size, risk_usd, stop_distance,
        )

        result: Optional[OrderResult] = self.broker.place_market_order(
            symbol=self.symbol,
            side=direction,
            qty=lot_size,
            stop_loss=stop,
            take_profit=target,
        )

        if result is None or result.status not in ("filled", "accepted", "new", "pending"):
            logger.error("Order not filled for %s.", self.symbol)
            return

        filled_entry = result.filled_price or entry

        # Save to DB
        trade_rec = TradeRecord(
            symbol=self.symbol,
            strategy=strategy,
            direction=direction,
            entry_price=filled_entry,
            stop_price=stop,
            target_price=target,
            position_size=lot_size,
            risk_amount=risk_usd,
            entry_time=datetime.now(timezone.utc),
        )
        db_id = self.db.insert_trade(trade_rec)

        self.active_trade = GoldTrade(
            direction=direction,
            entry_price=filled_entry,
            stop_price=stop,
            target_price=target,
            lot_size=lot_size,
            risk_usd=risk_usd,
            strategy=strategy,
            db_id=db_id,
            trailing_trigger_r=self.risk_cfg.get("trailing_stop_trigger_r", 1.5),
        )
        self.tracker.record_trade_entry()

    # ── position management ───────────────────────────────────────────

    def _manage_position(self, now: datetime) -> None:
        trade = self.active_trade
        if trade is None:
            return

        bid, ask = self.broker.get_bid_ask(self.symbol)

        # ── trailing stop ──────────────────────────────────────────
        monitor_price = bid if trade.direction == "buy" else ask
        if trade.should_trail(monitor_price):
            new_stop = trade.new_trailing_stop(monitor_price)
            if new_stop is not None:
                # Only tighten, never widen
                if trade.direction == "buy" and new_stop > trade.stop_price:
                    trade.stop_price = new_stop
                    self.broker.update_stop_loss(self.symbol, new_stop)
                    if not trade.trailing_active:
                        trade.trailing_active = True
                        logger.info(
                            "[TRAIL] %s LONG — stop moved to %.5f (%.2fR)",
                            self.symbol, new_stop, trade.current_r(monitor_price),
                        )
                elif trade.direction == "sell" and new_stop < trade.stop_price:
                    trade.stop_price = new_stop
                    self.broker.update_stop_loss(self.symbol, new_stop)
                    if not trade.trailing_active:
                        trade.trailing_active = True
                        logger.info(
                            "[TRAIL] %s SHORT — stop moved to %.5f (%.2fR)",
                            self.symbol, new_stop, trade.current_r(monitor_price),
                        )

        # ── stop / target check ────────────────────────────────────
        if trade.is_stopped(bid, ask):
            self._record_close(trade.stop_price, "stop_hit")
        elif trade.is_target_hit(bid, ask):
            self._record_close(
                trade.target_price if trade.target_price else bid,
                "target_hit",
            )

    def _record_close(self, exit_price: float, reason: str) -> None:
        trade = self.active_trade
        if trade is None:
            return

        # Actually close in MT5 (stop/target may already be filled)
        self.broker.close_position(self.symbol)

        outcome, r, pnl_usd = trade.pnl(exit_price)
        logger.info(
            "[CLOSE] %s | %s | Exit: %.5f | R: %+.2f | PnL: $%+.2f | Reason: %s",
            self.symbol, outcome.upper(), exit_price, r, pnl_usd, reason,
        )

        self.db.update_trade_exit(
            trade_id=trade.db_id,
            exit_time=datetime.now(timezone.utc),
            exit_price=exit_price,
            outcome=outcome,
            r_multiple=r,
            pnl=pnl_usd,
        )
        self.tracker.record_trade_exit(outcome, r, pnl_usd)
        self.active_trade = None

    # ── helpers ───────────────────────────────────────────────────────

    _last_day: Optional[int] = None

    def _is_new_day(self, now: datetime) -> bool:
        today = now.date().toordinal()
        if self._last_day != today:
            self._last_day = today
            return True
        return False


# ══════════════════════════════════════════════════════════════════════
# Backtester (Gold / MT5 flavour)
# ══════════════════════════════════════════════════════════════════════

class GoldBacktester:
    """
    Event-driven backtester for XAUUSD.
    Reads a CSV exported from MT5 (or any OHLCV source).

    CSV format (MT5 export):
        <DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<TICKVOL>
    or standard:
        timestamp,open,high,low,close,volume
    """

    def __init__(self, config: dict):
        self.config = config
        self.bt_cfg = config["backtesting"]
        self.instr = config["instruments"][0]
        self.symbol = self.instr["symbol"]
        self.initial_balance = self.bt_cfg.get("initial_balance", 10000.0)
        self.commission = self.bt_cfg.get("commission_per_trade", 3.0)
        self.tick_size = self.instr.get("tick_size", 0.01)

        self.trend_filter = TrendFilter(config["trend_filter"])
        self.breakout = BreakoutStrategy(
            config["breakout"], trend_filter=self.trend_filter
        )
        self.scenario = ScenarioStrategy(config["scenario"])
        self.gap = MindTheGapStrategy(config["mind_the_gap"], tick_size=self.tick_size)
        self.db = TradeDatabase("gold_backtest.db")

    def load_csv(self, path: str) -> pd.DataFrame:
        """Load and normalise an MT5-exported or standard CSV."""
        df = pd.read_csv(path)
        cols = [c.strip().lower().lstrip("<").rstrip(">") for c in df.columns]
        df.columns = cols

        # Handle MT5 two-column date+time format
        if "date" in cols and "time" in cols:
            df["timestamp"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time"].astype(str)
            )
        elif "timestamp" in cols:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        else:
            raise ValueError("Cannot parse timestamp from CSV columns: " + str(cols))

        # Rename MT5 volume column
        if "tickvol" in cols and "volume" not in cols:
            df = df.rename(columns={"tickvol": "volume"})

        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df = df.sort_values("timestamp").reset_index(drop=True)
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)

        # Apply date filter
        start = pd.Timestamp(self.bt_cfg.get("start_date", "2020-01-01"))
        end = pd.Timestamp(self.bt_cfg.get("end_date", "2099-01-01"))
        df = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]

        logger.info(
            "Loaded %d bars for %s (%s → %s)",
            len(df), self.symbol,
            df["timestamp"].iloc[0].date() if len(df) else "N/A",
            df["timestamp"].iloc[-1].date() if len(df) else "N/A",
        )
        return df.reset_index(drop=True)

    def run(self, csv_path: str) -> dict:
        df = self.load_csv(csv_path)
        if df.empty:
            logger.error("No data after filtering.")
            return {}

        balance = self.initial_balance
        trades: list[dict] = []
        open_trade: Optional[dict] = None
        session_open: Optional[datetime] = None
        last_date = None
        gap_checked = False

        open_utc_str = self.instr.get("session_open_utc", "08:00")
        open_h, open_m = map(int, open_utc_str.split(":"))

        for i in range(1, len(df)):
            candle = df.iloc[i]
            prev_candle = df.iloc[i - 1]
            ts = pd.Timestamp(candle["timestamp"])
            df_so_far = df.iloc[: i + 1].copy()

            o = float(candle["open"])
            h = float(candle["high"])
            l = float(candle["low"])
            c = float(candle["close"])

            # ── new day reset ─────────────────────────────────────
            if ts.date() != last_date:
                last_date = ts.date()
                session_open = ts.replace(
                    hour=open_h, minute=open_m, second=0, microsecond=0,
                    tzinfo=timezone.utc,
                )
                self.breakout.reset_session(self.symbol)
                self.gap.reset_session(self.symbol)
                gap_checked = False

            # ── manage open trade ─────────────────────────────────
            if open_trade:
                direction = open_trade["direction"]
                stop = open_trade["stop"]
                target = open_trade.get("target")

                stopped = (direction == "buy" and l <= stop) or \
                          (direction == "sell" and h >= stop)
                hit_tp = target is not None and (
                    (direction == "buy" and h >= target) or
                    (direction == "sell" and l <= target)
                )

                if stopped or hit_tp:
                    exit_price = stop if stopped else target
                    entry = open_trade["entry"]
                    risk = abs(entry - stop)
                    if direction == "buy":
                        r = (exit_price - entry) / risk if risk else 0.0
                    else:
                        r = (entry - exit_price) / risk if risk else 0.0

                    lot = open_trade["lot"]
                    pnl = r * open_trade["risk_usd"] - self.commission
                    balance += pnl
                    outcome = "win" if r > 0 else ("loss" if r < 0 else "breakeven")
                    open_trade.update({
                        "exit_price": exit_price,
                        "exit_time": str(ts),
                        "outcome": outcome,
                        "r_multiple": round(r, 2),
                        "pnl": round(pnl, 2),
                        "balance_after": round(balance, 2),
                    })
                    trades.append(open_trade)
                    logger.debug(
                        "[BT] CLOSE %s %s @ %.2f | R: %+.2f | PnL: $%+.2f | Bal: $%.2f",
                        self.symbol, outcome, exit_price, r, pnl, balance,
                    )
                    open_trade = None
                continue

            # ── check daily loss limit ────────────────────────────
            day_trades = [t for t in trades if t.get("exit_time", "")[:10] == str(ts.date())]
            day_pnl = sum(t["pnl"] for t in day_trades)
            if day_pnl < -(self.initial_balance * self.config["risk"]["daily_loss_limit_pct"] / 100):
                continue

            # ── signal detection ──────────────────────────────────
            signal: Optional[dict] = None

            # Gap
            if not gap_checked and session_open and ts >= session_open:
                prev_close = float(prev_candle["close"])
                gap_sig = self.gap.on_session_open(
                    self.symbol, prev_close, o,
                    ts.to_pydatetime().replace(tzinfo=timezone.utc),
                )
                gap_checked = True
                if gap_sig:
                    signal = {
                        "direction": gap_sig.direction,
                        "entry": gap_sig.entry_price,
                        "stop": gap_sig.stop_price,
                        "target": gap_sig.take_profit,
                        "strategy": "mind_the_gap",
                    }

            # Breakout
            if signal is None and session_open:
                br_sig = self.breakout.on_candle(
                    self.symbol, df_so_far,
                    session_open.replace(tzinfo=timezone.utc),
                )
                if br_sig:
                    stop = find_swing_stop(
                        df_so_far, br_sig.direction,
                        lookback=10, buffer_ticks=3.0,
                        tick_size=self.tick_size,
                    ) or br_sig.stop_price
                    signal = {
                        "direction": br_sig.direction,
                        "entry": br_sig.entry_price,
                        "stop": stop,
                        "target": None,
                        "strategy": "breakout",
                    }

            # Scenario (check at end of day)
            if signal is None and ts.hour == 23 and ts.minute >= 50:
                df_d = df_so_far.copy()
                df_d["date"] = df_d["timestamp"].dt.date
                df_daily = df_d.groupby("date").agg(
                    timestamp=("timestamp", "first"),
                    open=("open", "first"),
                    high=("high", "max"),
                    low=("low", "min"),
                    close=("close", "last"),
                    volume=("volume", "sum"),
                ).reset_index(drop=True)
                sc_sig = self.scenario.on_daily_candle(self.symbol, df_daily)
                if sc_sig:
                    signal = {
                        "direction": sc_sig.direction,
                        "entry": sc_sig.entry_price,
                        "stop": sc_sig.stop_price,
                        "target": None,
                        "strategy": f"scenario_{sc_sig.scenario}",
                    }

            if signal:
                stop_dist = abs(signal["entry"] - signal["stop"])
                lot = lots_for_risk(
                    balance,
                    self.config["risk"]["max_risk_per_trade_pct"],
                    stop_dist,
                )
                lot = max(lot, self.instr.get("min_lot", 0.01))
                risk_usd = balance * self.config["risk"]["max_risk_per_trade_pct"] / 100.0

                open_trade = {
                    "symbol": self.symbol,
                    "direction": signal["direction"],
                    "entry": signal["entry"],
                    "stop": signal["stop"],
                    "target": signal.get("target"),
                    "lot": lot,
                    "risk_usd": risk_usd,
                    "strategy": signal["strategy"],
                    "entry_time": str(ts),
                }
                logger.debug(
                    "[BT] ENTER %s %s %.2f lots @ %.2f | SL: %.2f | %s",
                    self.symbol, signal["direction"].upper(),
                    lot, signal["entry"], signal["stop"], signal["strategy"],
                )

        self._print_summary(trades, balance)
        return {
            "symbol": self.symbol,
            "initial_balance": self.initial_balance,
            "final_balance": balance,
            "total_trades": len(trades),
            "trades": trades,
        }

    def _print_summary(self, trades: list[dict], final_balance: float) -> None:
        total = len(trades)
        wins = [t for t in trades if t.get("outcome") == "win"]
        losses = [t for t in trades if t.get("outcome") == "loss"]
        total_r = sum(t.get("r_multiple", 0) for t in trades)
        pnl = final_balance - self.initial_balance

        print("\n" + "=" * 62)
        print(f"  GOLD BACKTEST RESULTS — {self.symbol}")
        print("=" * 62)
        print(f"  Period        : {self.bt_cfg.get('start_date')} → {self.bt_cfg.get('end_date')}")
        print(f"  Total Trades  : {total}")
        print(f"  Wins / Losses : {len(wins)} / {len(losses)}")
        wr = len(wins) / total * 100 if total > 0 else 0.0
        print(f"  Win Rate      : {wr:.1f}%")
        print(f"  Total R       : {total_r:+.2f}R")
        print(f"  P&L           : ${pnl:+.2f}  ({pnl / self.initial_balance * 100:+.2f}%)")
        print(f"  End Balance   : ${final_balance:.2f}")
        print("=" * 62)

        # Strategy breakdown
        from collections import defaultdict
        by_strat: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "r": 0.0})
        for t in trades:
            s = t.get("strategy", "unknown")
            by_strat[s]["trades"] += 1
            if t.get("outcome") == "win":
                by_strat[s]["wins"] += 1
            by_strat[s]["r"] += t.get("r_multiple", 0)

        print("\n  By Strategy:")
        for strat, data in by_strat.items():
            wr_s = data["wins"] / data["trades"] * 100 if data["trades"] > 0 else 0
            print(f"    {strat:<22} {data['trades']:3d} trades | WR: {wr_s:.0f}% | R: {data['r']:+.2f}")
        print()


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tom Hougaard Gold Bot — MetaTrader 5",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default="config_gold.yaml", help="Config file path")
    parser.add_argument("--backtest", action="store_true", help="Run backtesting mode")
    parser.add_argument("--csv", default=None, help="CSV file for backtesting")
    parser.add_argument(
        "--live", action="store_true",
        help="Confirm live trading (overrides paper_trading in config)"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config["logging"], use_colors=True)

    # Safety: require explicit --live flag to disable paper mode
    if not args.live:
        config["broker"]["paper_trading"] = True

    if args.backtest or config["backtesting"].get("enabled", False):
        backtester = GoldBacktester(config)
        csv_path = args.csv
        if csv_path is None:
            data_dir = config["data"]["csv_data_path"]
            csv_path = os.path.join(data_dir, "XAUUSD_M5.csv")
        if not os.path.exists(csv_path):
            logger.error(
                "CSV not found: %s\n"
                "Export XAUUSD M5 data from MT5:\n"
                "  MT5 → Tools → History Center → XAUUSD → M5 → Export",
                csv_path,
            )
            sys.exit(1)
        backtester.run(csv_path)
    else:
        bot = GoldBot(config)
        bot.run()


if __name__ == "__main__":
    main()
