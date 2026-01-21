"""
Endgame Sniper Bot - Logging Configuration
Production-grade logging with:
- Rotating file logs (10MB x 10 files)
- Event-based logging (no tick spam)
- Separate configs for Headless and UI modes
- Suppressed noisy libraries
"""
import os
import sys
import logging
import logging.handlers
import queue
from typing import Optional


# ==================== CONSTANTS ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(BASE_DIR, "sniper.log")

# Rotating log settings (10MB x 10 files)
MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 10

# Noisy libraries to suppress
NOISY_LIBS = [
    "urllib3",
    "requests",
    "websockets",
    "asyncio",
    "http.client",
    "httpx",
    "httpcore",
    "py_clob_client",
    "websocket",
    "charset_normalizer",
]


class QueueHandler(logging.Handler):
    """Handler that puts log records into a queue for UI consumption"""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_queue.put_nowait(msg)
        except queue.Full:
            pass  # Don't block if queue is full
        except Exception:
            self.handleError(record)


class EventThrottler:
    """
    Throttle repeated log messages to prevent spam.
    Event-based logging for production.
    """

    def __init__(self, cooldown_seconds: float = 10.0):
        self.cooldown = cooldown_seconds
        self._last_logs: dict = {}

    def should_log(self, key: str) -> bool:
        """Check if message with key should be logged"""
        import time
        now = time.time()
        last = self._last_logs.get(key, 0)
        if now - last > self.cooldown:
            self._last_logs[key] = now
            return True
        return False

    def cleanup(self, max_age: float = 300.0):
        """Clean up old entries"""
        import time
        now = time.time()
        old_keys = [k for k, t in self._last_logs.items() if now - t > max_age]
        for k in old_keys:
            del self._last_logs[k]


# Global throttler instance
_throttler = EventThrottler()


def log_throttled(key: str, message: str, level: int = logging.ERROR, cooldown: float = 10.0):
    """Log a message with throttling to prevent spam"""
    if _throttler.should_log(key):
        logging.log(level, f"[{key}] {message}")


def setup_headless_logging(log_level: str = "INFO") -> logging.Logger:
    """
    Setup logging for Headless (VPS) mode.
    - Rotating file handler (10MB x 10)
    - No UI queue handler
    - Event-based (no tick spam)
    - Suppressed noisy libraries
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear existing handlers
    root_logger.handlers = []

    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # File handler with rotation (10MB x 10 files)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler (stderr) - minimal output
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.WARNING)  # Only warnings and above to console
    root_logger.addHandler(console_handler)

    # Suppress noisy libraries
    for lib in NOISY_LIBS:
        logging.getLogger(lib).setLevel(logging.CRITICAL)

    logging.info("Headless logging initialized")
    return root_logger


def setup_ui_logging(
    log_queue: queue.Queue,
    log_level: str = "INFO"
) -> logging.Logger:
    """
    Setup logging for UI mode.
    - Rotating file handler (10MB x 10)
    - Queue handler for UI display
    - Event-based (no tick spam)
    - Suppressed noisy libraries
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Clear existing handlers
    root_logger.handlers = []

    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Queue handler for UI
    queue_handler = QueueHandler(log_queue)
    queue_handler.setFormatter(formatter)
    root_logger.addHandler(queue_handler)

    # Suppress noisy libraries
    for lib in NOISY_LIBS:
        logging.getLogger(lib).setLevel(logging.CRITICAL)

    logging.info("UI logging initialized")
    return root_logger


def setup_logging(
    mode: str = "headless",
    log_queue: Optional[queue.Queue] = None,
    log_level: str = "INFO"
) -> logging.Logger:
    """
    Setup logging based on mode.

    Args:
        mode: "headless" or "ui"
        log_queue: Queue for UI mode (required if mode="ui")
        log_level: Log level string
    """
    if mode.lower() == "ui" and log_queue:
        return setup_ui_logging(log_queue, log_level)
    else:
        return setup_headless_logging(log_level)


def get_throttler() -> EventThrottler:
    """Get global event throttler"""
    return _throttler
