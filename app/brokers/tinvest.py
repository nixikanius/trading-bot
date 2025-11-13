from __future__ import annotations

import time
from typing import Optional, Any
from contextlib import contextmanager
from pydantic import BaseModel

from app.logger import get_logger
from app.brokers import BrokerService, TradingError, InstrumentInfo, Position, OrderResult, EnsureOrder, StopOrder
from tinkoff.invest import Client, OrderDirection, OrderType, Quotation, StopOrderDirection, StopOrderType, StopOrderExpirationType, ExchangeOrderType, PriceType
from tinkoff.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from tinkoff.invest.schemas import GetMaxLotsRequest, InstrumentIdType
from tinkoff.invest.exceptions import RequestError

logger = get_logger(__name__)


class TInvestConfig(BaseModel):
    token: str
    account_id: str
    sandbox_mode: bool = False


def create_tinvest_service(config: dict[str, Any]) -> TInvestBrokerService:
    """Create Tinkoff Invest trading service from config"""
    cfg = TInvestConfig(**config)
    return TInvestBrokerService(cfg)


class TInvestBrokerService(BrokerService):
    def __init__(self, config: TInvestConfig) -> None:
        self.config = config

    @contextmanager
    def _client(self):
        """Context manager that creates a Tinkoff Invest client with error handling"""
        target = INVEST_GRPC_API_SANDBOX if self.config.sandbox_mode else INVEST_GRPC_API

        try:
            with Client(self.config.token, target=target) as client:
                yield client
        except RequestError as e:
            code = e.code.name if e.code else "UNKNOWN"
            message = getattr(e.metadata, "message", None) if e.metadata else None
            message = message or e.details or "Trading request error"

            raise TradingError(
                code="TINVEST_REQUEST_ERROR",
                message=f"TInvest request error ({code}): {message}"
            )

    def _get_instrument_type(self, figi: str) -> str | None:
        """Get instrument type by FIGI. Returns None if not found."""
        with self._client() as client:
            instrument = client.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            return instrument.instrument.instrument_type

    def get_instrument_info(self, instrument: str) -> Optional[InstrumentInfo]:
        """Get instrument details including currency and lot size"""
        instrument_type = self._get_instrument_type(instrument)
        if not instrument_type:
            return None

        with self._client() as client:
            if instrument_type == "share":
                instrument_response = client.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            elif instrument_type == "futures":
                instrument_response = client.instruments.future_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            elif instrument_type == "bonds":
                instrument_response = client.instruments.bond_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            elif instrument_type == "etfs":
                instrument_response = client.instruments.etf_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            elif instrument_type == "currencies":
                instrument_response = client.instruments.currency_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            elif instrument_type == "options":
                instrument_response = client.instruments.option_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            elif instrument_type == "structured_products":
                instrument_response = client.instruments.structured_product_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=instrument)
            else:
                raise TradingError(
                    code="TINVEST_UNSUPPORTED_INSTRUMENT_TYPE",
                    message=f"Unsupported TInvest instrument type: {instrument_type}"
                )
            
        instrument_data = instrument_response.instrument
        
        # Extract basic_asset_size for futures (used for margin calculation)
        basic_asset_size = None
        if hasattr(instrument_data, 'basic_asset_size'):
            basic_asset_size = float(instrument_data.basic_asset_size.units + instrument_data.basic_asset_size.nano / 1e9)
        
        lot_size = float(instrument_data.lot) * (basic_asset_size or 1)
        min_price_step = float(instrument_data.min_price_increment.units + instrument_data.min_price_increment.nano / 1e9)
        
        return InstrumentInfo(
            instrument=instrument,
            name=instrument_data.name,
            type=instrument_type,
            currency=instrument_data.currency,
            lot_size=lot_size,
            min_price_step=min_price_step
        )
    
    def get_position(self, instrument_info: InstrumentInfo) -> Optional[Position]:
        """Get current position for instrument from portfolio"""
        with self._client() as client:
            portfolio = client.operations.get_portfolio(account_id=self.config.account_id)

        for position in list(portfolio.positions):
            if position.figi == instrument_info.instrument:
                return Position(
                    instrument=instrument_info.instrument,
                    quantity=int(position.quantity.units + position.quantity.nano / 1e9),
                    average_price=float(position.average_position_price.units + position.average_position_price.nano / 1e9)
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

    def get_money_balance(self, currency: str) -> float:
        """Get available money balance in specified currency"""
        with self._client() as client:
            positions = client.operations.get_positions(account_id=self.config.account_id)
            
        # Return balance for specified currency
        for money in positions.money:
            if money.currency == currency:
                return float(money.units + money.nano / 1e9)
        
        return 0.0

    def get_last_price(self, instrument: str) -> float:
        """Get last price for instrument"""
        with self._client() as client:
            last_prices_response = client.market_data.get_last_prices(figi=[instrument])
        
        if not last_prices_response.last_prices:
            raise TradingError(
                code="NO_PRICE_DATA",
                message=f"No price data available for {instrument}"
            )
        
        last_price_data = last_prices_response.last_prices[0]
        return float(last_price_data.price.units + last_price_data.price.nano / 1e9)

    def calculate_position_size(self, instrument_info: InstrumentInfo, leverage_percent: float, reserve_capital: float, position_direction: str = "long") -> int:
        """Calculate position size based on available funds, leverage cap, and futures margin requirements"""
        currency = instrument_info.currency
        available_money = self.get_money_balance(currency)
        last_price = self.get_last_price(instrument_info.instrument)

        # 1. Upper limit: (available_money + reserve_capital) * leverage_percent
        total_capital = available_money + reserve_capital
        leverage_cap = total_capital * (leverage_percent / 100.0)
        
        # 2. Get maximum lots available for purchase
        with self._client() as client:
            # Get max lots for purchase
            max_lots_request = GetMaxLotsRequest(
                account_id=self.config.account_id,
                instrument_id=instrument_info.instrument
            )
            max_lots_response = client.orders.get_max_lots(max_lots_request)
            
            # Extract limits based on position direction
            if position_direction == "long":
                quantity_by_balance = max_lots_response.buy_limits.buy_max_lots

                # Use margin limits if available, otherwise fallback to balance limits
                if hasattr(max_lots_response, 'buy_margin_limits'):
                    quantity_by_margin = max_lots_response.buy_margin_limits.buy_max_lots
                else:
                    quantity_by_margin = quantity_by_balance
            elif position_direction == "short":
                quantity_by_balance = max_lots_response.sell_limits.sell_max_lots
                
                # Use margin limits if available, otherwise fallback to balance limits
                if hasattr(max_lots_response, 'sell_margin_limits'):
                    quantity_by_margin = max_lots_response.sell_margin_limits.sell_max_lots
                else:
                    quantity_by_margin = quantity_by_balance
            else:
                raise TradingError(
                    code="INVALID_PRICE_POSITION_DIRECTION",
                    message=f"Invalid price size position direction: {position_direction}"
                )
        
        # 3. Calculate maximum lots allowed by leverage cap
        per_lot_cost = last_price * instrument_info.lot_size
        quantity_by_leverage = int(leverage_cap // per_lot_cost)
        
        # 4. Final quantity: minimum of margin and leverage constraints
        quantity = min(quantity_by_margin, quantity_by_leverage)
        
        logger.info(f"Position calculation for {instrument_info.instrument}: available={available_money:.2f}, leverage_cap={leverage_cap:.2f}, per_lot_cost={per_lot_cost:.2f}, by_balance={quantity_by_balance}, by_margin={quantity_by_margin}, by_leverage={quantity_by_leverage}, final={quantity}")
        
        return quantity

    def place_market_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int) -> str:
        """Place market order"""
        order_direction = OrderDirection.ORDER_DIRECTION_BUY if direction == "buy" else OrderDirection.ORDER_DIRECTION_SELL
        
        with self._client() as client:
            response = client.orders.post_order(
                figi=instrument_info.instrument,
                quantity=quantity,
                price=Quotation(units=0, nano=0),  # Market order
                direction=order_direction,
                account_id=self.config.account_id,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id="",  # Let server generate
            )
            
        logger.info(f"Placed market {direction} order for {quantity} lots of {instrument_info.instrument}, order_id: {response.order_id}")
        return response.order_id

    def place_stop_loss_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, stop_price: float) -> str:
        """Place stop loss order"""
        order_direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if direction == "sell" else StopOrderDirection.STOP_ORDER_DIRECTION_BUY

        with self._client() as client:
            response = client.stop_orders.post_stop_order(
                figi=instrument_info.instrument,
                quantity=quantity,
                stop_price=Quotation(units=int(stop_price), nano=int((stop_price - int(stop_price)) * 1e9)),
                direction=order_direction,
                account_id=self.config.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
            )
        
        logger.info(f"Placed stop loss order for {quantity} lots of {instrument_info.instrument} at {stop_price}, order_id: {response.stop_order_id}")
        return response.stop_order_id

    def place_take_profit_order(self, instrument_info: InstrumentInfo, direction: str, quantity: int, take_price: float) -> str:
        """Place take profit order"""
        order_direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if direction == "sell" else StopOrderDirection.STOP_ORDER_DIRECTION_BUY

        with self._client() as client:
            response = client.stop_orders.post_stop_order(
                figi=instrument_info.instrument,
                quantity=quantity,
                stop_price=Quotation(units=int(take_price), nano=int((take_price - int(take_price)) * 1e9)),
                direction=order_direction,
                account_id=self.config.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
            )
        
        logger.info(f"Placed take profit order for {quantity} lots of {instrument_info.instrument} at {take_price}, order_id: {response.stop_order_id}")
        return response.stop_order_id

    def cancel_stop_orders(self, orders: list[StopOrder]) -> None:
        """Cancel stop orders"""
        with self._client() as client:
            for order in orders:
                client.stop_orders.cancel_stop_order(account_id=self.config.account_id, stop_order_id=order.order_id)
                logger.info(f"Cancelled stop order {order.order_id}")

    def get_current_stop_orders(self, instrument_info: InstrumentInfo) -> list[StopOrder]:
        """Get current active stop orders for instrument"""
        with self._client() as client:
            stop_orders = client.stop_orders.get_stop_orders(account_id=self.config.account_id)
        
        current_orders = []
        for order in stop_orders.stop_orders:
            if order.figi == instrument_info.instrument:
                current_orders.append(StopOrder(
                    order_id=order.stop_order_id,
                    order_type="stop_loss" if order.order_type == StopOrderType.STOP_ORDER_TYPE_STOP_LOSS else "take_profit",
                    direction="sell" if order.direction == StopOrderDirection.STOP_ORDER_DIRECTION_SELL else "buy",
                    quantity=order.lots_requested,
                    price=float(order.price.units + order.price.nano / 1e9) if order.price else None,
                    stop_price=float(order.stop_price.units + order.stop_price.nano / 1e9) if order.stop_price else None,
                    exchange_order_type="market" if order.exchange_order_type == ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET else "limit"
                ))
        
        return current_orders

    def pull_ensure_orders_result(self, ensure_orders: list[EnsureOrder], instrument_info: InstrumentInfo) -> list[EnsureOrder]:
        """Pull execution results for ensure orders"""
        if instrument_info.type in ["futures", "bonds"]:
            price_type = PriceType.PRICE_TYPE_POINT
        else:
            price_type = PriceType.PRICE_TYPE_CURRENCY

        for ensure_order in ensure_orders:
            if ensure_order.type in ["buy", "sell"]:
                order_result = self.get_order_result(ensure_order.order_id, price_type)
                ensure_order.result = order_result

        return ensure_orders

    def get_order_result(self, order_id: str, price_type: PriceType) -> OrderResult:
        with self._client() as client:
            order_state = client.orders.get_order_state(account_id=self.config.account_id, order_id=order_id, price_type=price_type)
        
        order_result = OrderResult(
            date=order_state.order_date,
            price=float(order_state.average_position_price.units + order_state.average_position_price.nano / 1e9)
        )
        return order_result
