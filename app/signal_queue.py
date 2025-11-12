from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.schemas import Signal
from app.logger import get_logger, set_thread_context
from app.config import AccountConfig, TelegramConfig
from app.signal_service import SignalService
from app.brokers import TradingError
from app.telegram_service import TelegramService
from tinkoff.invest.exceptions import RequestError

logger = get_logger(__name__)


@dataclass
class QueuedSignal:
    """Signal queued for processing"""
    key: str
    signal: Signal
    account: str
    enqueue_time: datetime
    processing_start_time: Optional[datetime] = None
    processing_end_time: Optional[datetime] = None

class SignalQueue:
    """In-memory signal queue with async processing"""
    
    def __init__(self, account_configs: Dict[str, AccountConfig], telegram_config: TelegramConfig):
        self._processing: Dict[str, QueuedSignal] = {}  # key: f"{account}/{instrument}" -> currently processing signal
        self._waiting: Dict[str, QueuedSignal] = {}  # key: f"{account}/{instrument}" -> waiting signal
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="signal_worker")

        self._telegram_service = TelegramService(telegram_config)
        
        # Create signal services for all accounts
        self._signal_services: Dict[str, SignalService] = {}
        for account_name, account_config in account_configs.items():
            self._signal_services[account_name] = SignalService(
                account_name, 
                account_config, 
                self._telegram_service
            )
    
    def enqueue_signal(self, signal: Signal, account: str) -> str:
        """
        Enqueue a signal for asynchronous processing.
        
        This method implements a sophisticated queuing system that ensures:
        - Only one signal can be processed at a time per account+instrument combination
        - New signals wait in queue if another signal is currently being processed
        - New signals replace any waiting signals (but never interrupt processing ones)
        - Processing signals always complete fully before the next signal starts
            
        Queue States:
            - _processing: Currently executing signal (max 1 per key)
            - _waiting: Next signal to execute (max 1 per key)
        """
        
        key = f"{account}/{signal.instrument}"
        signal_id = signal.signal_id
        
        with self._lock:
            queued_signal = QueuedSignal(
                key=key,
                signal=signal,
                account=account,
                enqueue_time=datetime.now()
            )
            
            if waiting_signal := self._waiting.get(key):
                logger.info(f"Replacing waiting signal {waiting_signal.signal.signal_id} for {key} with new signal {signal_id}")
            else:
                logger.info(f"Signal {signal_id} added waiting execution for {key}")
            
            self._waiting[key] = queued_signal

            if processing_signal := self._processing.get(key):
                logger.info(f"Signal {signal_id} queued as next for {key} (current {processing_signal.signal.signal_id} is processing)")
                trigger_processing = False
            else:
                logger.info(f"Signal {signal_id} triggered processing for {key}")
                trigger_processing = True
        
        # Start processing this signal (outside of lock) only if needed
        if trigger_processing:
            self._executor.submit(self._process_waiting_signal_key, key)
        
        return signal_id
    
    def _process_waiting_signal_key(self, key: str):
        """Process a signal for given key"""
        with self._lock:
            queued_signal = self._waiting[key]
            
            del self._waiting[key]
            self._processing[key] = queued_signal

        self._process_queued_signal(queued_signal)
        
        # Handle queue management after processing
        with self._lock:
            del self._processing[key]

            if waiting_signal := self._waiting.get(key):
                logger.info(f"Signal {waiting_signal.signal.signal_id} triggered processing as next for {key}")
                trigger_processing = True
            else:
                logger.info(f"No more signals waiting for {key}")
                trigger_processing = False
        
        if trigger_processing:
            self._process_waiting_signal_key(key)

    def _process_queued_signal(self, queued_signal: QueuedSignal):
        """Execute the actual signal processing logic"""
        # Set thread context for logging
        set_thread_context(f"signal-{queued_signal.signal.signal_id}")
        queued_signal.processing_start_time = datetime.now()

        try:
            logger.info(f"Processing signal for {queued_signal.key}...")
            
            signal_service = self._signal_services[queued_signal.account]
            result = signal_service.process_signal(queued_signal.signal)

            logger.info(f"Processed signal for {queued_signal.key}: {result}")
        except RequestError as e:
            # Handle Tinkoff trading errors
            code = e.code.name if e.code else "UNKNOWN"
            message = getattr(e.metadata, "message", None) if e.metadata else None
            message = message or e.details or "Trading request error"
            
            logger.error(f"Trading request error for {queued_signal.key}: {code} - {message}", exc_info=True)
            self._send_error_notification(queued_signal, f"Trading Request Error: {code}", message)
        except TradingError as e:
            # Handle custom trading errors
            logger.error(f"Trading error for {queued_signal.key}: {e.code} - {e}", exc_info=True)            
            self._send_error_notification(queued_signal, f"Trading Error: {e.code}", str(e))
        except Exception as e:
            # Handle all other errors
            logger.error(f"Unexpected error processing signal for {queued_signal.key}: {e}", exc_info=True)
            self._send_error_notification(queued_signal, "Signal Processing Error", str(e))
        
        queued_signal.processing_end_time = datetime.now()
        queue_duration_s = (queued_signal.processing_start_time - queued_signal.enqueue_time).total_seconds()
        processing_duration_s = (queued_signal.processing_end_time - queued_signal.processing_start_time).total_seconds()
        total_duration_s = (queued_signal.processing_end_time - queued_signal.enqueue_time).total_seconds()

        logger.info(f"Processing time: queue={queue_duration_s:.3f}s, processing={processing_duration_s:.3f}s, total={total_duration_s:.3f}s")
    
    def _send_error_notification(self, queued_signal: QueuedSignal, error: str, details: str):
        """Send error notification to Telegram"""
        try:
            position = queued_signal.signal.position
            position_emoji = "⬆️" if position == 'long' else "⬇️" if position == 'short' else "➖"
            
            message = f"❌ <b>{error}</b>\n\n"
            message += f"<i>{queued_signal.account}</i>\n"
            message += f"{queued_signal.signal.instrument}: {position_emoji} <b>{position.upper()}</b>\n\n"
            message += f"{details}"
            
            self._telegram_service.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")
    
    def get_queue_items(self) -> Dict[str, Dict]:
        """Get current queue items"""
        result: Dict[str, list[Dict]] = {}

        with self._lock:
            for list_name in ['processing', 'waiting']:
                result[list_name] = [
                    {
                        "signal": queued_signal.signal.model_dump(mode='json'),
                        "account": queued_signal.account,
                    } for queued_signal in getattr(self, f"_{list_name}").values()
                ]
            
        return result
    
    def stop_processing(self):
        """Stop background processing"""
        self._executor.shutdown(wait=True)
        logger.info("Stopped signal processing executor")
