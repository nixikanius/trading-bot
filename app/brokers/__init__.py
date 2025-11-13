from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.config import AccountConfig
from app.logger import get_logger

logger = get_logger(__name__)


class TradingError(Exception):
    """Base class for trading-related errors"""
    def __init__(self, message: str, code: str = "TRADING_ERROR"):
        super().__init__(message)
        self.code = code


@dataclass
class InstrumentInfo:
    instrument: str
    name: str
    type: str
    currency: str
    lot_size: float
    min_price_step: float
    initial_margin_long: float = None
    initial_margin_short: float = None

@dataclass
class Position:
    instrument: str
    quantity: int
    average_price: float

@dataclass
class OrderResult:
    date: datetime
    price: float

@dataclass
class EnsureOrder:
    type: str  # "buy", "sell", "stop_loss", "take_profit"
    quantity: int
    order_id: str
    action: Optional[str] = None  # "open_long", "close_short", etc.
    price: Optional[float] = None # for stop_loss and take_profit
    result: Optional[OrderResult] = None # for buy and sell

@dataclass
class StopOrder:
    order_id: str
    order_type: str  # "stop_loss" or "take_profit"
    direction: str  # "buy" or "sell"
    quantity: int
    price: Optional[float] = None  # for take profit
    stop_price: Optional[float] = None  # for stop loss
    exchange_order_type: str = "market"  # "market" or "limit"


class BrokerService(ABC):
    """Base class for broker service implementations"""
    
    @abstractmethod
    def get_instrument_info(self, instrument: str) -> Optional[InstrumentInfo]:
        """Get instrument details"""
        pass
    
    @abstractmethod
    def get_position(self, instrument_info: InstrumentInfo) -> Optional[Position]:
        """Get current position for instrument from portfolio"""
        pass
    
    @abstractmethod
    def get_position_waiting_for_state(self, instrument_info: InstrumentInfo, expected_quantity: int, max_attempts: int = 20, delay: float = 0.250) -> Optional[Position]:
        """Get current position for instrument from portfolio waiting for expected state"""
        pass
    
    @abstractmethod
    def calculate_position_size(self, instrument_info: InstrumentInfo, leverage_percent: float, reserve_capital: float, position_direction: str = "long") -> int:
        """Calculate position size based on available funds, leverage cap, and futures margin requirements"""
        pass
    
    @abstractmethod
    def place_market_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int) -> str:
        """Place market order. Returns order_id."""
        pass
    
    @abstractmethod
    def place_stop_loss_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, stop_price: float) -> str:
        """Place stop loss order. Returns order_id."""
        pass
    
    @abstractmethod
    def place_take_profit_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, take_price: float) -> str:
        """Place take profit order. Returns order_id."""
        pass
    
    @abstractmethod
    def cancel_stop_orders(self, orders: list[StopOrder]) -> None:
        """Cancel stop orders"""
        pass
    
    @abstractmethod
    def get_current_stop_orders(self, instrument_info: InstrumentInfo) -> list[StopOrder]:
        """Get current active stop orders for instrument"""
        pass
    
    @abstractmethod
    def pull_ensure_orders_result(self, ensure_orders: list[EnsureOrder], instrument_info: InstrumentInfo) -> list[EnsureOrder]:
        """Pull execution results for ensure orders"""
        pass
    
    def _should_update_stop_orders(self, stop_orders: list[StopOrder], stop_price: Optional[float], take_price: Optional[float]) -> bool:
        """Check if stop orders need to be updated based on stop price changes"""
        current_stop_price = None
        current_take_price = None
        
        for stop_order in stop_orders:
            if stop_order.order_type == 'stop_loss':
                if current_stop_price is not None:
                    logger.info(f"Stop orders need to be updated: more than one stop loss order found")
                    return True
                
                current_stop_price = stop_order.stop_price
            elif stop_order.order_type == 'take_profit':
                if current_take_price is not None:
                    logger.info(f"Stop orders need to be updated: more than one take profit order found")
                    return True
                
                current_take_price = stop_order.stop_price
            
        if stop_price != current_stop_price or take_price != current_take_price:
            logger.info(f"Stop orders need to be updated: current_stop_price={current_stop_price}, desired_stop_price={stop_price}, current_take_price={current_take_price}, desired_take_price={take_price}")
            return True
            
        return False
    
    def ensure_position(self, instrument_info: InstrumentInfo, init_position: Optional[Position], desired_position: str, leverage_percent: float, reserve_capital: float, stop_price: Optional[float] = None, take_price: Optional[float] = None) -> tuple[Optional[Position], list[EnsureOrder]]:
        """Ensure position matches desired state. Common logic for all brokers."""
        init_pos_qty = init_position.quantity if init_position else 0
        init_stop_orders = self.get_current_stop_orders(instrument_info)
        logger.info(f"Init position quantity: {init_pos_qty}, desired position: {desired_position}, init stop orders: {init_stop_orders}")
        
        expected_pos_qty = init_pos_qty
        orders = []
        
        if desired_position == "long":
            if init_pos_qty <= 0:
                if init_pos_qty < 0:
                    logger.info(f"Closing short position of {init_pos_qty} lots...")

                    self.cancel_stop_orders(init_stop_orders)
                    order_id = self.place_market_order(instrument_info, 'buy', -init_pos_qty)
                    orders.append(EnsureOrder(type="buy", quantity=-init_pos_qty, order_id=order_id, action="close_short"))
                
                available_qty = self.calculate_position_size(instrument_info, leverage_percent, reserve_capital, "long")
                expected_pos_qty = available_qty
                if available_qty > 0:
                    logger.info(f"Opening long position of {available_qty} lots...")

                    order_id = self.place_market_order(instrument_info, 'buy', available_qty)
                    orders.append(EnsureOrder(type="buy", quantity=available_qty, order_id=order_id, action="open_long"))
                else:
                    logger.info(f"No available funds to open long position")
            else:
                logger.info(f"Long position already exists")
                
        elif desired_position == "short":
            if init_pos_qty >= 0:
                if init_pos_qty > 0:
                    logger.info(f"Closing long position of {init_pos_qty} lots...")

                    self.cancel_stop_orders(init_stop_orders)
                    order_id = self.place_market_order(instrument_info, 'sell', init_pos_qty)
                    orders.append(EnsureOrder(type="sell", quantity=init_pos_qty, order_id=order_id, action="close_long"))
                
                available_qty = self.calculate_position_size(instrument_info, leverage_percent, reserve_capital, "short")
                expected_pos_qty = -available_qty
                if available_qty > 0:
                    logger.info(f"Opening short position of {expected_pos_qty} lots...")

                    order_id = self.place_market_order(instrument_info, 'sell', available_qty)
                    orders.append(EnsureOrder(type="sell", quantity=available_qty, order_id=order_id, action="open_short"))
                else:
                    logger.info(f"No available funds to open short position")
            else:
                logger.info(f"Short position already exists")
                
        elif desired_position == "flat":
            if init_pos_qty > 0:
                logger.info(f"Closing long position of {init_pos_qty} lots...")

                order_id = self.place_market_order(instrument_info, 'sell', init_pos_qty)
                orders.append(EnsureOrder(type="sell", quantity=init_pos_qty, order_id=order_id, action="close_long"))
                expected_pos_qty = 0
            elif init_pos_qty < 0:
                logger.info(f"Closing short position of {init_pos_qty} lots...")

                order_id = self.place_market_order(instrument_info, 'buy', -init_pos_qty)
                orders.append(EnsureOrder(type="buy", quantity=-init_pos_qty, order_id=order_id, action="close_short"))
                expected_pos_qty = 0
            else:
                logger.info(f"Position already flat")

        final_position = self.get_position_waiting_for_state(instrument_info, expected_pos_qty)
        final_pos_qty = final_position.quantity if final_position else 0
        final_stop_orders = self.get_current_stop_orders(instrument_info)

        logger.info(f"Final position quantity: {final_pos_qty}")

        if final_pos_qty != init_pos_qty or self._should_update_stop_orders(final_stop_orders, stop_price, take_price):
            logger.info(f"Updating stop orders...")

            self.cancel_stop_orders(final_stop_orders)
            
            # Long position
            if final_pos_qty > 0:
                if stop_price:
                    stop_order_id = self.place_stop_loss_order(instrument_info, "sell", final_pos_qty, stop_price)
                    orders.append(EnsureOrder(type="stop_loss", quantity=final_pos_qty, price=stop_price, order_id=stop_order_id))
                if take_price:
                    take_order_id = self.place_take_profit_order(instrument_info, "sell", final_pos_qty, take_price)
                    orders.append(EnsureOrder(type="take_profit", quantity=final_pos_qty, price=take_price, order_id=take_order_id))
            # Short position
            elif final_pos_qty < 0:
                if stop_price:
                    stop_order_id = self.place_stop_loss_order(instrument_info, "buy", abs(final_pos_qty), stop_price)
                    orders.append(EnsureOrder(type="stop_loss", quantity=abs(final_pos_qty), price=stop_price, order_id=stop_order_id))
                if take_price:
                    take_order_id = self.place_take_profit_order(instrument_info, "buy", abs(final_pos_qty), take_price)
                    orders.append(EnsureOrder(type="take_profit", quantity=abs(final_pos_qty), price=take_price, order_id=take_order_id))
            # Flat position
            else:
                logger.info("Stop orders are not needed for flat position")
        else:
            logger.info("Stop orders are not needed to be updated")
        
        return final_position, orders


# Avoid circular import
from app.brokers.finam import create_finam_service
from app.brokers.tinvest import create_tinvest_service

def create_broker_service(account_config: AccountConfig) -> BrokerService:
    """Create broker service based on account broker configuration"""
    name = account_config.broker.name
    config = account_config.broker.config
    
    if name == "finam":
        return create_finam_service(config)
    elif name == "tinvest":
        return create_tinvest_service(config)
    else:
        raise ValueError(f"Unsupported broker: {name}")
