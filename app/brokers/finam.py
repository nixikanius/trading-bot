from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from grpc import RpcError
from google.type.decimal_pb2 import Decimal
from google.type.interval_pb2 import Interval
from google.protobuf.timestamp_pb2 import Timestamp
from FinamPy import FinamPy
from FinamPy.grpc.assets.assets_service_pb2 import GetAssetRequest, GetAssetParamsRequest
from FinamPy.grpc.accounts.accounts_service_pb2 import GetAccountRequest, TradesRequest, TradesResponse
from FinamPy.grpc.marketdata.marketdata_service_pb2 import QuoteRequest
from FinamPy.grpc.orders.orders_service_pb2 import (
    Order, OrdersRequest, CancelOrderRequest,
    ORDER_STATUS_WATCHING, ORDER_TYPE_MARKET, ORDER_TYPE_STOP, ORDER_TYPE_STOP_LIMIT,
    STOP_CONDITION_LAST_UP, STOP_CONDITION_LAST_DOWN, VALID_BEFORE_GOOD_TILL_CANCEL,
)
from FinamPy.grpc.side_pb2 import SIDE_BUY, SIDE_SELL

from pydantic import BaseModel, ValidationInfo, field_validator

from app.logger import get_logger
from app.schemas import Instrument
from app.brokers import BrokerService, TradingError, InstrumentInfo, Position, OrderResult, EnsureOrder, StopOrder

logger = get_logger(__name__)


class FinamContext(BaseModel):
    token: str
    account_id: str


def create_finam_service(context: dict[str, Any]) -> FinamBrokerService:
    """Create Finam trading service from context"""
    ctx = FinamContext(**context)
    return FinamBrokerService(ctx)


class FinamBrokerService(BrokerService):
    def __init__(self, ctx: FinamContext) -> None:
        self.ctx = ctx
        self._client = FinamPy(self.ctx.token)
    
    def call_function(self, func, request):
        """Call FinamPy function (fork of _client.call_function with proper error handling)"""

        self._client.auth()
        func_name = func._method.decode('utf-8')
        logger.debug(f'FinamPy request: {func_name}({request})')
        while True:
            try:
                response, _ = func.with_call(request=request, metadata=(self._client.metadata,))
                logger.debug(f'FinamPy response: {response}')
                return response
            except RpcError as ex:
                details = ex.args[0].details
                raise TradingError(code="FINAM_REQUEST_ERROR",
                                   message=f"Failed to call function {func_name} with request ({request}): {details}")

    def get_instrument_info(self, instrument: Instrument) -> Optional[InstrumentInfo]:
        """Get instrument details. Returns None if not found."""
        asset = self.call_function(
            self._client.assets_stub.GetAsset, GetAssetRequest(symbol=str(instrument), account_id=self.ctx.account_id))
        if not asset:
            return None
        
        asset_params = self.call_function(
            self._client.assets_stub.GetAssetParams, GetAssetParamsRequest(symbol=str(instrument), account_id=self.ctx.account_id))
        lot_size = float(asset.lot_size.value)
        min_price_step = int(asset.min_step)/lot_size
        initial_margin_long = float(int(asset_params.long_initial_margin.units) + asset_params.long_initial_margin.nanos / 1e9)
        initial_margin_short = float(int(asset_params.short_initial_margin.units) + asset_params.short_initial_margin.nanos / 1e9)

        return InstrumentInfo(
            instrument=instrument,
            name=asset.name,
            type=asset.type,
            currency=asset_params.long_initial_margin.currency_code,
            lot_size=lot_size,
            min_price_step=min_price_step,
            initial_margin_long=initial_margin_long,
            initial_margin_short=initial_margin_short
        )
    
    def get_position(self, instrument: Instrument) -> Optional[Position]:
        """Get current position for instrument from portfolio"""
        account = self.call_function(
            self._client.accounts_stub.GetAccount, GetAccountRequest(account_id=self.ctx.account_id))
        
        for position in account.positions:
            if position.symbol == str(instrument):
                return Position(
                    instrument=instrument,
                    quantity=int(float(position.quantity.value)),
                    average_price=float(position.average_price.value)
                )
        
        return None
    
    def get_position_waiting_for_state(self, instrument: Instrument, expected_quantity: int, max_attempts: int = 20, delay: float = 0.250) -> Optional[Position]:
        """Get current position for instrument from portfolio waiting for expected state"""
        for attempt in range(max_attempts):
            position = self.get_position(instrument)
            # Return position, if it's ready
            if position and position.quantity == expected_quantity and (position.average_price != 0 or expected_quantity == 0) \
                or not position and expected_quantity == 0:
                return position

            logger.info(f"Waiting for position state ready (attempt {attempt + 1}/{max_attempts}) for instrument {instrument}")
            time.sleep(delay)
        
        raise TradingError(code="POSITION_STATE_READY_TIMEOUT", message=f"Position state ready timeout after {max_attempts} attempts for instrument {instrument}")

    def get_money_balance(self) -> float:
        """Get available money balance in specified currency"""
        account = self.call_function(
            self._client.accounts_stub.GetAccount, GetAccountRequest(account_id=self.ctx.account_id))
        
        balance = float(account.portfolio_mc.available_cash.value)
        return balance
    
    def get_last_price(self, instrument: Instrument) -> float:
        """Get last price for instrument"""
        last_quote = self.call_function(
            self._client.marketdata_stub.LastQuote, QuoteRequest(symbol=str(instrument)))
        last_price = float(last_quote.quote.last.value)

        return last_price

    def calculate_position_size(self, instrument_info: InstrumentInfo, leverage_percent: float, reserve_capital: float, position_direction: str = "long") -> int:
        """Calculate position size based on available funds, leverage cap, and futures margin requirements"""
        available_money = self.get_money_balance()
        last_price = self.get_last_price(instrument_info.instrument)

        # 1. Upper limit: (available_money + reserve_capital) * leverage_percent
        total_capital = available_money + reserve_capital
        leverage_cap = total_capital * (leverage_percent / 100.0)
        
        # 2. Get maximum lots available for purchase based on position direction
        # TODO: Add margin limits
        if position_direction == "long":
            quantity_by_balance = int(available_money // instrument_info.initial_margin_long)
        elif position_direction == "short":
            quantity_by_balance = int(available_money // instrument_info.initial_margin_short)
        else:
            raise ValueError(f"Invalid position direction: {position_direction}")
        
        # 3. Calculate maximum lots allowed by leverage cap
        # Get current price from market data to calculate leverage limit
        per_lot_cost = last_price * instrument_info.lot_size
        quantity_by_leverage = int(leverage_cap // per_lot_cost)
        
        # 4. Final quantity: minimum of margin and leverage constraints
        quantity = min(quantity_by_balance, quantity_by_leverage)
        
        logger.info(f"Position calculation for {instrument_info.instrument}: available={available_money}, leverage_cap={leverage_cap}, per_lot_cost={per_lot_cost}, by_balance={quantity_by_balance}, by_leverage={quantity_by_leverage}, final={quantity}")
        
        return quantity

    def place_market_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int) -> str:
        """Place market order"""
        order = self.call_function(
            self._client.orders_stub.PlaceOrder, Order(
                account_id=self.ctx.account_id,
                symbol=str(instrument_info.instrument),
                quantity=Decimal(value=str(quantity)),
                side=SIDE_SELL if direction == "sell" else SIDE_BUY,
                type=ORDER_TYPE_MARKET,
                # valid_before=VALID_BEFORE_GOOD_TILL_CANCEL,
            ))
        
        logger.info(f"Placed market {direction} order for {quantity} lots of {instrument_info.instrument}, order_id: {order.order_id}")
        return order.order_id

    def place_stop_loss_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, stop_price: float) -> str:
        """Place stop loss order"""
        order = self.call_function(
            self._client.orders_stub.PlaceOrder, Order(
                account_id=self.ctx.account_id,
                symbol=str(instrument_info.instrument),
                quantity=Decimal(value=str(quantity)),
                side=SIDE_SELL if direction == "sell" else SIDE_BUY,
                type=ORDER_TYPE_STOP,
                stop_price=Decimal(value=str(stop_price)),
                stop_condition=STOP_CONDITION_LAST_DOWN if direction == "sell" else STOP_CONDITION_LAST_UP,
                valid_before=VALID_BEFORE_GOOD_TILL_CANCEL,
            ))
        
        logger.info(f"Placed stop loss order for {quantity} lots of {instrument_info.instrument} at {stop_price}, order_id: {order.order_id}")
        return order.order_id
    
    def place_take_profit_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, take_price: float) -> str:
        """Place take profit order"""
        order = self.call_function(
            self._client.orders_stub.PlaceOrder, Order(
                account_id=self.ctx.account_id,
                symbol=str(instrument_info.instrument),
                quantity=Decimal(value=str(quantity)),
                side=SIDE_SELL if direction == "sell" else SIDE_BUY,
                type=ORDER_TYPE_STOP,
                stop_price=Decimal(value=str(take_price)),
                stop_condition=STOP_CONDITION_LAST_UP if direction == "sell" else STOP_CONDITION_LAST_DOWN,
                valid_before=VALID_BEFORE_GOOD_TILL_CANCEL,
            ))
        
        logger.info(f"Placed take profit order for {quantity} lots of {instrument_info.instrument} at {take_price}, order_id: {order.order_id}")
        return order.order_id

    def cancel_orders(self, orders: list[StopOrder]) -> None:
        """Cancel orders"""
        for order in orders:
            self.cancel_order(order)

    def cancel_order(self, order: StopOrder) -> None:
        """Cancel order"""
        self.call_function(
            self._client.orders_stub.CancelOrder, CancelOrderRequest(account_id=self.ctx.account_id, order_id=order.order_id))
        logger.info(f"Cancelled order {order.order_id}")

    def get_current_stop_orders(self, instrument: Instrument) -> list[StopOrder]:
        """Get current active stop orders for instrument"""
        current_orders = []
        orders_result = self.call_function(
            self._client.orders_stub.GetOrders, OrdersRequest(account_id=self.ctx.account_id))
        
        for order in orders_result.orders:
            if order.status == ORDER_STATUS_WATCHING and order.order.type in [ORDER_TYPE_STOP, ORDER_TYPE_STOP_LIMIT] and \
                 order.order.symbol == str(instrument):
                if order.order.stop_condition == STOP_CONDITION_LAST_DOWN and order.order.side == SIDE_SELL \
                    or order.order.stop_condition == STOP_CONDITION_LAST_UP and order.order.side == SIDE_BUY:
                    order_type = 'stop_loss'
                else:
                    order_type = 'take_profit'

                current_orders.append(StopOrder(
                    order_id=order.order_id,
                    order_type=order_type,
                    direction="sell" if order.order.side == SIDE_SELL else "buy",
                    quantity=int(float(order.order.quantity.value)),
                    price=float(order.order.limit_price.value) if order.order.type == ORDER_TYPE_STOP_LIMIT else None,
                    stop_price=float(order.order.stop_price.value),
                    exchange_order_type="market" if hasattr(order.order, 'limit_price') else "limit"
                ))
            
        return current_orders
    
    def pull_ensure_orders_result(self, ensure_orders: list[EnsureOrder]) -> list[EnsureOrder]:
        trades = self.get_trades()

        for ensure_order in ensure_orders:
            if ensure_order.type in ["buy", "sell"]:
                order_result = self.get_order_result(ensure_order.order_id, trades)
                ensure_order.result = order_result

        return ensure_orders

    def get_order_result(self, order_id: str, trades: list[TradesResponse]) -> OrderResult:
        for trade in trades:
            if trade.order_id == order_id:
                return OrderResult(
                    date=datetime.fromtimestamp(trade.timestamp.seconds + trade.timestamp.nanos/1e9, tz=timezone.utc),
                    price=float(trade.price.value)
                )
        
        raise TradingError(code="ORDER_TRADE_NOT_FOUND", message=f"Order {order_id} not found in trades")
    
    def get_trades(self, start_date: datetime = datetime.now() - timedelta(days=1), end_date: datetime = datetime.now() + timedelta(days=1)) -> list[TradesResponse]:
        trades = self.call_function(
            self._client.accounts_stub.Trades, TradesRequest(
                account_id=self.ctx.account_id,
                interval=Interval(
                    start_time=Timestamp(seconds=int(start_date.timestamp())),
                    end_time=Timestamp(seconds=int(end_date.timestamp())))
            ))

        return trades.trades
