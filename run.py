from __future__ import annotations

import logging
import os
import signal
import sys

from app.config import load_config
from app.logger import setup_context_aware_logging
from app.server import app
from app.routes import init_routes

CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.getcwd(), "config.yml"))

# Load configuration
config = load_config(CONFIG_PATH)

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.server.log_level.upper()),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Configure external library logging to use our format with request_id
setup_context_aware_logging('FinamPy', level=logging.WARNING)

app.register_blueprint(init_routes(CONFIG_PATH))

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger = logging.getLogger(__name__)
    logger.info(f"Received signal {signum}, shutting down gracefully...")
    sys.exit(0)

if __name__ == "__main__":
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
    
    logger = logging.getLogger(__name__)
    logger.info("Starting Trading Bot server...")
    
    # This is only used for development with Flask dev server
    # In production, use gunicorn: gunicorn -w 4 -b 0.0.0.0:8000 run:app
    try:
        app.run(
            host="127.0.0.1",
            port=8000,
            debug=True
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)
