import logging
import time
import uuid
from http import HTTPStatus

from flask import Blueprint, jsonify, request, g
from werkzeug.exceptions import HTTPException
from pydantic import ValidationError

from app import create_app
from app.logger import get_logger

logger = get_logger(__name__)

api_bp = Blueprint("api", __name__)


@api_bp.route("/healthz", methods=["GET"])
def health() -> tuple[dict, int]:
    return {"status": "ok"}, HTTPStatus.OK.value


def init_app():
    app = create_app()
    
    # Global request/response debug logging
    @app.before_request
    def _log_request_payload():
        g._start_time = time.time()
        g._request_id = str(uuid.uuid4())[:8]  # Short request ID for logs
        
        if logger.isEnabledFor(logging.DEBUG):
            body_repr = None
            # Prefer JSON if available, otherwise raw body as text
            try:
                json_payload = request.get_json(silent=True)
            except Exception:
                json_payload = None
            if json_payload is not None:
                body_repr = json_payload
            else:
                try:
                    body_repr = request.get_data(cache=True, as_text=True)
                except Exception:
                    body_repr = '<unreadable body>'
            logger.debug(
                f"Incoming request: {request.method} {request.path} body={body_repr}"
            )

    @app.after_request
    def _log_response_payload(response):
        if logger.isEnabledFor(logging.DEBUG):
            duration_ms = None
            try:
                if hasattr(g, "_start_time"):
                    duration_ms = int((time.time() - g._start_time) * 1000)
            except Exception:
                duration_ms = None

            # Try to log JSON body, otherwise raw body as text
            try:
                body_json = response.get_json(silent=True)
            except Exception:
                body_json = None

            if body_json is not None:
                body_repr = body_json
            else:
                try:
                    body_repr = response.get_data(cache=False, as_text=True)
                except Exception:
                    body_repr = '<unreadable body>'

            logger.debug(
                f"Response: status={response.status_code} duration_ms={duration_ms} body={body_repr}"
            )
        return response
    
    # Handle HTTP exceptions first (404, 400, 405, etc.)
    @app.errorhandler(HTTPException)
    def handle_http_exception(e):
        logger.warning(f"HTTP error: {e.code} - {HTTPStatus(e.code).phrase}")
        return jsonify({
            "error": "HTTP error",
            "code": e.code,
            "details": HTTPStatus(e.code).phrase,
        }), e.code
    
    
    # Handle validation errors
    @app.errorhandler(ValidationError)
    def handle_validation_error(e):
        logger.warning(f"Validation error: {e}")
        
        details = []
        for err in e.errors():
            path = ".".join(map(str, err.get("loc", []))) or "unknown"
            msg = err.get("msg", "Invalid value")
            details.append({"path": path, "message": msg})

        return jsonify({
            "error": "Validation error",
            "details": details,
        }), HTTPStatus.UNPROCESSABLE_ENTITY.value
    
    # Handle all other exceptions
    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), HTTPStatus.INTERNAL_SERVER_ERROR.value
    
    app.register_blueprint(api_bp)
    
    # Graceful shutdown handler
    @app.teardown_appcontext
    def shutdown_signal_queue(error):
        if error:
            logger.error(f"App context error: {error}")
    
    return app


app = init_app()
