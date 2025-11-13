from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.config import AccountConfig
from app.logger import get_logger
from app.schemas import Signal
from app.brokers import Position, EnsureOrder, InstrumentInfo, BrokerService
from app.brokers import create_broker_service
from app.telegram_service import TelegramService

logger = get_logger(__name__)


class SignalService:
    def __init__(self, account_name: str, account_config: AccountConfig, telegram_service: TelegramService) -> None:
        self.account_name = account_name
        self.account_config = account_config
        self.broker: BrokerService = create_broker_service(account_config)
        self.telegram = telegram_service

    def process_signal(self, signal: Signal) -> dict:
        """Process trading signal and return result"""
        logger.info(f"Processing signal for {self.account_name}: {signal.model_dump()}")

        instrument_info = self.broker.get_instrument_info(signal.instrument)
        logger.info(f"Instrument info: {instrument_info}")
        
        init_position = self.broker.get_position(instrument_info)
        logger.info(f"Initial position: {init_position}")
        
        # Handle signal based on position
        if signal.position == "flat":
            position, ensure_orders = self._handle_flat_signal(signal, instrument_info, init_position)
        else:
            position, ensure_orders = self._handle_position_signal(signal, instrument_info, init_position)
        
        ensure_orders = self.broker.pull_ensure_orders_result(ensure_orders, instrument_info)
        slippage = self._calculate_slippage(signal, ensure_orders)
        profit = self._calculate_profit(instrument_info, init_position, ensure_orders)
        stop_orders = self.broker.get_current_stop_orders(instrument_info)

        logger.info(f"Ensure orders: {ensure_orders}")
        logger.info(f"Slippage: {slippage}")
        logger.info(f"Profit: {profit}")
        logger.info(f"Resulting position: {position}")
        logger.info(f"Resulting stop orders: {stop_orders}")
        
        # # Check if position signal was processed correctly
        # if signal.position != "flat" and not position:
        #     raise TradingError(
        #         message=f"Failed to open {signal.position} position: no available funds or insufficient margin",
        #         code="INSUFFICIENT_FUNDS"
        #     )

        result = {
            "init_position": init_position,
            "ensure_orders": ensure_orders,
            "profit": profit,
            "slippage": slippage,
            "position": position,
            "stop_orders": stop_orders,
        }
        
        # Send Telegram notification when orders are placed
        if ensure_orders:
            telegram_message = self.telegram.format_signal_result(
                self.account_name, 
                signal.model_dump(), 
                result
            )
            self.telegram.send_message(telegram_message)
        
        return result

    def _handle_flat_signal(self, signal: Signal, instrument_info: InstrumentInfo, init_position: Position) -> tuple[Optional[Position], list[EnsureOrder]]:
        """Handle flat signal (close all positions)"""
        logger.info(f"Signal: FLAT - ensuring position for {signal.instrument}")
        
        return self.broker.ensure_position(
            instrument_info=instrument_info,
            init_position=init_position,
            desired_position="flat",
            leverage_percent=signal.capital_leverage_percent,
            reserve_capital=signal.reserve_capital
        )

    def _handle_position_signal(self, signal: Signal, instrument_info: InstrumentInfo, init_position: Position) -> tuple[Optional[Position], list[EnsureOrder]]:
        """Handle position signal (long/short)"""
        logger.info(f"Signal: {signal.position.upper()} - ensuring position for {signal.instrument}")
        
        # Ensure position matches signal (position size calculated automatically)
        return self.broker.ensure_position(
            instrument_info=instrument_info,
            init_position=init_position,
            desired_position=signal.position,
            leverage_percent=signal.capital_leverage_percent,
            reserve_capital=signal.reserve_capital,
            stop_price=signal.stop_price,
            take_price=signal.limit_price
        )
    
    def _calculate_slippage(self, signal: Signal, ensure_orders: list[EnsureOrder]) -> dict:
        """Calculate slippage from signal and ensure orders"""
        if not signal.entry_price and not signal.entry_time:
            return {}
        
        slippage = {}

        for ensure_order in ensure_orders:
            if ensure_order.type in ["buy", "sell"]:
                price_slippage = None
                if signal.entry_price:
                    order_price = ensure_order.result.price
                    # slippage > 0: losing money, slippage < 0: making money
                    if ensure_order.action in ["open_short", "close_long"]:
                        price_slippage = float(Decimal(str(signal.entry_price)) - Decimal(str(order_price)))
                    else:
                        price_slippage = float(Decimal(str(order_price)) - Decimal(str(signal.entry_price)))
                    
                time_slippage = None
                if signal.entry_time:
                    time_slippage = ensure_order.result.date - signal.entry_time
                
                slippage[ensure_order.order_id] = {
                    "price": price_slippage,
                    "time": time_slippage,
                }
        
        return slippage
    
    def _calculate_profit(self, instrument_info: InstrumentInfo, position: Position, ensure_orders: list[EnsureOrder]) -> float | None:
        """Calculate profit from orders for position"""
        if not position or not ensure_orders:
            return None

        # Action filter and profit multiplier based on position
        if position.quantity > 0:
            action_filter = "close_long"
            profit_multiplier = 1   # (result_price - position_price)
        else:
            action_filter = "close_short"
            profit_multiplier = -1  # (position_price - result_price)

        profit = 0.0
        profit_orders = [eo for eo in ensure_orders if eo.action == action_filter]

        if not profit_orders:
            return None

        for ensure_order in profit_orders:
            qty = ensure_order.quantity * instrument_info.lot_size
            profit += profit_multiplier * (ensure_order.result.price - position.average_price) * qty
        
        return profit
