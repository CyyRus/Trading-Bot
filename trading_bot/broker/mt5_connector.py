"""
MetaTrader 5 Broker Connector
Implements BrokerBase for the MT5 Python API.

Requirements:
    pip install MetaTrader5 pandas numpy

Notes:
    - MT5 terminal must be running on the same Windows machine
      (or via Wine on Linux with the mt5 bridge).
    - Magic number (MAGIC) tags all bot orders so they can be
      distinguished from manual trades.
    - Gold symbol is typically "XAUUSD" — confirm in your broker's
      Market Watch and set it in config_gold.yaml.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .connector import BrokerBase, OrderResult, Position

logger = logging.getLogger(__name__)

# Unique magic number to identify bot orders in MT5
MAGIC = 20240101


def _import_mt5():
    """Lazy import so the module loads even without MT5 installed."""
    try:
        import MetaTrader5 as mt5  # type: ignore
        return mt5
    except ImportError:
        raise ImportError(
            "MetaTrader5 not installed. Run: pip install MetaTrader5\n"
            "MT5 terminal must also be running on the same machine."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe mapping
# ─────────────────────────────────────────────────────────────────────────────

_TF_MAP = {
    "1min":  1,   # TIMEFRAME_M1
    "2min":  2,
    "3min":  3,
    "4min":  4,
    "5min":  5,   # TIMEFRAME_M5
    "6min":  6,
    "10min": 10,
    "12min": 12,
    "15min": 15,  # TIMEFRAME_M15
    "20min": 20,
    "30min": 30,  # TIMEFRAME_M30
    "1h":    16385,  # TIMEFRAME_H1
    "2h":    16386,
    "3h":    16387,
    "4h":    16388,  # TIMEFRAME_H4
    "6h":    16390,
    "8h":    16392,
    "12h":   16396,
    "1d":    16408,  # TIMEFRAME_D1
    "1w":    32769,  # TIMEFRAME_W1
    "1mo":   49153,  # TIMEFRAME_MN1
}


def _mt5_timeframe(timeframe: str):
    mt5 = _import_mt5()
    # Try the lookup table first
    tf_id = _TF_MAP.get(timeframe.lower())
    if tf_id is not None:
        return tf_id
    # Fall back to mt5 constant by name
    attr = f"TIMEFRAME_{timeframe.upper()}"
    if hasattr(mt5, attr):
        return getattr(mt5, attr)
    raise ValueError(f"Unknown MT5 timeframe: {timeframe!r}")


# ─────────────────────────────────────────────────────────────────────────────
# MT5 Connector
# ─────────────────────────────────────────────────────────────────────────────

class MT5Connector(BrokerBase):
    """
    Full MetaTrader 5 connector.

    Config keys used:
        mt5_path      : optional path to terminal64.exe
        mt5_login     : account number (int)
        mt5_password  : account password (str)
        mt5_server    : broker server name (str)
        paper_trading : if True, confirms orders are on a demo account
        lot_size      : default lot size multiplier (overridden by position sizer)
        slippage      : max slippage in points (default 10)
    """

    def __init__(self, config: dict):
        self.config = config
        self.mt5_login: int = int(config.get("mt5_login", 0))
        self.mt5_password: str = config.get("mt5_password", "")
        self.mt5_server: str = config.get("mt5_server", "")
        self.mt5_path: Optional[str] = config.get("mt5_path")
        self.slippage: int = int(config.get("slippage", 10))
        self._mt5 = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    def connect(self) -> bool:
        mt5 = _import_mt5()
        self._mt5 = mt5

        init_kwargs: dict = {}
        if self.mt5_path:
            init_kwargs["path"] = self.mt5_path
        if self.mt5_login:
            init_kwargs["login"] = self.mt5_login
            init_kwargs["password"] = self.mt5_password
            init_kwargs["server"] = self.mt5_server

        if not mt5.initialize(**init_kwargs):
            err = mt5.last_error()
            logger.error("MT5 initialize() failed: %s", err)
            return False

        info = mt5.account_info()
        if info is None:
            logger.error("MT5 account_info() returned None: %s", mt5.last_error())
            return False

        logger.info(
            "MT5 connected | Account: %d | Server: %s | Balance: %.2f %s | %s",
            info.login,
            info.server,
            info.balance,
            info.currency,
            "DEMO" if not info.trade_allowed or self.config.get("paper_trading") else "LIVE",
        )
        return True

    def disconnect(self) -> None:
        if self._mt5:
            self._mt5.shutdown()
            logger.info("MT5 disconnected.")

    # ── account ───────────────────────────────────────────────────────────

    def get_account_balance(self) -> float:
        info = self._mt5.account_info()
        if info is None:
            return 0.0
        return float(info.equity)

    def get_account_info(self) -> dict:
        info = self._mt5.account_info()
        if info is None:
            return {}
        return {
            "login": info.login,
            "server": info.server,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "currency": info.currency,
            "leverage": info.leverage,
        }

    # ── market data ───────────────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> float:
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("Could not get tick for %s: %s", symbol, self._mt5.last_error())
            return 0.0
        # Return mid-price
        return (tick.bid + tick.ask) / 2.0

    def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """Return (bid, ask) for a symbol."""
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            return 0.0, 0.0
        return float(tick.bid), float(tick.ask)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        mt5 = self._mt5
        tf = _mt5_timeframe(timeframe)

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, limit)
        if rates is None or len(rates) == 0:
            logger.error(
                "No data for %s/%s: %s", symbol, timeframe, mt5.last_error()
            )
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "tick_volume": "volume",
        })
        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def get_symbol_info(self, symbol: str) -> dict:
        """Return key symbol properties: tick_size, point, contract_size, etc."""
        info = self._mt5.symbol_info(symbol)
        if info is None:
            return {}
        return {
            "symbol": symbol,
            "point": info.point,
            "tick_size": info.trade_tick_size,
            "tick_value": info.trade_tick_value,
            "contract_size": info.trade_contract_size,
            "min_lot": info.volume_min,
            "max_lot": info.volume_max,
            "lot_step": info.volume_step,
            "digits": info.digits,
            "spread": info.spread,
        }

    # ── order execution ───────────────────────────────────────────────────

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[OrderResult]:
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error("Symbol %s not found in MT5.", symbol)
            return None

        # Ensure symbol is visible in Market Watch
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                logger.error("Could not select symbol %s.", symbol)
                return None

        # Clamp lot size
        lot = self._normalise_lot(qty, info)
        bid, ask = self.get_bid_ask(symbol)

        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = ask if side == "buy" else bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": price,
            "deviation": self.slippage,
            "magic": MAGIC,
            "comment": "TomHBot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(symbol),
        }

        if stop_loss is not None:
            request["sl"] = round(stop_loss, info.digits)
        if take_profit is not None:
            request["tp"] = round(take_profit, info.digits)

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            retcode = result.retcode if result else "N/A"
            comment = result.comment if result else mt5.last_error()
            logger.error(
                "Order failed for %s %s: retcode=%s | %s",
                side.upper(), symbol, retcode, comment,
            )
            return None

        logger.info(
            "MT5 ORDER FILLED | %s %s %.2f lots @ %.5f | Ticket: %d",
            side.upper(), symbol, lot, result.price, result.order,
        )

        return OrderResult(
            order_id=str(result.order),
            symbol=symbol,
            side=side,
            qty=lot,
            filled_price=float(result.price),
            status="filled",
            timestamp=datetime.now(timezone.utc),
        )

    def close_position(self, symbol: str) -> Optional[OrderResult]:
        """Close the open position for a symbol (identified by magic number)."""
        mt5 = self._mt5
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            logger.warning("No open position for %s.", symbol)
            return None

        results = []
        for pos in positions:
            if pos.magic != MAGIC:
                continue  # not our trade

            # Opposite side to close
            if pos.type == mt5.POSITION_TYPE_BUY:
                close_type = mt5.ORDER_TYPE_SELL
                close_side = "sell"
                bid, _ = self.get_bid_ask(symbol)
                price = bid
            else:
                close_type = mt5.ORDER_TYPE_BUY
                close_side = "buy"
                _, ask = self.get_bid_ask(symbol)
                price = ask

            info = mt5.symbol_info(symbol)
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": price,
                "deviation": self.slippage,
                "magic": MAGIC,
                "comment": "TomHBot-close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling_mode(symbol),
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    "MT5 CLOSE | %s ticket %d @ %.5f", symbol, pos.ticket, result.price
                )
                results.append(
                    OrderResult(
                        order_id=str(result.order),
                        symbol=symbol,
                        side=close_side,
                        qty=pos.volume,
                        filled_price=float(result.price),
                        status="filled",
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            else:
                logger.error(
                    "Failed to close %s ticket %d: %s",
                    symbol, pos.ticket, result.comment if result else mt5.last_error(),
                )

        return results[0] if results else None

    def get_open_positions(self) -> list[Position]:
        mt5 = self._mt5
        positions = mt5.positions_get()
        if positions is None:
            return []

        result = []
        for pos in positions:
            if pos.magic != MAGIC:
                continue
            result.append(
                Position(
                    symbol=pos.symbol,
                    side="long" if pos.type == mt5.POSITION_TYPE_BUY else "short",
                    qty=pos.volume,
                    avg_entry_price=pos.price_open,
                    unrealized_pnl=pos.profit,
                    current_price=pos.price_current,
                )
            )
        return result

    def update_stop_loss(self, symbol: str, new_stop: float) -> bool:
        """Modify stop loss on all open positions for symbol (magic-tagged)."""
        mt5 = self._mt5
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return False

        success = True
        for pos in positions:
            if pos.magic != MAGIC:
                continue
            info = mt5.symbol_info(symbol)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": symbol,
                "position": pos.ticket,
                "sl": round(new_stop, info.digits if info else 5),
                "tp": pos.tp,  # preserve existing TP
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error(
                    "Failed to update SL on %s ticket %d: %s",
                    symbol, pos.ticket, result.comment if result else mt5.last_error(),
                )
                success = False
            else:
                logger.info(
                    "MT5 SL updated | %s ticket %d → SL %.5f",
                    symbol, pos.ticket, new_stop,
                )
        return success

    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all pending (non-position) orders for symbol."""
        mt5 = self._mt5
        orders = mt5.orders_get(symbol=symbol)
        if not orders:
            return True

        success = True
        for order in orders:
            if order.magic != MAGIC:
                continue
            request = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": order.ticket,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                logger.error("Failed to cancel order %d.", order.ticket)
                success = False
        return success

    # ── helpers ───────────────────────────────────────────────────────────

    def _normalise_lot(self, qty: float, info) -> float:
        """Clamp and round lot size to broker's constraints."""
        lot = max(qty, info.volume_min)
        lot = min(lot, info.volume_max)
        step = info.volume_step
        lot = round(round(lot / step) * step, 2)
        return lot

    def _get_filling_mode(self, symbol: str) -> int:
        """Return the broker-supported filling mode for a symbol."""
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC

        filling = info.filling_mode
        if filling & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if filling & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def place_pending_stop_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        trigger_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[OrderResult]:
        """
        Place a BUY_STOP or SELL_STOP pending order.
        Useful for entering on breakout at a specific price level.
        """
        mt5 = self._mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            return None

        order_type = mt5.ORDER_TYPE_BUY_STOP if side == "buy" else mt5.ORDER_TYPE_SELL_STOP
        lot = self._normalise_lot(qty, info)

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot,
            "type": order_type,
            "price": round(trigger_price, info.digits),
            "deviation": self.slippage,
            "magic": MAGIC,
            "comment": "TomHBot-stop",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(symbol),
        }

        if stop_loss is not None:
            request["sl"] = round(stop_loss, info.digits)
        if take_profit is not None:
            request["tp"] = round(take_profit, info.digits)

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "Pending order failed for %s: %s",
                symbol, result.comment if result else mt5.last_error(),
            )
            return None

        logger.info(
            "MT5 PENDING %s | %s %.2f lots @ %.5f | Ticket: %d",
            "BUY_STOP" if side == "buy" else "SELL_STOP",
            symbol, lot, trigger_price, result.order,
        )
        return OrderResult(
            order_id=str(result.order),
            symbol=symbol,
            side=side,
            qty=lot,
            filled_price=trigger_price,
            status="pending",
            timestamp=datetime.now(timezone.utc),
        )
