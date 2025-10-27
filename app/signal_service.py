from __future__ import annotations

from app.config import AccountConfig
from app.logger import get_logger
from app.schemas import Signal
from app.tinvest_service import PositionInfo, EnsurePositionOrder
from app.tinvest_service import InvestContext, TInvestService, InstrumentInfo
from app.telegram_service import TelegramService

logger = get_logger(__name__)


class TradingError(Exception):
    """Base class for trading-related errors"""
    def __init__(self, message: str, code: str = "TRADING_ERROR"):
        super().__init__(message)
        self.code = code


class SignalService:
    def __init__(self, account_name: str, account_config: AccountConfig, telegram_service: TelegramService) -> None:
        self.account_name = account_name
        self.account_config = account_config
        self.ctx = InvestContext(
            token=account_config.api_token, 
            account_id=account_config.account_id,
            sandbox_mode=account_config.sandbox_mode
        )
        self.svc = TInvestService(self.ctx)
        self.telegram = telegram_service

    def process_signal(self, signal: Signal) -> dict:
        """Process TradingView signal and return result"""
        logger.info(f"Processing signal for {self.account_name}: {signal.model_dump()}")

        # Get instrument type and info
        instrument_type = self.svc.get_instrument_type(signal.figi)
        if not instrument_type:
            raise TradingError(
                message=f"Instrument not found: {signal.figi}",
                code="INSTRUMENT_NOT_FOUND"
            )
        instrument = self.svc.get_instrument_info(signal.figi, instrument_type)
        
        init_position = self.svc.get_position(signal.figi)
        logger.info(f"Initial position: {init_position}")
        
        # Handle signal based on position
        if signal.position == "flat":
            ensure_orders = self._handle_flat_signal(signal, instrument)
        else:
            ensure_orders = self._handle_position_signal(signal, instrument)
        
        ensure_orders = self.svc.pull_ensure_orders_state(instrument.instrument_type, ensure_orders)
        slippage = self._calculate_slippage(signal, ensure_orders)
        profit = self._calculate_profit(instrument, init_position, ensure_orders)
        position = self.svc.get_position_waiting_for_price(signal.figi)
        stop_orders = self.svc.get_current_stop_orders(signal.figi)

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

    def _handle_flat_signal(self, signal: Signal, instrument: InstrumentInfo) -> list[EnsurePositionOrder]:
        """Handle flat signal (close all positions)"""
        logger.info(f"Signal: FLAT - ensuring position for {signal.figi}")
        
        return self.svc.ensure_position(
            instrument=instrument,
            desired_position="flat",
            leverage_percent=signal.capital_leverage_percent,
            reserve_capital=signal.reserve_capital
        )

    def _handle_position_signal(self, signal: Signal, instrument: InstrumentInfo) -> list[EnsurePositionOrder]:
        """Handle position signal (long/short)"""
        logger.info(f"Signal: {signal.position.upper()} - ensuring position for {signal.figi}")
        
        # Ensure position matches signal (position size calculated inside)
        return self.svc.ensure_position(
            instrument=instrument,
            desired_position=signal.position,
            leverage_percent=signal.capital_leverage_percent,
            reserve_capital=signal.reserve_capital,
            stop_price=signal.stop_price,
            take_price=signal.limit_price
        )
    
    def _calculate_slippage(self, signal: Signal, ensure_orders: list[EnsurePositionOrder]) -> dict:
        """Calculate slippage from signal and ensure orders"""
        if not signal.entry_price and not signal.entry_time:
            return {}
        
        slippage = {}

        for ensure_order in ensure_orders:
            if ensure_order.type in ["buy", "sell"]:
                price_slippage = None
                if signal.entry_price:
                    order_price = ensure_order.state.price
                    # slippage > 0: losing money, slippage < 0: making money
                    if ensure_order.action in ["open_short", "close_long"]:
                        price_slippage = signal.entry_price - order_price
                    else:
                        price_slippage = order_price - signal.entry_price
                    
                time_slippage = None
                if signal.entry_time:
                    time_slippage = ensure_order.state.date - signal.entry_time
                
                slippage[ensure_order.order_id] = {
                    "price": price_slippage,
                    "time": time_slippage,
                }
        
        return slippage
    
    def _calculate_profit(self, instrument: InstrumentInfo, position: PositionInfo, ensure_orders: list[EnsurePositionOrder]) -> float | None:
        """Calculate profit from position and orders"""
        if not position or not ensure_orders:
            return None

        # Action filter and profit multiplier based on position
        if position.quantity > 0:
            action_filter = "close_long"
            profit_multiplier = 1   # (ensure_order.state.price - position.average_price)
        else:
            action_filter = "close_short"
            profit_multiplier = -1  # (position.average_price - ensure_order.state.price)

        profit = 0.0
        profit_orders = [eo for eo in ensure_orders if eo.action == action_filter]

        for ensure_order in profit_orders:
            qty = ensure_order.quantity * instrument.lot_size * (instrument.basic_asset_size or 1)
            profit += profit_multiplier * (ensure_order.state.price - position.average_price) * qty
        
        return profit
