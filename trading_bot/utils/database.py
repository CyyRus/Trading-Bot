"""
SQLite trade log database.
Stores every trade with full details: entry, stop, target, outcome, R-multiple.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Generator, Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    symbol: str
    strategy: str
    direction: str          # "buy" or "sell"
    entry_price: float
    stop_price: float
    target_price: Optional[float]
    position_size: float
    risk_amount: float      # Dollar risk on the trade
    entry_time: datetime
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None   # "win", "loss", "breakeven"
    r_multiple: Optional[float] = None
    pnl: Optional[float] = None
    notes: str = ""
    id: Optional[int] = None

    def risk_reward_ratio(self) -> Optional[float]:
        if self.target_price is None:
            return None
        risk = abs(self.entry_price - self.stop_price)
        reward = abs(self.target_price - self.entry_price)
        return reward / risk if risk > 0 else None

    def compute_r_multiple(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        risk = abs(self.entry_price - self.stop_price)
        if risk == 0:
            return 0.0
        if self.direction == "buy":
            profit = self.exit_price - self.entry_price
        else:
            profit = self.entry_price - self.exit_price
        return profit / risk


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    direction       TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    stop_price      REAL NOT NULL,
    target_price    REAL,
    position_size   REAL NOT NULL,
    risk_amount     REAL NOT NULL,
    entry_time      TEXT NOT NULL,
    exit_time       TEXT,
    exit_price      REAL,
    outcome         TEXT,
    r_multiple      REAL,
    pnl             REAL,
    notes           TEXT DEFAULT ''
);
"""

CREATE_DAILY_SUMMARY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS daily_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    total_trades    INTEGER NOT NULL,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    win_rate        REAL NOT NULL,
    total_r         REAL NOT NULL,
    pnl             REAL NOT NULL,
    ending_balance  REAL NOT NULL
);
"""


class TradeDatabase:
    """SQLite-backed trade journal."""

    def __init__(self, db_path: str = "trading_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(CREATE_TABLE_SQL)
            conn.execute(CREATE_DAILY_SUMMARY_TABLE_SQL)
            conn.commit()
        logger.debug("Database initialised at %s", self.db_path)

    @contextmanager
    def _get_conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def insert_trade(self, trade: TradeRecord) -> int:
        """Insert a new trade. Returns the auto-assigned row id."""
        sql = """
        INSERT INTO trades (
            symbol, strategy, direction, entry_price, stop_price,
            target_price, position_size, risk_amount, entry_time,
            exit_time, exit_price, outcome, r_multiple, pnl, notes
        ) VALUES (
            :symbol, :strategy, :direction, :entry_price, :stop_price,
            :target_price, :position_size, :risk_amount, :entry_time,
            :exit_time, :exit_price, :outcome, :r_multiple, :pnl, :notes
        )
        """
        data = asdict(trade)
        data["entry_time"] = _dt_str(trade.entry_time)
        data["exit_time"] = _dt_str(trade.exit_time)
        data.pop("id", None)

        with self._get_conn() as conn:
            cur = conn.execute(sql, data)
            conn.commit()
            return cur.lastrowid

    def update_trade_exit(
        self,
        trade_id: int,
        exit_time: datetime,
        exit_price: float,
        outcome: str,
        r_multiple: float,
        pnl: float,
    ) -> None:
        """Update an existing trade with exit details."""
        sql = """
        UPDATE trades
        SET exit_time = :exit_time,
            exit_price = :exit_price,
            outcome = :outcome,
            r_multiple = :r_multiple,
            pnl = :pnl
        WHERE id = :id
        """
        with self._get_conn() as conn:
            conn.execute(
                sql,
                {
                    "exit_time": _dt_str(exit_time),
                    "exit_price": exit_price,
                    "outcome": outcome,
                    "r_multiple": r_multiple,
                    "pnl": pnl,
                    "id": trade_id,
                },
            )
            conn.commit()

    def save_daily_summary(
        self,
        date: datetime,
        total_trades: int,
        wins: int,
        losses: int,
        win_rate: float,
        total_r: float,
        pnl: float,
        ending_balance: float,
    ) -> None:
        sql = """
        INSERT INTO daily_summary (
            date, total_trades, wins, losses, win_rate, total_r, pnl, ending_balance
        ) VALUES (
            :date, :total_trades, :wins, :losses, :win_rate, :total_r, :pnl, :ending_balance
        )
        """
        with self._get_conn() as conn:
            conn.execute(
                sql,
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "total_trades": total_trades,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": win_rate,
                    "total_r": total_r,
                    "pnl": pnl,
                    "ending_balance": ending_balance,
                },
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_all_trades(self) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM trades ORDER BY entry_time DESC").fetchall()
        return [dict(r) for r in rows]

    def get_trades_by_date(self, date_str: str) -> list[dict]:
        """date_str format: 'YYYY-MM-DD'"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE entry_time LIKE ? ORDER BY entry_time",
                (f"{date_str}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trades(self) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_time IS NULL ORDER BY entry_time"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return overall performance statistics."""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                    AVG(r_multiple) as avg_r,
                    SUM(r_multiple) as total_r,
                    SUM(pnl) as total_pnl
                FROM trades
                WHERE exit_time IS NOT NULL
            """).fetchone()
        return dict(row) if row else {}


def _dt_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")
