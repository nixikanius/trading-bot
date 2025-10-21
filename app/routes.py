from __future__ import annotations

import atexit
import uuid
from http import HTTPStatus
from flask import Blueprint, jsonify, request

from app.config import AppConfig, AccountConfig, load_config
from app.schemas import Signal
from app.signal_queue import SignalQueue
from app.logger import get_logger

logger = get_logger(__name__)

signals_bp = Blueprint("signals", __name__)

_app_config: AppConfig | None = None
_accounts: dict[str, AccountConfig] = {}
_signal_queue: SignalQueue | None = None


def init_routes(config_path: str) -> Blueprint:
    global _app_config, _accounts, _signal_queue
    _app_config = load_config(config_path)
    _accounts = dict(_app_config.accounts)
    _signal_queue = SignalQueue(_accounts, _app_config.telegram)

    @signals_bp.post("/signals/enqueue/<account>")
    def handle_signal(account: str):
        if account not in _accounts:
            return jsonify({"error": "unknown account", "account": account}), HTTPStatus.NOT_FOUND.value

        # Parse and validate signal
        payload = request.get_json(silent=True) or {}
        signal = Signal(**payload)

        # Enqueue signal for async processing
        signal_id = _signal_queue.enqueue_signal(signal, account)
        logger.info(f"Signal {signal_id} enqueued for async processing")

        # Return 202 Accepted immediately
        return jsonify({
            "status": "accepted",
            "message": "Signal queued for processing",
            "account": account,
            "signal": signal.model_dump(mode='json')
        }), HTTPStatus.ACCEPTED.value

    @signals_bp.get("/signals/queue")
    def get_queue_status():
        """Get current signal queue status"""
        signals = _signal_queue.get_queue_items()
        return jsonify({
            "signals": signals
        }), HTTPStatus.OK.value

    # Register graceful shutdown
    atexit.register(lambda: _signal_queue.stop_processing())
    
    return signals_bp
