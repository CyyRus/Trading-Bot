"""
Modular broker connector.
Supports Alpaca (alpaca-trade-api) and ccxt-based exchanges.
Swap brokers by changing broker.name in config.yaml.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str           # "buy" or "sell"
    qty: float
    filled_price: float
    status: str         # "filled", "pending", "cancelled"
    timestamp: datetime


@dataclass
class Position:
    symbol: str
    side: str           # "long" or "short"
    qty: float
    avg_entry_price: float
    unrealized_pnl: float
    current_price: float


class BrokerBase(ABC):
    """Abstract base class for all broker connectors."""

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to broker. Returns True on success."""

    @abstractmethod
    def get_account_balance(self) -> float:
        """Return current account equity/balance."""

    @abstractmethod
    def get_current_price(self, symbol: str) -> float:
        """Return latest price for a symbol."""

    @abstractmethod
    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """
        Return OHLCV DataFrame with columns:
        [timestamp, open, high, low, close, volume]
        """

    @abstractmethod
    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """Place a market order. side = 'buy' | 'sell'."""

    @abstractmethod
    def close_position(self, symbol: str) -> Optional[OrderResult]:
        """Close the open position for a symbol."""

    @abstractmethod
    def get_open_positions(self) -> list[Position]:
        """Return list of currently open positions."""

    @abstractmethod
    def update_stop_loss(self, symbol: str, new_stop: float) -> bool:
        """Update the stop loss for an open position."""

    @abstractmethod
    def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all pending orders for a symbol."""


# ---------------------------------------------------------------------------
# Alpaca connector
# ---------------------------------------------------------------------------

class AlpacaConnector(BrokerBase):
    """Connector using alpaca-trade-api."""

    def __init__(self, config: dict):
        self.config = config
        self._api = None
        self._paper = config.get("paper_trading", True)

    def connect(self) -> bool:
        try:
            import alpaca_trade_api as tradeapi  # type: ignore

            self._api = tradeapi.REST(
                key_id=self.config["api_key"],
                secret_key=self.config["api_secret"],
                base_url=self.config["base_url"],
                api_version="v2",
            )
            account = self._api.get_account()
            logger.info(
                "Alpaca connected. Account status: %s | Equity: $%.2f",
                account.status,
                float(account.equity),
            )
            return True
        except ImportError:
            logger.error(
                "alpaca-trade-api not installed. Run: pip install alpaca-trade-api"
            )
            return False
        except Exception as exc:
            logger.error("Alpaca connection failed: %s", exc)
            return False

    def get_account_balance(self) -> float:
        account = self._api.get_account()
        return float(account.equity)

    def get_current_price(self, symbol: str) -> float:
        trade = self._api.get_latest_trade(symbol)
        return float(trade.price)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        # Map common timeframe strings to Alpaca format
        tf_map = {
            "1min": "1Min", "5min": "5Min", "15min": "15Min",
            "30min": "30Min", "1h": "1Hour", "1d": "1Day",
        }
        alpaca_tf = tf_map.get(timeframe, timeframe)
        bars = self._api.get_bars(symbol, alpaca_tf, limit=limit).df
        bars = bars.reset_index()
        bars.columns = [c.lower() for c in bars.columns]
        bars = bars.rename(columns={"t": "timestamp"})
        return bars[["timestamp", "open", "high", "low", "close", "volume"]]

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        order_kwargs: dict = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": "market",
            "time_in_force": "gtc",
        }

        # Attach bracket order if stop/tp provided
        if stop_loss is not None and take_profit is not None:
            order_kwargs["order_class"] = "bracket"
            order_kwargs["stop_loss"] = {"stop_price": str(stop_loss)}
            order_kwargs["take_profit"] = {"limit_price": str(take_profit)}
        elif stop_loss is not None:
            order_kwargs["order_class"] = "oto"
            order_kwargs["stop_loss"] = {"stop_price": str(stop_loss)}

        order = self._api.submit_order(**order_kwargs)
        return OrderResult(
            order_id=order.id,
            symbol=symbol,
            side=side,
            qty=float(order.qty),
            filled_price=float(order.filled_avg_price or 0),
            status=order.status,
            timestamp=datetime.utcnow(),
        )

    def close_position(self, symbol: str) -> Optional[OrderResult]:
        try:
            order = self._api.close_position(symbol)
            return OrderResult(
                order_id=order.id,
                symbol=symbol,
                side=order.side,
                qty=float(order.qty),
                filled_price=float(order.filled_avg_price or 0),
                status=order.status,
                timestamp=datetime.utcnow(),
            )
        except Exception as exc:
            logger.warning("Could not close position for %s: %s", symbol, exc)
            return None

    def get_open_positions(self) -> list[Position]:
        positions = []
        for p in self._api.list_positions():
            positions.append(
                Position(
                    symbol=p.symbol,
                    side="long" if float(p.qty) > 0 else "short",
                    qty=abs(float(p.qty)),
                    avg_entry_price=float(p.avg_entry_price),
                    unrealized_pnl=float(p.unrealized_pl),
                    current_price=float(p.current_price),
                )
            )
        return positions

    def update_stop_loss(self, symbol: str, new_stop: float) -> bool:
        try:
            position = self._api.get_position(symbol)
            self._api.cancel_all_orders()  # cancel existing stop orders
            side = "sell" if float(position.qty) > 0 else "buy"
            self._api.submit_order(
                symbol=symbol,
                qty=abs(float(position.qty)),
                side=side,
                type="stop",
                time_in_force="gtc",
                stop_price=str(new_stop),
            )
            return True
        except Exception as exc:
            logger.error("Failed to update stop loss for %s: %s", symbol, exc)
            return False

    def cancel_all_orders(self, symbol: str) -> bool:
        try:
            self._api.cancel_all_orders()
            return True
        except Exception as exc:
            logger.error("Failed to cancel orders for %s: %s", symbol, exc)
            return False


# ---------------------------------------------------------------------------
# ccxt connector
# ---------------------------------------------------------------------------

class CcxtConnector(BrokerBase):
    """Connector using ccxt for crypto/CFD exchanges."""

    def __init__(self, config: dict):
        self.config = config
        self._exchange = None

    def connect(self) -> bool:
        try:
            import ccxt  # type: ignore

            exchange_class = getattr(ccxt, self.config.get("ccxt_exchange", "binance"))
            self._exchange = exchange_class(
                {
                    "apiKey": self.config["api_key"],
                    "secret": self.config["api_secret"],
                    "enableRateLimit": True,
                }
            )
            if self.config.get("paper_trading", True):
                self._exchange.set_sandbox_mode(True)
            balance = self._exchange.fetch_balance()
            logger.info(
                "ccxt connected to %s. Total USDT: %.2f",
                self.config.get("ccxt_exchange"),
                balance.get("total", {}).get("USDT", 0),
            )
            return True
        except ImportError:
            logger.error("ccxt not installed. Run: pip install ccxt")
            return False
        except Exception as exc:
            logger.error("ccxt connection failed: %s", exc)
            return False

    def get_account_balance(self) -> float:
        balance = self._exchange.fetch_balance()
        return float(balance.get("total", {}).get("USDT", 0))

    def get_current_price(self, symbol: str) -> float:
        ticker = self._exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        ohlcv = self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        order = self._exchange.create_order(symbol, "market", side, qty)
        filled_price = float(order.get("average") or order.get("price") or 0)

        if stop_loss is not None:
            sl_side = "sell" if side == "buy" else "buy"
            self._exchange.create_order(symbol, "stop_market", sl_side, qty, None, {"stopPrice": stop_loss})

        return OrderResult(
            order_id=str(order["id"]),
            symbol=symbol,
            side=side,
            qty=qty,
            filled_price=filled_price,
            status=order.get("status", "filled"),
            timestamp=datetime.utcnow(),
        )

    def close_position(self, symbol: str) -> Optional[OrderResult]:
        positions = self._exchange.fetch_positions([symbol])
        for pos in positions:
            if float(pos.get("contracts", 0)) != 0:
                side = "sell" if pos["side"] == "long" else "buy"
                qty = abs(float(pos["contracts"]))
                return self.place_market_order(symbol, side, qty)
        return None

    def get_open_positions(self) -> list[Position]:
        positions = []
        for p in self._exchange.fetch_positions():
            if float(p.get("contracts", 0)) != 0:
                positions.append(
                    Position(
                        symbol=p["symbol"],
                        side=p["side"],
                        qty=abs(float(p["contracts"])),
                        avg_entry_price=float(p.get("entryPrice") or 0),
                        unrealized_pnl=float(p.get("unrealizedPnl") or 0),
                        current_price=float(p.get("markPrice") or 0),
                    )
                )
        return positions

    def update_stop_loss(self, symbol: str, new_stop: float) -> bool:
        logger.warning("update_stop_loss not fully implemented for ccxt; use exchange UI.")
        return False

    def cancel_all_orders(self, symbol: str) -> bool:
        try:
            self._exchange.cancel_all_orders(symbol)
            return True
        except Exception as exc:
            logger.error("Failed to cancel orders: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Paper trading simulator (no external dependencies)
# ---------------------------------------------------------------------------

class PaperBrokerConnector(BrokerBase):
    """
    Fully local paper trading simulator.
    No external API required. Useful for backtesting and demos.
    """

    def __init__(self, config: dict, initial_balance: float = 10000.0):
        self._balance = initial_balance
        self._positions: dict[str, Position] = {}
        self._order_counter = 0
        self._price_feed: dict[str, float] = {}  # injected externally
        self._ohlcv_feed: dict[str, pd.DataFrame] = {}

    def connect(self) -> bool:
        logger.info("Paper broker connected (simulation mode).")
        return True

    def inject_price(self, symbol: str, price: float) -> None:
        """Inject a price for the paper broker to use."""
        self._price_feed[symbol] = price

    def inject_ohlcv(self, symbol: str, df: pd.DataFrame) -> None:
        """Inject OHLCV data for the paper broker to use."""
        self._ohlcv_feed[symbol] = df

    def get_account_balance(self) -> float:
        return self._balance

    def get_current_price(self, symbol: str) -> float:
        return self._price_feed.get(symbol, 0.0)

    def get_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        df = self._ohlcv_feed.get(symbol, pd.DataFrame())
        return df.tail(limit).reset_index(drop=True)

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        price = self._price_feed.get(symbol, 0.0)
        self._order_counter += 1
        position_side = "long" if side == "buy" else "short"

        self._positions[symbol] = Position(
            symbol=symbol,
            side=position_side,
            qty=qty,
            avg_entry_price=price,
            unrealized_pnl=0.0,
            current_price=price,
        )
        logger.info(
            "[PAPER] %s %s %.2f @ %.4f | SL: %s | TP: %s",
            side.upper(), symbol, qty, price, stop_loss, take_profit,
        )
        return OrderResult(
            order_id=f"PAPER-{self._order_counter}",
            symbol=symbol,
            side=side,
            qty=qty,
            filled_price=price,
            status="filled",
            timestamp=datetime.utcnow(),
        )

    def close_position(self, symbol: str) -> Optional[OrderResult]:
        pos = self._positions.pop(symbol, None)
        if pos is None:
            return None
        price = self._price_feed.get(symbol, pos.avg_entry_price)
        if pos.side == "long":
            pnl = (price - pos.avg_entry_price) * pos.qty
        else:
            pnl = (pos.avg_entry_price - price) * pos.qty
        self._balance += pnl
        self._order_counter += 1
        logger.info("[PAPER] CLOSE %s @ %.4f | PnL: %.2f", symbol, price, pnl)
        return OrderResult(
            order_id=f"PAPER-{self._order_counter}",
            symbol=symbol,
            side="sell" if pos.side == "long" else "buy",
            qty=pos.qty,
            filled_price=price,
            status="filled",
            timestamp=datetime.utcnow(),
        )

    def get_open_positions(self) -> list[Position]:
        return list(self._positions.values())

    def update_stop_loss(self, symbol: str, new_stop: float) -> bool:
        logger.info("[PAPER] Updated stop loss for %s to %.4f", symbol, new_stop)
        return True

    def cancel_all_orders(self, symbol: str) -> bool:
        logger.info("[PAPER] Cancelled all orders for %s", symbol)
        return True


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_broker(config: dict, initial_balance: float = 10000.0) -> BrokerBase:
    """Return the appropriate broker connector based on config."""
    broker_name = config.get("name", "paper").lower()
    paper = config.get("paper_trading", True)

    if paper or broker_name == "paper":
        logger.info("Using PaperBrokerConnector (simulation mode).")
        return PaperBrokerConnector(config, initial_balance=initial_balance)
    elif broker_name == "alpaca":
        return AlpacaConnector(config)
    elif broker_name == "ccxt":
        return CcxtConnector(config)
    else:
        logger.warning("Unknown broker '%s'. Falling back to paper trading.", broker_name)
        return PaperBrokerConnector(config, initial_balance=initial_balance)
