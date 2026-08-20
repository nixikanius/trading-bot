from datetime import datetime

from app.brokers import EnsureOrder, InstrumentInfo, OrderResult, Position, StopOrder
from app.config import TelegramConfig
from app.schemas import Signal
from app.signal_service import SignalService
from app.telegram_service import TelegramService
from app.utils import format_price


def test_format_signal_result_uses_instrument_price_precision():
    service = TelegramService(TelegramConfig(bot_token="token", chat_id=1))
    instrument = InstrumentInfo(
        instrument="GLDRUBF@RTSX",
        name="Gold",
        type="futures",
        currency="RUB",
        lot_size=1,
        min_price_step=0.1,
    )
    order = EnsureOrder(
        type="buy",
        quantity=180,
        order_id="order-id",
        action="open_long",
        result=OrderResult(date=datetime(2026, 8, 19, 16, 0, 17), price=12039.87111111111),
    )
    result = {
        "init_position": Position("GLDRUBF@RTSX", 0, 0.0),
        "ensure_orders": [
            order,
            EnsureOrder("stop_loss", 180, "sl", price=11895.34),
            EnsureOrder("take_profit", 180, "tp", price=12316.66),
        ],
        "profit": None,
        "slippage": {"order-id": {"price": 0.07111111111, "amount": 390.0}},
        "position": Position("GLDRUBF@RTSX", 180, 12039.87111111111),
        "stop_orders": [
            StopOrder("sl", "stop_loss", "sell", 180, stop_price=11895.34),
            StopOrder("tp", "take_profit", "sell", 180, stop_price=12316.66, exchange_order_type="limit"),
        ],
    }
    signal = {
        "instrument": "GLDRUBF@RTSX",
        "position": "long",
        "entry_price": 12039.8,
    }

    message = service.format_signal_result("account", signal, result, instrument)

    assert "12039.9 (open_long), slp. 0.1 (390.0)" in message
    assert "Initial Position:</b> <b>0</b> lots @ <b>0.0</b>" in message
    assert "SL: 180 lots @ 11895.3" in message
    assert "TP: 180 lots @ 12316.7" in message
    assert "Current Position:</b> <b>180</b> lots @ <b>12039.9</b>" in message
    assert "@ <b>11895.3</b>" in message
    assert "@ <b>12316.7</b>" in message


def test_price_precision_supports_integer_and_multi_decimal_steps():
    assert format_price(12.6, 1) == "13"
    assert format_price(1.23456, 0.001) == "1.235"


def test_slippage_amount_includes_lots_and_contracts_per_lot():
    signal = Signal(position="short", instrument="FUTURE", entry_price=2143.0)
    order = EnsureOrder(
        type="sell",
        quantity=78,
        order_id="order-id",
        action="open_short",
        result=OrderResult(date=datetime(2026, 8, 19), price=2142.5),
    )
    instrument = InstrumentInfo(
        instrument="FUTURE",
        name="Future",
        type="futures",
        currency="RUB",
        lot_size=10,
        min_price_step=0.1,
    )

    slippage = SignalService._calculate_slippage(None, signal, [order], instrument)

    assert slippage["order-id"]["price"] == 0.5
    assert slippage["order-id"]["amount"] == 390.0
