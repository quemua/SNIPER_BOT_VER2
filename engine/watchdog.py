"""
Endgame Sniper Bot - Watchdog
Monitors threads and engine health, auto-restarts if stalled.
"""
import time
import threading
import logging
import sys
from typing import Dict, Callable, Optional


class Watchdog:
    """
    Monitors the health of the sniper engine.

    Features:
    - Thread liveness monitoring
    - Stall detection (no progress)
    - Auto-restart on failure
    - Telegram notification on issues
    """

    def __init__(
        self,
        check_interval: float = 60.0,
        stall_threshold: float = 300.0,  # 5 minutes
        max_restarts: int = 3,
        restart_cooldown: float = 600.0  # 10 minutes
    ):
        self.check_interval = check_interval
        self.stall_threshold = stall_threshold
        self.max_restarts = max_restarts
        self.restart_cooldown = restart_cooldown

        self._thread: Optional[threading.Thread] = None
        self._running = False

        # Track restarts
        self._restart_count = 0
        self._last_restart_time = 0.0

        # Heartbeat tracking
        self._heartbeats: Dict[str, float] = {}
        self._heartbeat_lock = threading.Lock()

        # Callbacks
        self._on_stall: Optional[Callable[[str], None]] = None
        self._on_restart: Optional[Callable[[], None]] = None

    def set_callbacks(
        self,
        on_stall: Callable[[str], None] = None,
        on_restart: Callable[[], None] = None
    ):
        """Set callback functions"""
        self._on_stall = on_stall
        self._on_restart = on_restart

    def heartbeat(self, component: str):
        """Record a heartbeat from a component"""
        with self._heartbeat_lock:
            self._heartbeats[component] = time.time()

    def start(self):
        """Start watchdog monitoring"""
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="Watchdog"
        )
        self._thread.start()
        logging.info("Watchdog started")

    def stop(self):
        """Stop watchdog"""
        self._running = False
        logging.info("Watchdog stopped")

    def _monitor_loop(self):
        """Main monitoring loop"""
        while self._running:
            try:
                self._check_threads()
                self._check_heartbeats()
                time.sleep(self.check_interval)
            except Exception as e:
                logging.error(f"Watchdog error: {e}")
                time.sleep(self.check_interval)

    def _check_threads(self):
        """Check if critical threads are alive"""
        from engine.sniper_engine import get_sniper_engine

        try:
            engine = get_sniper_engine()
            if not engine.is_running():
                return

            dead_threads = []

            # Check market workers
            for symbol, worker in engine._workers.items():
                if worker and not worker.is_alive():
                    dead_threads.append(f"Sniper-{symbol}")

            # Check fill monitor
            if engine._fill_monitor_thread and not engine._fill_monitor_thread.is_alive():
                dead_threads.append("FillMonitor")

            # Check settlement watcher
            if engine._settlement_thread and not engine._settlement_thread.is_alive():
                dead_threads.append("SettlementWatcher")

            if dead_threads:
                self._handle_dead_threads(dead_threads)

        except Exception as e:
            logging.error(f"Thread check error: {e}")

    def _check_heartbeats(self):
        """Check for stalled components"""
        now = time.time()
        stalled = []

        with self._heartbeat_lock:
            for component, last_beat in self._heartbeats.items():
                if now - last_beat > self.stall_threshold:
                    stalled.append(component)

        for component in stalled:
            self._handle_stall(component)

    def _handle_dead_threads(self, dead_threads: list):
        """Handle dead threads"""
        logging.error(f"Dead threads detected: {dead_threads}")

        # Notify
        if self._on_stall:
            self._on_stall(f"Dead threads: {', '.join(dead_threads)}")

        # Try to restart
        self._try_restart()

    def _handle_stall(self, component: str):
        """Handle stalled component"""
        logging.error(f"Component stalled: {component}")

        # Clear stale heartbeat
        with self._heartbeat_lock:
            if component in self._heartbeats:
                del self._heartbeats[component]

        # Notify
        if self._on_stall:
            self._on_stall(f"Stalled: {component}")

        # Try to restart
        self._try_restart()

    def _try_restart(self):
        """Try to restart the engine"""
        now = time.time()

        # Check cooldown
        if now - self._last_restart_time < self.restart_cooldown:
            logging.warning("Restart skipped - cooldown active")
            return

        # Check max restarts
        if self._restart_count >= self.max_restarts:
            logging.error(f"Max restarts ({self.max_restarts}) reached!")
            self._notify_critical("Max restarts reached - manual intervention needed")
            return

        logging.warning("Attempting engine restart...")
        self._restart_count += 1
        self._last_restart_time = now

        try:
            from engine.sniper_engine import stop_engine, start_engine

            # Stop
            stop_engine()
            time.sleep(2)

            # Start
            start_engine()

            logging.info("Engine restarted successfully")

            if self._on_restart:
                self._on_restart()

        except Exception as e:
            logging.error(f"Restart failed: {e}")
            self._notify_critical(f"Restart failed: {e}")

    def _notify_critical(self, message: str):
        """Send critical notification"""
        try:
            from engine.notifier import get_notifier
            notifier = get_notifier()
            notifier.notify_error("Watchdog Critical", message)
        except Exception:
            pass

    def get_status(self) -> dict:
        """Get watchdog status"""
        with self._heartbeat_lock:
            return {
                "running": self._running,
                "restart_count": self._restart_count,
                "last_restart": self._last_restart_time,
                "heartbeats": dict(self._heartbeats),
            }


# ==================== GLOBAL INSTANCE ====================
_watchdog: Optional[Watchdog] = None
_watchdog_lock = threading.Lock()


def get_watchdog() -> Watchdog:
    """Get or create watchdog"""
    global _watchdog
    with _watchdog_lock:
        if _watchdog is None:
            _watchdog = Watchdog()
        return _watchdog


def start_watchdog():
    """Start the watchdog"""
    watchdog = get_watchdog()

    # Setup callbacks
    from engine.notifier import get_notifier

    def on_stall(message: str):
        get_notifier().notify_error("Watchdog", message)

    def on_restart():
        get_notifier().notify_error("Watchdog", "Engine auto-restarted")

    watchdog.set_callbacks(on_stall=on_stall, on_restart=on_restart)
    watchdog.start()


def stop_watchdog():
    """Stop the watchdog"""
    if _watchdog:
        _watchdog.stop()
