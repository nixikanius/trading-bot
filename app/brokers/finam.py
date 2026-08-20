from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from grpc import RpcError, StatusCode

from google.type.decimal_pb2 import Decimal
from google.type.interval_pb2 import Interval
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import BaseModel, Field
from FinamPy import FinamPy
from FinamPy.grpc.auth_service_pb2 import AuthRequest, TokenDetailsRequest
from FinamPy.grpc.assets_service_pb2 import GetAssetRequest, GetAssetParamsRequest
from FinamPy.grpc.accounts_service_pb2 import GetAccountRequest, TradesRequest, TradesResponse
from FinamPy.grpc.marketdata_service_pb2 import QuoteRequest
from FinamPy.grpc.orders_service_pb2 import (
    Order, OrdersRequest, CancelOrderRequest,
    ORDER_STATUS_WATCHING, ORDER_TYPE_MARKET, ORDER_TYPE_STOP, ORDER_TYPE_STOP_LIMIT,
    STOP_CONDITION_LAST_UP, STOP_CONDITION_LAST_DOWN, VALID_BEFORE_GOOD_TILL_CANCEL,
)
from FinamPy.grpc.side_pb2 import SIDE_BUY, SIDE_SELL

from app.logger import get_logger
from app.brokers import BrokerService, TradingError, InstrumentInfo, Position, OrderResult, EnsureOrder, StopOrder

logger = get_logger(__name__)

RETRY_DELAY_SECONDS = 0.250
RETRYABLE_STATUS_CODES = {
    StatusCode.ABORTED,
    StatusCode.DEADLINE_EXCEEDED,
    StatusCode.INTERNAL,
    StatusCode.RESOURCE_EXHAUSTED,
    StatusCode.UNAVAILABLE,
}


class FinamConfig(BaseModel):
    token: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    request_timeout: float = Field(default=5.0, gt=0)
    request_max_attempts: int = Field(default=20, ge=1)
    non_retryable_request_timeout: float = Field(default=30.0, gt=0)


class FinamApiClient(FinamPy):
    """FinamPy client with bounded RPC calls and safe JWT refresh."""

    def __init__(
        self,
        access_token: str,
        request_timeout: float,
        request_max_attempts: int,
        non_retryable_request_timeout: float,
    ) -> None:
        self.request_timeout = request_timeout
        self.request_max_attempts = request_max_attempts
        self.non_retryable_request_timeout = non_retryable_request_timeout
        self._auth_lock = threading.RLock()
        super().__init__(access_token)

    @staticmethod
    def _rpc_name(func) -> str:
        method = getattr(func, "_method", b"unknown")
        return method.decode("utf-8") if isinstance(method, bytes) else str(method)

    def _call_rpc(self, func, request, *, metadata=None, max_attempts: int, timeout: float | None = None):
        rpc_name = self._rpc_name(func)
        request_timeout = self.request_timeout if timeout is None else timeout

        for attempt in range(1, max_attempts + 1):
            try:
                response, _ = func.with_call(
                    request=request,
                    timeout=request_timeout,
                    metadata=metadata,
                )
                return response
            except RpcError as ex:
                status_code = ex.code()
                details = ex.details() or str(ex)
                retryable = status_code in RETRYABLE_STATUS_CODES

                if not retryable or attempt == max_attempts:
                    raise TradingError(
                        code="FINAM_REQUEST_ERROR",
                        message=(
                            f"Finam request {rpc_name} failed after {attempt} attempt(s): "
                            f"{status_code.name}: {details}"
                        ),
                    ) from ex

                logger.info(
                    f"Finam request {rpc_name} failed with {status_code.name} "
                    f"(attempt {attempt}/{max_attempts}): {details}. Retrying..."
                )
                time.sleep(RETRY_DELAY_SECONDS)

        raise AssertionError("Finam RPC retry loop exited unexpectedly")

    def auth(self) -> None:
        """Refresh JWT with the same timeout and retry policy as read requests."""
        now = int(time.time())
        if self.jwt_token and now - self.jwt_token_issued <= self.jwt_token_ttl:
            return

        with self._auth_lock:
            now = int(time.time())
            if self.jwt_token and now - self.jwt_token_issued <= self.jwt_token_ttl:
                return

            response = self._call_rpc(
                self.auth_stub.Auth,
                AuthRequest(secret=self.access_token),
                max_attempts=self.request_max_attempts,
            )
            self.jwt_token = response.token
            self.jwt_token_issued = now
            self.metadata = ("authorization", self.jwt_token)

    def token_details(self):
        """Get token details without an unbounded RPC during client startup."""
        self.auth()
        return self._call_rpc(
            self.auth_stub.TokenDetails,
            TokenDetailsRequest(token=self.jwt_token),
            max_attempts=self.request_max_attempts,
        )

    def call_function(self, func, request, *, retry: bool = True):
        self.auth()
        max_attempts = self.request_max_attempts if retry else 1
        timeout = self.request_timeout if retry else self.non_retryable_request_timeout
        return self._call_rpc(
            func,
            request,
            metadata=(self.metadata,),
            max_attempts=max_attempts,
            timeout=timeout,
        )


def create_finam_service(config: dict[str, Any]) -> FinamBrokerService:
    """Create Finam trading service from config"""
    cfg = FinamConfig(**config)
    return FinamBrokerService(cfg)


class FinamBrokerService(BrokerService):
    def __init__(self, config: FinamConfig) -> None:
        self.config = config
        self._client = FinamApiClient(
            self.config.token,
            request_timeout=self.config.request_timeout,
            request_max_attempts=self.config.request_max_attempts,
            non_retryable_request_timeout=self.config.non_retryable_request_timeout,
        )

    def close(self) -> None:
        """Close the gRPC channel before interpreter shutdown."""
        self._client.close_channel()
    
    def call_function(self, func, request, *, retry: bool = True):
        """Call a Finam function with the configured timeout and retry policy."""
        return self._client.call_function(func, request, retry=retry)

    def get_instrument_info(self, instrument: str, max_attempts: int = 20, delay: float = 0.250) -> Optional[InstrumentInfo]:
        """Get instrument details waiting for them to be ready"""

        for attempt in range(max_attempts):
            instrument_info = self._get_instrument_info(instrument)
            if instrument_info.initial_margin_long != 0 and instrument_info.initial_margin_short != 0:
                return instrument_info

            logger.info(f"Waiting for instrument info ready (attempt {attempt + 1}/{max_attempts}) for instrument {instrument}")
            time.sleep(delay)
        
        raise TradingError(
            code="INSTRUMENT_INFO_READY_TIMEOUT",
            message=f"Instrument info ready timeout after {max_attempts} attempts for instrument {instrument}"
        )
    
    def _get_instrument_info(self, instrument: str) -> InstrumentInfo:
        """Get instrument details"""
        asset = self.call_function(
            self._client.assets_stub.GetAsset, GetAssetRequest(symbol=instrument, account_id=self.config.account_id))
        if not asset:
            return None
        
        asset_params = self.call_function(
            self._client.assets_stub.GetAssetParams, GetAssetParamsRequest(symbol=instrument, account_id=self.config.account_id))
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
    
    def get_position(self, instrument_info: InstrumentInfo) -> Optional[Position]:
        """Get current position for instrument from portfolio"""
        account = self.call_function(
            self._client.accounts_stub.GetAccount, GetAccountRequest(account_id=self.config.account_id))
        
        for position in account.positions:
            if position.symbol == instrument_info.instrument:
                return Position(
                    instrument=instrument_info.instrument,
                    quantity=int(float(position.quantity.value)),
                    average_price=float(position.average_price.value)
                )
        
        return None
    
    def get_position_waiting_for_state(self, instrument_info: InstrumentInfo, expected_quantity: int, max_attempts: int = 20, delay: float = 0.250) -> Optional[Position]:
        """Get current position for instrument from portfolio waiting for expected state"""
        for attempt in range(max_attempts):
            position = self.get_position(instrument_info)
            # Return position, if it's ready
            if position and position.quantity == expected_quantity and (position.average_price != 0 or expected_quantity == 0) \
                or not position and expected_quantity == 0:
                return position

            logger.info(f"Waiting for position state ready (attempt {attempt + 1}/{max_attempts}) for instrument {instrument_info.instrument}")
            time.sleep(delay)
        
        raise TradingError(
            code="POSITION_STATE_READY_TIMEOUT",
            message=f"Position state ready timeout after {max_attempts} attempts for instrument {instrument_info.instrument}"
        )

    def get_money_balance(self) -> float:
        """Get available money balance"""
        account = self.call_function(
            self._client.accounts_stub.GetAccount, GetAccountRequest(account_id=self.config.account_id))
        
        # balance = float(account.portfolio_mc.available_cash.value)
        # ugly way, but using available_cash sum is frequently delayed on changing position from short to long or vice versa
        balance = float(account.equity.value)
        return balance
    
    def get_last_price(self, instrument: str) -> float:
        """Get last price for instrument"""
        last_quote = self.call_function(
            self._client.marketdata_stub.LastQuote, QuoteRequest(symbol=instrument))
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
            raise TradingError(
                code="INVALID_PRICE_POSITION_DIRECTION",
                message=f"Invalid price size position direction: {position_direction}"
            )
        
        # 3. Calculate maximum lots allowed by leverage cap
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
                account_id=self.config.account_id,
                symbol=str(instrument_info.instrument),
                quantity=Decimal(value=str(quantity)),
                side=SIDE_SELL if direction == "sell" else SIDE_BUY,
                type=ORDER_TYPE_MARKET
            ),
            retry=False,
        )
        
        logger.info(f"Placed market {direction} order for {quantity} lots of {instrument_info.instrument}, order_id: {order.order_id}")
        return order.order_id

    def place_stop_loss_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, stop_price: float) -> str:
        """Place stop loss order"""
        order = self.call_function(
            self._client.orders_stub.PlaceOrder, Order(
                account_id=self.config.account_id,
                symbol=str(instrument_info.instrument),
                quantity=Decimal(value=str(quantity)),
                side=SIDE_SELL if direction == "sell" else SIDE_BUY,
                type=ORDER_TYPE_STOP,
                stop_price=Decimal(value=str(stop_price)),
                stop_condition=STOP_CONDITION_LAST_DOWN if direction == "sell" else STOP_CONDITION_LAST_UP,
                valid_before=VALID_BEFORE_GOOD_TILL_CANCEL,
            ),
            retry=False,
        )
        
        logger.info(f"Placed stop loss order for {quantity} lots of {instrument_info.instrument} at {stop_price}, order_id: {order.order_id}")
        return order.order_id
    
    def place_take_profit_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, take_price: float) -> str:
        """Place take profit order"""
        order = self.call_function(
            self._client.orders_stub.PlaceOrder, Order(
                account_id=self.config.account_id,
                symbol=str(instrument_info.instrument),
                quantity=Decimal(value=str(quantity)),
                side=SIDE_SELL if direction == "sell" else SIDE_BUY,
                type=ORDER_TYPE_STOP_LIMIT,
                limit_price=Decimal(value=str(take_price)),
                stop_price=Decimal(value=str(take_price)),
                stop_condition=STOP_CONDITION_LAST_UP if direction == "sell" else STOP_CONDITION_LAST_DOWN,
                valid_before=VALID_BEFORE_GOOD_TILL_CANCEL,
            ),
            retry=False,
        )
        
        logger.info(f"Placed take profit order for {quantity} lots of {instrument_info.instrument} at {take_price}, order_id: {order.order_id}")
        return order.order_id

    def cancel_stop_orders(self, orders: list[StopOrder]) -> None:
        """Cancel stop orders"""
        for order in orders:
            self.call_function(
                self._client.orders_stub.CancelOrder,
                CancelOrderRequest(account_id=self.config.account_id, order_id=order.order_id),
                retry=False,
            )
            logger.info(f"Cancelled stop order {order.order_id}")

    def get_current_stop_orders(self, instrument_info: InstrumentInfo) -> list[StopOrder]:
        """Get current active stop orders for instrument"""
        current_orders = []
        orders_result = self.call_function(
            self._client.orders_stub.GetOrders, OrdersRequest(account_id=self.config.account_id))
        
        for order in orders_result.orders:
            if order.status == ORDER_STATUS_WATCHING and order.order.type in [ORDER_TYPE_STOP, ORDER_TYPE_STOP_LIMIT] and \
                 order.order.symbol == instrument_info.instrument:
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
                    exchange_order_type="limit" if order.order.type == ORDER_TYPE_STOP_LIMIT else "market"
                ))
            
        return current_orders
    
    def pull_ensure_orders_result(self, ensure_orders: list[EnsureOrder], _: InstrumentInfo) -> list[EnsureOrder]:
        logger.info(f"Pulling ensure orders result for orders: {ensure_orders}")
        orders = [order for order in ensure_orders if order.type in ["buy", "sell"]]
        logger.info(f"Waiting for trades readiness for orders: {orders}")
        trades = self.get_trades_waiting_for_orders(orders)

        for order in ensure_orders:
            if order in orders:
                logger.info(f"Getting order result for order {order.order_id}")
                order.result = self.get_order_result(order, trades)

        return ensure_orders
    
    def get_trades_waiting_for_orders(self, orders: list[EnsureOrder], max_attempts: int = 20, delay: float = 0.250) -> list[TradesResponse]:
        for attempt in range(max_attempts):
            trades = self.get_trades()

            trades_ready = True
            for order in orders:
                trade_qty = sum([int(float(trade.size.value)) for trade in trades if trade.order_id == order.order_id])
                if trade_qty != order.quantity:
                    logger.info(f"Trades quantity {trade_qty} for order {order.order_id} does not match expected quantity {order.quantity}")
                    trades_ready = False
                    break

            if trades_ready:
                return trades
            
            logger.info(f"Waiting for trades readiness (attempt {attempt + 1}/{max_attempts})")
            time.sleep(delay)
        
        raise TradingError(
            code="TRADES_READINESS_TIMEOUT",
            message=f"Trades readiness timeout after {max_attempts} attempts"
        )

    def get_order_result(self, order: EnsureOrder, trades: list[TradesResponse]) -> OrderResult:
        order_trades = [trade for trade in trades if trade.order_id == order.order_id]
        order_date = max([datetime.fromtimestamp(trade.timestamp.seconds + trade.timestamp.nanos/1e9, tz=timezone.utc) for trade in order_trades])
        order_price = sum([float(trade.price.value) * int(float(trade.size.value)) for trade in order_trades]) / order.quantity
        
        return OrderResult(
            date=order_date,
            price=order_price
        )
    
    def get_trades(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None) -> list[TradesResponse]:
        if start_date is None:
            start_date = datetime.now() - timedelta(hours=1)
        if end_date is None:
            end_date = datetime.now() + timedelta(hours=1)

        trades = self.call_function(
            self._client.accounts_stub.Trades, TradesRequest(
                account_id=self.config.account_id,
                interval=Interval(
                    start_time=Timestamp(seconds=int(start_date.timestamp())),
                    end_time=Timestamp(seconds=int(end_date.timestamp())))
            ))

        return trades.trades
