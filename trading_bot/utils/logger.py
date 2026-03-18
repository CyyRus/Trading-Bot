"""
Logging utilities with color-coded console output.
Green = LONG signal, Red = SHORT signal.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import Optional


# ANSI color codes
class Colors:
    RESET = "\033[0m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    BOLD = "\033[1m"
    DIM = "\033[2m"


class ColoredFormatter(logging.Formatter):
    """Custom formatter that adds colors based on log level and content."""

    LEVEL_COLORS = {
        logging.DEBUG: Colors.DIM,
        logging.INFO: Colors.WHITE,
        logging.WARNING: Colors.YELLOW,
        logging.ERROR: Colors.RED,
        logging.CRITICAL: Colors.RED + Colors.BOLD,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, Colors.WHITE)
        message = super().format(record)

        # Color trade signals
        if "LONG" in message or "BUY" in message:
            return Colors.GREEN + Colors.BOLD + message + Colors.RESET
        if "SHORT" in message or "SELL" in message:
            return Colors.RED + Colors.BOLD + message + Colors.RESET
        if "SIGNAL" in message:
            return Colors.CYAN + message + Colors.RESET

        return color + message + Colors.RESET


def setup_logging(config: dict, use_colors: bool = True) -> logging.Logger:
    """
    Configure root logger with:
    - Color-coded console handler
    - File handler for all logs
    - Separate file handler for errors only
    """
    log_dir = config.get("log_dir", "logs/")
    os.makedirs(log_dir, exist_ok=True)

    log_level_str = config.get("log_level", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    fmt = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    if use_colors and config.get("console_colors", True):
        console_handler.setFormatter(ColoredFormatter(fmt, datefmt=date_fmt))
    else:
        console_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))
    root_logger.addHandler(console_handler)

    # Trade log file handler
    trade_log_file = config.get("trade_log_file", os.path.join(log_dir, "trades.log"))
    file_handler = logging.FileHandler(trade_log_file)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))
    root_logger.addHandler(file_handler)

    # Error-only file handler
    error_log_file = config.get("error_log_file", os.path.join(log_dir, "error.log"))
    error_handler = logging.FileHandler(error_log_file)
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(fmt, datefmt=date_fmt))
    root_logger.addHandler(error_handler)

    return root_logger


def log_trade_signal(
    logger: logging.Logger,
    strategy: str,
    symbol: str,
    direction: str,
    entry: float,
    stop: float,
    target: Optional[float],
    size: float,
) -> None:
    """Log a trade signal with color-coded direction."""
    direction_label = "LONG" if direction == "buy" else "SHORT"
    r_distance = abs(entry - stop)
    tp_text = f"TP: {target:.4f}" if target else "TP: trailing"
    logger.info(
        "[SIGNAL] %s | %s %s | Entry: %.4f | SL: %.4f | %s | Size: %.2f | R-dist: %.4f",
        strategy, direction_label, symbol, entry, stop, tp_text, size, r_distance,
    )


def print_daily_summary(
    logger: logging.Logger,
    date: datetime,
    trades: list[dict],
    starting_balance: float,
    ending_balance: float,
) -> None:
    """Print end-of-session summary."""
    total_trades = len(trades)
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0.0
    total_r = sum(t.get("r_multiple", 0) for t in trades)
    pnl = ending_balance - starting_balance
    pnl_pct = (pnl / starting_balance * 100) if starting_balance > 0 else 0.0

    separator = "=" * 60
    logger.info(separator)
    logger.info("  DAILY SUMMARY — %s", date.strftime("%Y-%m-%d"))
    logger.info(separator)
    logger.info("  Trades taken  : %d", total_trades)
    logger.info("  Wins / Losses : %d / %d", len(wins), len(losses))
    logger.info("  Win Rate      : %.1f%%", win_rate)
    logger.info("  Total R       : %+.2fR", total_r)
    logger.info("  P&L           : %+.2f (%+.2f%%)", pnl, pnl_pct)
    logger.info("  End Balance   : $%.2f", ending_balance)
    logger.info(separator)
