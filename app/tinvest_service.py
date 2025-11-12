from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.logger import get_logger
from tinkoff.invest import Client, OrderDirection, OrderType, Quotation, StopOrderDirection, StopOrderType, StopOrderExpirationType, ExchangeOrderType, PriceType
from tinkoff.invest.constants import INVEST_GRPC_API, INVEST_GRPC_API_SANDBOX
from tinkoff.invest.exceptions import RequestError
from tinkoff.invest.schemas import GetMaxLotsRequest, InstrumentIdType

logger = get_logger(__name__)


@dataclass
class InvestContext:
    token: str
    account_id: str
    sandbox_mode: bool = False

@dataclass
class PositionInfo:
    position_uid: str
    figi: str
    quantity: int  # positive for long, negative for short
    average_price: float

@dataclass
class EnsurePositionOrderState:
    date: datetime
    price: float

@dataclass
class EnsurePositionOrder:
    type: str  # "buy", "sell", "stop_loss", "take_profit"
    quantity: int
    order_id: str
    action: Optional[str] = None  # "open_long", "close_short", etc.
    price: Optional[float] = None # for stop_loss and take_profit
    state: Optional[EnsurePositionOrderState] = None # for buy and sell

@dataclass
class StopOrderInfo:
    order_id: str
    order_type: str  # "stop_loss" or "take_profit"
    direction: str  # "buy" or "sell"
    quantity: int
    price: Optional[float] = None  # for take profit
    stop_price: Optional[float] = None  # for stop loss
    exchange_order_type: str = "market"  # "market" or "limit"

@dataclass
class InstrumentInfo:
    figi: str
    instrument_type: str
    ticker: str
    name: str
    currency: str
    lot_size: int
    min_price_increment: float
    basic_asset_size: Optional[float] = None


class TInvestService:
    def __init__(self, ctx: InvestContext) -> None:
        self.ctx = ctx

    def _client(self) -> Client:
        target = INVEST_GRPC_API_SANDBOX if self.ctx.sandbox_mode else INVEST_GRPC_API
        return Client(self.ctx.token, target=target)

    def get_instrument_type(self, figi: str) -> str | None:
        """Get instrument type by FIGI. Returns None if not found."""
        with self._client() as client:
            try:
                instrument = client.instruments.get_instrument_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
                return instrument.instrument.instrument_type.lower()
            except RequestError as e:
                if e.code and e.code.name == "NOT_FOUND":
                    return None
                raise

    def get_instrument_info(self, figi: str, instrument_type: str) -> InstrumentInfo | None:
        """Get instrument details including currency and lot size. Returns None if not found."""
        with self._client() as client:
            if instrument_type == "share":
                instrument = client.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            elif instrument_type == "futures":
                instrument = client.instruments.future_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            elif instrument_type == "bonds":
                instrument = client.instruments.bond_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            elif instrument_type == "etfs":
                instrument = client.instruments.etf_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            elif instrument_type == "currencies":
                instrument = client.instruments.currency_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            elif instrument_type == "options":
                instrument = client.instruments.option_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            elif instrument_type == "structured_products":
                instrument = client.instruments.structured_product_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi)
            else:
                raise ValueError(f"Unsupported instrument type: {instrument_type}")
            
            instrument_data = instrument.instrument
            
            # Extract basic_asset_size for futures
            basic_asset_size = None
            if hasattr(instrument_data, 'basic_asset_size'):
                basic_asset_size = float(instrument_data.basic_asset_size.units + instrument_data.basic_asset_size.nano / 1e9)
            
            return InstrumentInfo(
                figi=figi,
                instrument_type=instrument_type,
                ticker=instrument_data.ticker,
                name=instrument_data.name,
                currency=instrument_data.currency,
                lot_size=instrument_data.lot,
                min_price_increment=float(instrument_data.min_price_increment.units + instrument_data.min_price_increment.nano / 1e9),
                basic_asset_size=basic_asset_size
            )

    def get_money_balance(self, currency: str = "rub") -> float:
        """Get available money balance in specified currency"""
        with self._client() as client:
            positions = client.operations.get_positions(account_id=self.ctx.account_id)
            
            for money in positions.money:
                if money.currency.lower() == currency.lower():
                    return float(money.units + money.nano / 1e9)
            
            return 0.0

    def get_position(self, figi: str) -> Optional[PositionInfo]:
        """Get current position for instrument from portfolio"""
        with self._client() as client:
            portfolio = client.operations.get_portfolio(account_id=self.ctx.account_id)

            for position in list(portfolio.positions):
                if position.figi == figi:
                    return PositionInfo(
                        position_uid=position.position_uid,
                        figi=figi,
                        quantity=int(position.quantity.units + position.quantity.nano / 1e9),
                        average_price=float(position.average_position_price.units + position.average_position_price.nano / 1e9)
                    )
            
            return None

    def get_position_waiting_for_price(self, figi: str, max_attempts: int = 10, delay_ms: int = 500) -> Optional[PositionInfo]:
        """Get current position for instrument from portfolio with waiting for price calculation"""
        for attempt in range(max_attempts):
            position = self.get_position(figi)
            if not position:
                return None
            
            # Return position, if it's price is ready
            if position.average_price != 0 or position.quantity == 0:
                return position

            logger.info(f"Waiting for position price calculation (attempt {attempt + 1}/{max_attempts})")
            time.sleep(delay_ms / 1000.0)
        
        raise TimeoutError(f"Position price calculation timeout after {max_attempts} attempts for instrument {figi}")

    def calculate_position_size(self, instrument: InstrumentInfo, leverage_percent: float, reserve_capital: float, position_direction: str = "long") -> int:
        """Calculate position size based on available funds, leverage cap, and futures margin requirements"""
        figi = instrument.figi
        
        currency = instrument.currency.lower()
        available_money = self.get_money_balance(currency)
        # 1. Upper limit: (available_money + reserve_capital) * leverage_percent
        total_capital = available_money + reserve_capital

        leverage_cap = total_capital * (leverage_percent / 100.0)
        
        # 2. Get maximum lots available for purchase using GetMaxLots
        with self._client() as client:
            # Get max lots for purchase
            max_lots_request = GetMaxLotsRequest(
                account_id=self.ctx.account_id,
                instrument_id=figi
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
                raise ValueError(f"Invalid position direction: {position_direction}")
        
        # 3. Calculate maximum lots allowed by leverage cap
        # Get current price from market data to calculate leverage limit
        with self._client() as client:
            # Get real price from market data
            last_prices_response = client.market_data.get_last_prices(figi=[figi])
            if not last_prices_response.last_prices:
                raise ValueError(f"No price data available for {figi}")
            
            current_price = float(last_prices_response.last_prices[0].price.units + last_prices_response.last_prices[0].price.nano / 1e9)
            per_lot_cost = current_price * instrument.lot_size * (instrument.basic_asset_size or 1)
            
            quantity_by_leverage = int(leverage_cap // per_lot_cost)
        
        # 4. Final quantity: minimum of margin and leverage constraints
        quantity = min(quantity_by_margin, quantity_by_leverage)
        
        logger.info(f"Position calculation for {figi}: available={available_money:.2f}, leverage_cap={leverage_cap:.2f}, per_lot_cost={per_lot_cost:.2f}, by_balance={quantity_by_balance}, by_margin={quantity_by_margin}, by_leverage={quantity_by_leverage}, final={quantity}")
        
        return quantity

    def place_market_order(self, figi: str, direction: OrderDirection, quantity: int) -> str:
        """Place market order and return order ID"""
        with self._client() as client:
            response = client.orders.post_order(
                figi=figi,
                quantity=quantity,
                price=Quotation(units=0, nano=0),  # Market order
                direction=direction,
                account_id=self.ctx.account_id,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id="",  # Let server generate
            )
            
            direction_name = "BUY" if direction == OrderDirection.ORDER_DIRECTION_BUY else "SELL"
            logger.info(f"Placed {direction_name} order for {quantity} lots of {figi}, order_id: {response.order_id}")
            return response.order_id
    
    def place_limit_order(self, figi: str, direction: OrderDirection, quantity: int, limit_price: float) -> str:
        """Place limit order"""
        with self._client() as client:
            response = client.orders.post_order(
                figi=figi,
                quantity=quantity,
                price=Quotation(units=int(limit_price), nano=int((limit_price - int(limit_price)) * 1e9)),
                direction=direction,
                account_id=self.ctx.account_id,
                order_type=OrderType.ORDER_TYPE_LIMIT,
                order_id="",
            )
            
            logger.info(f"Placed limit order for {quantity} lots of {figi} at {limit_price}, order_id: {response.order_id}")
            return response.order_id

    def place_stop_order(self, figi: str, direction: OrderDirection, quantity: int, stop_price: float) -> str:
        """Place stop loss order"""
        with self._client() as client:
            # Convert OrderDirection to StopOrderDirection
            stop_direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if direction == OrderDirection.ORDER_DIRECTION_SELL else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
            
            response = client.stop_orders.post_stop_order(
                figi=figi,
                quantity=quantity,
                stop_price=Quotation(units=int(stop_price), nano=int((stop_price - int(stop_price)) * 1e9)),
                direction=stop_direction,
                account_id=self.ctx.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
            )
            
            logger.info(f"Placed stop loss order for {quantity} lots of {figi} at {stop_price}, order_id: {response.stop_order_id}")
            return response.stop_order_id

    def place_take_profit_order(self, figi: str, direction: OrderDirection, quantity: int, take_price: float) -> str:
        """Place take profit order"""
        with self._client() as client:
            # Convert OrderDirection to StopOrderDirection
            stop_direction = StopOrderDirection.STOP_ORDER_DIRECTION_SELL if direction == OrderDirection.ORDER_DIRECTION_SELL else StopOrderDirection.STOP_ORDER_DIRECTION_BUY
            
            # Place market order to prevent order not being executed
            response = client.stop_orders.post_stop_order(
                figi=figi,
                quantity=quantity,
                stop_price=Quotation(units=int(take_price), nano=int((take_price - int(take_price)) * 1e9)),
                direction=stop_direction,
                account_id=self.ctx.account_id,
                stop_order_type=StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT,
                expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                exchange_order_type=ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET,
            )
            
            logger.info(f"Placed take profit order for {quantity} lots of {figi} at {take_price}, order_id: {response.stop_order_id}")
            return response.stop_order_id

    def cancel_all_orders(self, figi: str) -> None:
        """Cancel all active orders for instrument"""
        with self._client() as client:
            orders = client.orders.get_orders(account_id=self.ctx.account_id)
            
            for order in orders.orders:
                if order.figi == figi and order.execution_report_status in ["EXECUTION_REPORT_STATUS_NEW", "EXECUTION_REPORT_STATUS_PARTIALLYFILL"]:
                    client.orders.cancel_order(account_id=self.ctx.account_id, order_id=order.order_id)
                    logger.info(f"Cancelled order {order.order_id} for {figi}")

    def cancel_stop_orders(self, figi: str) -> None:
        """Cancel all active stop orders for instrument"""
        with self._client() as client:
            stop_orders = client.stop_orders.get_stop_orders(account_id=self.ctx.account_id)
            
            for stop_order in stop_orders.stop_orders:
                if stop_order.figi == figi:
                    client.stop_orders.cancel_stop_order(account_id=self.ctx.account_id, stop_order_id=stop_order.stop_order_id)
                    logger.info(f"Cancelled stop order {stop_order.stop_order_id} for {figi}")

    def get_current_stop_orders(self, figi: str) -> list[StopOrderInfo]:
        """Get current active stop orders for instrument"""
        with self._client() as client:
            stop_orders = client.stop_orders.get_stop_orders(account_id=self.ctx.account_id)
            
            current_orders = []
            for stop_order in stop_orders.stop_orders:
                if stop_order.figi == figi:
                    order_info = StopOrderInfo(
                        order_id=stop_order.stop_order_id,
                        order_type="stop_loss" if stop_order.order_type == StopOrderType.STOP_ORDER_TYPE_STOP_LOSS else "take_profit",
                        direction="sell" if stop_order.direction == StopOrderDirection.STOP_ORDER_DIRECTION_SELL else "buy",
                        quantity=stop_order.lots_requested,
                        price=float(stop_order.price.units + stop_order.price.nano / 1e9) if stop_order.price else None,
                        stop_price=float(stop_order.stop_price.units + stop_order.stop_price.nano / 1e9) if stop_order.stop_price else None,
                        exchange_order_type="market" if stop_order.exchange_order_type == ExchangeOrderType.EXCHANGE_ORDER_TYPE_MARKET else "limit"
                    )
                    current_orders.append(order_info)
            
            return current_orders

    def _should_update_stop_orders(self, figi: str, stop_price: Optional[float], take_price: Optional[float]) -> bool:
        """Check if stop orders need to be updated based on price changes"""
        current_stop_price = None
        current_take_price = None
        
        # Check if existing stop orders have different prices
        with self._client() as client:
            existing_stop_orders = client.stop_orders.get_stop_orders(account_id=self.ctx.account_id)
            
            for stop_order in existing_stop_orders.stop_orders:
                if stop_order.figi == figi:
                    if stop_order.order_type == StopOrderType.STOP_ORDER_TYPE_STOP_LOSS:
                        current_stop_price = float(stop_order.stop_price.units + stop_order.stop_price.nano / 1e9)
                    elif stop_order.order_type == StopOrderType.STOP_ORDER_TYPE_TAKE_PROFIT:
                        current_take_price = float(stop_order.price.units + stop_order.price.nano / 1e9)
            
        if stop_price != current_stop_price or take_price != current_take_price:
            logger.info(f"Stop orders need to be updated for {figi}: current_stop_price={current_stop_price}, current_take_price={current_take_price}, stop_price={stop_price}, take_price={take_price}")
            return True
            
        return False

    def ensure_position(self, instrument: InstrumentInfo, desired_position: str, leverage_percent: float, reserve_capital: float, stop_price: Optional[float] = None, take_price: Optional[float] = None) -> list[EnsurePositionOrder]:
        """Ensure position matches desired state"""
        figi = instrument.figi

        current_pos = self.get_position(figi)
        current_qty = current_pos.quantity if current_pos else 0
        logger.info(f"Current position info: {current_pos}, desired: {desired_position}")
        
        orders = []
        position_changed = False
        
        if desired_position == "long":
            if current_qty <= 0:
                if current_qty < 0:
                    logger.info(f"Closing short position of {current_qty} lots...")

                    close_qty = abs(current_qty)
                    order_id = self.place_market_order(figi, OrderDirection.ORDER_DIRECTION_BUY, close_qty)
                    orders.append(EnsurePositionOrder(type="buy", quantity=close_qty, order_id=order_id, action="close_short"))
                    position_changed = True
                
                available_long_qty = self.calculate_position_size(instrument, leverage_percent, reserve_capital, "long")
                if available_long_qty > 0:
                    logger.info(f"Opening long position of {available_long_qty} lots...")

                    order_id = self.place_market_order(figi, OrderDirection.ORDER_DIRECTION_BUY, available_long_qty)
                    orders.append(EnsurePositionOrder(type="buy", quantity=available_long_qty, order_id=order_id, action="open_long"))
                    position_changed = True
                else:
                    logger.info(f"No available funds to open long position")
            else:
                logger.info(f"Long position already exists")
                
        elif desired_position == "short":
            if current_qty >= 0:
                if current_qty > 0:
                    logger.info(f"Closing long position of {current_qty} lots...")

                    close_qty = current_qty
                    order_id = self.place_market_order(figi, OrderDirection.ORDER_DIRECTION_SELL, close_qty)
                    orders.append(EnsurePositionOrder(type="sell", quantity=close_qty, order_id=order_id, action="close_long"))
                    position_changed = True
                
                available_short_qty = self.calculate_position_size(instrument, leverage_percent, reserve_capital, "short")
                if available_short_qty > 0:
                    logger.info(f"Opening short position of {available_short_qty} lots...")

                    order_id = self.place_market_order(figi, OrderDirection.ORDER_DIRECTION_SELL, available_short_qty)
                    logger.info(f"Placed order to open short position: {available_short_qty}, order_id: {order_id}")
                    orders.append(EnsurePositionOrder(type="sell", quantity=available_short_qty, order_id=order_id, action="open_short"))
                    position_changed = True
                else:
                    logger.info(f"No available funds to open short position")
            else:
                logger.info(f"Short position already exists")
                
        elif desired_position == "flat":
            if current_qty > 0:
                logger.info(f"Closing long position of {current_qty} lots...")

                order_id = self.place_market_order(figi, OrderDirection.ORDER_DIRECTION_SELL, current_qty)
                orders.append(EnsurePositionOrder(type="sell", quantity=current_qty, order_id=order_id, action="close_long"))
                position_changed = True
            elif current_qty < 0:
                logger.info(f"Closing short position of {current_qty} lots...")

                order_id = self.place_market_order(figi, OrderDirection.ORDER_DIRECTION_BUY, abs(current_qty))
                orders.append(EnsurePositionOrder(type="buy", quantity=abs(current_qty), order_id=order_id, action="close_short"))
                position_changed = True
            else:
                logger.info(f"Position already flat")
        
        # Check if stop orders need to be updated
        should_update_stop_orders = self._should_update_stop_orders(figi, stop_price, take_price)

        if position_changed or should_update_stop_orders:
            logger.info(f"Updating stop orders...")

            # Cancel existing stop orders only if we need to update them
            self.cancel_stop_orders(figi)
            
            # Place new stop loss and take profit if specified and we have a position
            # Get current position after all changes to determine quantity for stop orders
            final_pos = self.get_position(figi)
            final_qty = final_pos.quantity if final_pos else 0

            logger.info(f"Final position quantity: {final_qty}")
            
            # Long position
            if final_qty > 0:
                if stop_price:
                    stop_order_id = self.place_stop_order(figi, OrderDirection.ORDER_DIRECTION_SELL, final_qty, stop_price)
                    orders.append(EnsurePositionOrder(type="stop_loss", quantity=final_qty, price=stop_price, order_id=stop_order_id))
                if take_price:
                    take_order_id = self.place_take_profit_order(figi, OrderDirection.ORDER_DIRECTION_SELL, final_qty, take_price)
                    orders.append(EnsurePositionOrder(type="take_profit", quantity=final_qty, price=take_price, order_id=take_order_id))
            # Short position
            elif final_qty < 0:
                if stop_price:
                    stop_order_id = self.place_stop_order(figi, OrderDirection.ORDER_DIRECTION_BUY, abs(final_qty), stop_price)
                    orders.append(EnsurePositionOrder(type="stop_loss", quantity=abs(final_qty), price=stop_price, order_id=stop_order_id))
                if take_price:
                    take_order_id = self.place_take_profit_order(figi, OrderDirection.ORDER_DIRECTION_BUY, abs(final_qty), take_price)
                    orders.append(EnsurePositionOrder(type="take_profit", quantity=abs(final_qty), price=take_price, order_id=take_order_id))
            # Flat position
            else:
                logger.info("Stop orders are not needed for flat position")
        else:
            logger.info("Stop orders are not needed to be updated")
        
        return orders
    
    def pull_ensure_orders_state(self, instrument_type: str, ensure_orders: list[EnsurePositionOrder]) -> list[EnsurePositionOrder]:
        if instrument_type in ["futures", "bonds"]:
            price_type = PriceType.PRICE_TYPE_POINT
        else:
            price_type = PriceType.PRICE_TYPE_CURRENCY

        for ensure_order in ensure_orders:
            if ensure_order.type in ["buy", "sell"]:
                order_state = self.get_ensure_order_state(ensure_order.order_id, price_type)
                ensure_order.state = order_state

        return ensure_orders

    def get_ensure_order_state(self, order_id: str, price_type: PriceType = PriceType.PRICE_TYPE_UNSPECIFIED) -> EnsurePositionOrderState:
        with self._client() as client:
            order_state = client.orders.get_order_state(account_id=self.ctx.account_id, order_id=order_id, price_type=price_type)
            
            order_date = order_state.order_date
            order_price = float(order_state.average_position_price.units + order_state.average_position_price.nano / 1e9)
            
            return EnsurePositionOrderState(
                date=order_date,
                price=order_price
            )

    # def get_ensure_order_state_waiting_for_price(self, order_id: str, price_type: PriceType = PriceType.PRICE_TYPE_UNSPECIFIED, max_attempts: int = 10, delay_ms: int = 500) -> EnsurePositionOrderState:
    #     for attempt in range(max_attempts):
    #         order_state = self.get_ensure_order_state(order_id, price_type)

    #         # Return order state, if it's price is ready
    #         if order_state.price != 0:
    #             return order_state
            
    #         logger.info(f"Waiting for order state {order_id} price calculation (attempt {attempt + 1}/{max_attempts})")
    #         time.sleep(delay_ms / 1000.0)
            
    #     raise TimeoutError(f"Order state price calculation timeout after {max_attempts} attempts for id {order_id}")
