import logging
import threading
from flask import g

# Thread-local storage for signal context
_thread_context = threading.local()


def _get_context_id() -> str:
    """Get current request ID from Flask g object or thread-local context."""

    try:
        flask_request_id = getattr(g, "_request_id", None)
        if flask_request_id:
            return flask_request_id
    # Working outside of Flask application context
    except RuntimeError:
        pass
    
    # Try thread-local signal context
    thread_id = getattr(_thread_context, 'thread_id', None)
    if thread_id:
        return thread_id
    
    return "no-context"


class ContextAwareLogger:
    """Logger wrapper that automatically includes context ID in log messages."""
    
    def __init__(self, logger: logging.Logger):
        self._logger = logger
    
    def _get_context_id(self) -> str:
        return _get_context_id()
    
    def _format_message(self, message: str) -> str:
        """Format message with context ID prefix."""
        context_id = self._get_context_id()
        return f"[{context_id}] {message}"
    
    def debug(self, message: str, *args, **kwargs):
        self._logger.debug(self._format_message(message), *args, **kwargs)
    
    def info(self, message: str, *args, **kwargs):
        self._logger.info(self._format_message(message), *args, **kwargs)
    
    def warning(self, message: str, *args, **kwargs):
        self._logger.warning(self._format_message(message), *args, **kwargs)
    
    def error(self, message: str, *args, **kwargs):
        self._logger.error(self._format_message(message), *args, **kwargs)
    
    def critical(self, message: str, *args, **kwargs):
        self._logger.critical(self._format_message(message), *args, **kwargs)
    
    def exception(self, message: str, *args, **kwargs):
        self._logger.exception(self._format_message(message), *args, **kwargs)
    
    def isEnabledFor(self, level):
        """Check if logger is enabled for given level."""
        return self._logger.isEnabledFor(level)
    
    def setLevel(self, level):
        """Set logging level."""
        self._logger.setLevel(level)
    
    def getEffectiveLevel(self):
        """Get effective logging level."""
        return self._logger.getEffectiveLevel()


class ContextAwareFormatter(logging.Formatter):
    """Custom formatter that adds context_id to log messages."""
    
    def format(self, record):
        # Use the same logic as ContextAwareLogger
        context_id = _get_context_id()
        
        # Add context_id to the message
        original_msg = record.getMessage()
        record.msg = f"[{context_id}] {original_msg}"
        record.args = ()
        
        return super().format(record)


def get_logger(name: str) -> ContextAwareLogger:
    """Get a context-aware logger for the given name."""
    return ContextAwareLogger(logging.getLogger(name))


def setup_context_aware_logging(logger_name: str, level: int = logging.root.level, 
                                format_string: str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'):
    """Configure any logger to use our format with contex_id."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    
    # Apply the custom formatter
    formatter = ContextAwareFormatter(format_string)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    # Prevent duplicate logs
    logger.propagate = False


def set_thread_context(thread_id: str):
    """Set context id for current thread"""
    _thread_context.thread_id = thread_id
