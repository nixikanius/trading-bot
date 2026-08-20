from __future__ import annotations

import requests
from app.config import TelegramConfig
from app.logger import get_logger
from app.utils import format_duration, format_price

logger = get_logger(__name__)


class TelegramService:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.base_url = f"https://api.telegram.org/bot{config.bot_token}"

    def send_message(self, text: str) -> bool:
        """Send message to Telegram chat"""
        try:
            url = f"{self.base_url}/sendMessage"
            payload = {
                "chat_id": self.config.chat_id,
                "text": text,
                "parse_mode": "HTML"
            }
            
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            logger.info(f"Telegram message sent successfully")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send Telegram message: {e}")
            if hasattr(e, 'response') and e.response:
                try:
                    logger.error(f"Telegram API error: {e.response.json()}")
                except:
                    logger.error(f"Telegram API response: {e.response.text}")
            
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return False
        
        return True

    def format_signal_result(self, account: str, signal: dict, result: dict, instrument_info) -> str:
        """Format signal processing result for Telegram"""
        stop_orders = result["stop_orders"]

        def format_instrument_price(value: float) -> str:
            return format_price(value, instrument_info.min_price_step)
        
        position_emoji = "⬆️" if signal['position'] == 'long' else "⬇️" if signal['position'] == 'short' else "➖"

        message = f"🛎️ <b>Trading Signal</b>\n\n"
        message += f"<i>{account}</i>\n"
        message += f"{signal['instrument']}: {position_emoji} <b>{signal['position'].upper()}</b>\n"

        # Signal entry data
        signal_entry_data = []
        if value := signal.get('entry_price'):
            signal_entry_data.append(format_instrument_price(value))
        if value := signal.get('entry_time'):
            delay = signal['timestamp'] - value
            entry_time = f"{value} (+ {format_duration(delay)})"
            signal_entry_data.append(entry_time)
        
        if signal_entry_data:
            message += f"▶️ {' @ '.join(signal_entry_data)}\n"
        
        if init_position := result.get('init_position'):
            message += f"\n◉ <b>Initial Position:</b> <b>{init_position.quantity}</b> lots @ <b>{format_instrument_price(init_position.average_price)}</b>\n"
        else:
            message += f"\n◉ <b>Initial Position:</b> None\n"
        
        # Add placed orders if any
        if ensure_orders := result.get('ensure_orders'):
            message += "\n🔄 <b>Orders Placed</b>\n"
            for order in ensure_orders:
                if order.type in ['buy', 'sell']:
                    order_emoji = "⬆️" if order.type == 'buy' else "⬇️"
                    order_message = f"{order_emoji} {order.type.upper()} {order.quantity} lots @ {format_instrument_price(order.result.price)} ({order.action})"

                    order_slippage = result['slippage'].get(order.order_id, {})
                    
                    order_slippage_data = []
                    if (value := order_slippage.get('price')) is not None:
                        order_slippage_data.append(format_instrument_price(value))
                    if (value := order_slippage.get('time')) is not None:
                        order_slippage_data.append(format_duration(value))

                    if order_slippage_data:
                        order_message += f", slp. {' @ '.join(order_slippage_data)}"

                    message += f"{order_message}\n"
                elif order.type == 'stop_loss':
                    message += f"⛔ SL: {order.quantity} lots @ {format_instrument_price(order.price)}\n"
                elif order.type == 'take_profit':
                    message += f"🎯 TP: {order.quantity} lots @ {format_instrument_price(order.price)}\n"
        
        if result.get('profit') is not None:
            profit_emoji = "🟢" if result['profit'] >= 0 else "🔴"
            message += f"\n💰 <b>Profit</b>: {profit_emoji} <b>{result['profit']}</b>\n"

        # Add position info
        if position := result.get('position'):
            message += f"\n● <b>Current Position:</b> <b>{position.quantity}</b> lots @ <b>{format_instrument_price(position.average_price)}</b>\n"
        else:
            message += f"\n● <b>Current Position:</b> None\n"
        
        # Add stop orders
        if stop_orders:
            message += "\n⏳ <b>Stop Orders</b>\n"

            for order in sorted(stop_orders, key=lambda x: x.order_type):
                order_type = "⛔ SL" if order.order_type == 'stop_loss' else "🎯 TP"
                action = f"⬆️ {order.direction.upper()}" if order.direction == 'buy' else f"⬇️ {order.direction.upper()}"
                
                message += f"{order_type}: {action} {order.quantity} lots @ <b>{format_instrument_price(order.stop_price)}</b> ({order.exchange_order_type[0].upper()})\n"
        
        return message
