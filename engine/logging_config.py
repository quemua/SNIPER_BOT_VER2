"""
Endgame Sniper Bot - Logging Configuration
Production-grade logging with:
- Rotating file logs (10MB x 10 files)
- Automatic compression of old logs
- Event-based logging (no tick spam)
- Separate configs for Headless and UI modes
- Suppressed noisy libraries
- Disk space monitoring
"""
import os
import sys
import gzip
import shutil
import logging
import logging.handlers
import queue
import threading
from typing import Optional
from pathlib import Path


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

# Max total log size (MB) before warning
MAX_TOTAL_LOG_SIZE_MB = 200


class CompressingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    RotatingFileHandler that compresses rotated files with gzip.
    Saves significant disk space on VPS.
    """

    def __init__(self, *args, compress_delay: float = 5.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.compress_delay = compress_delay
        self._compressor_thread = None

    def doRollover(self):
        """Override to add compression after rotation"""
        # Get the file that will be rotated
        if self.stream:
            self.stream.close()
            self.stream = None

        # Rotate files
        if self.backupCount > 0:
            # Compress the oldest file if it exists (before it gets deleted)
            for i in range(self.backupCount, 0, -1):
                sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
                dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")

                if os.path.exists(sfn):
                    if os.path.exists(dfn):
                        os.remove(dfn)
                    os.rename(sfn, dfn)

                # Check for compressed version too
                sfn_gz = sfn + ".gz"
                dfn_gz = dfn + ".gz"
                if os.path.exists(sfn_gz):
                    if os.path.exists(dfn_gz):
                        os.remove(dfn_gz)
                    os.rename(sfn_gz, dfn_gz)

            dfn = self.rotation_filename(f"{self.baseFilename}.1")
            if os.path.exists(dfn):
                os.remove(dfn)
            self.rotate(self.baseFilename, dfn)

            # Schedule compression of the rotated file
            self._schedule_compress(dfn)

        if not self.delay:
            self.stream = self._open()

    def _schedule_compress(self, filepath: str):
        """Schedule compression in background thread"""
        def compress():
            try:
                import time
                time.sleep(self.compress_delay)

                if os.path.exists(filepath):
                    gz_path = filepath + ".gz"
                    with open(filepath, 'rb') as f_in:
                        with gzip.open(gz_path, 'wb') as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    os.remove(filepath)
            except Exception:
                pass  # Don't crash on compression failure

        thread = threading.Thread(target=compress, daemon=True)
        thread.start()


def check_log_disk_usage() -> dict:
    """Check total log disk usage and warn if too high"""
    log_dir = Path(BASE_DIR)
    total_size = 0
    log_files = []

    for pattern in ["*.log", "*.log.*", "*.log.*.gz"]:
        for f in log_dir.glob(pattern):
            try:
                size = f.stat().st_size
                total_size += size
                log_files.append((str(f), size))
            except OSError:
                pass

    total_mb = total_size / (1024 * 1024)
    warning = total_mb > MAX_TOTAL_LOG_SIZE_MB

    if warning:
        logging.warning(f"Log disk usage high: {total_mb:.1f}MB (limit: {MAX_TOTAL_LOG_SIZE_MB}MB)")

    return {
        "total_bytes": total_size,
        "total_mb": total_mb,
        "warning": warning,
        "files": len(log_files),
    }


def cleanup_old_logs(max_age_days: int = 7):
    """Clean up old compressed log files"""
    import time
    log_dir = Path(BASE_DIR)
    cutoff = time.time() - (max_age_days * 86400)
    cleaned = 0

    for f in log_dir.glob("*.log.*.gz"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                cleaned += 1
        except OSError:
            pass

    if cleaned > 0:
        logging.info(f"Cleaned up {cleaned} old log files")


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
    - Compressing rotating file handler (10MB x 10)
    - No UI queue handler
    - Event-based (no tick spam)
    - Suppressed noisy libraries
    - Disk usage monitoring
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

    # File handler with rotation and compression (10MB x 10 files)
    file_handler = CompressingRotatingFileHandler(
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

    # Check disk usage
    check_log_disk_usage()

    # Cleanup old compressed logs
    cleanup_old_logs(max_age_days=7)

    logging.info("Headless logging initialized (with compression)")
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
