"""
Endgame Sniper Bot - Persistence Manager
Safe restart capability with state persistence.

Persists:
- orders_placed: Prevents double orders on restart
- pending_settlements: Continue settlement watching
- fill data: Track fill status
- notify flags: Prevent duplicate notifications
- statistics: Maintain win/loss/PnL tracking
"""
import os
import json
import time
import tempfile
import threading
import logging
from typing import Dict, Any, Optional


# ==================== CONSTANTS ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
STATE_BACKUP_FILE = os.path.join(BASE_DIR, "state.backup.json")

# Auto-save interval (seconds)
AUTO_SAVE_INTERVAL = 60.0


class PersistenceManager:
    """
    Manages persistent state for safe restart.

    Features:
    - Atomic writes (write temp -> rename)
    - Periodic auto-save
    - Backup on save
    - Recovery from backup if main file corrupted
    """

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.backup_file = state_file.replace(".json", ".backup.json")
        self._lock = threading.Lock()
        self._auto_save_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_save_time = 0.0
        self._dirty = False

    def start_auto_save(self, interval: float = AUTO_SAVE_INTERVAL):
        """Start auto-save background thread"""
        if self._auto_save_thread and self._auto_save_thread.is_alive():
            return

        self._running = True
        self._auto_save_thread = threading.Thread(
            target=self._auto_save_worker,
            args=(interval,),
            daemon=True,
            name="Persistence-AutoSave"
        )
        self._auto_save_thread.start()
        logging.debug(f"Auto-save started (interval: {interval}s)")

    def stop_auto_save(self):
        """Stop auto-save thread"""
        self._running = False

    def _auto_save_worker(self, interval: float):
        """Background worker for periodic saves"""
        while self._running:
            try:
                time.sleep(interval)
                if self._dirty:
                    # Get state from state manager and save
                    from engine.state_manager import get_state_manager
                    state_manager = get_state_manager()
                    self.save(state_manager.get_persistent_state())
            except Exception as e:
                logging.error(f"Auto-save error: {e}")

    def mark_dirty(self):
        """Mark state as needing save"""
        self._dirty = True

    def save(self, state: Dict[str, Any]) -> bool:
        """
        Save state to file with atomic write.

        Args:
            state: State dictionary to save

        Returns:
            True on success
        """
        with self._lock:
            try:
                # Add metadata
                state_with_meta = {
                    "_version": 1,
                    "_saved_at": time.time(),
                    "_saved_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "data": state,
                }

                # Backup current file first
                if os.path.exists(self.state_file):
                    try:
                        with open(self.state_file, 'r', encoding='utf-8') as f:
                            current = f.read()
                        with open(self.backup_file, 'w', encoding='utf-8') as f:
                            f.write(current)
                    except Exception as e:
                        logging.debug(f"Backup failed: {e}")

                # Atomic write: temp file -> rename
                dir_path = os.path.dirname(self.state_file)
                with tempfile.NamedTemporaryFile(
                    mode='w',
                    dir=dir_path,
                    suffix='.tmp',
                    delete=False,
                    encoding='utf-8'
                ) as tmp_file:
                    json.dump(state_with_meta, tmp_file, indent=2)
                    tmp_path = tmp_file.name

                os.replace(tmp_path, self.state_file)

                self._last_save_time = time.time()
                self._dirty = False

                logging.debug(f"State saved to {self.state_file}")
                return True

            except Exception as e:
                logging.error(f"State save failed: {e}")
                # Cleanup temp file
                try:
                    if 'tmp_path' in locals():
                        os.unlink(tmp_path)
                except:
                    pass
                return False

    def load(self) -> Optional[Dict[str, Any]]:
        """
        Load state from file.

        Returns:
            State dictionary or None if not found/invalid
        """
        with self._lock:
            # Try main file first
            state = self._try_load_file(self.state_file)
            if state:
                return state

            # Try backup file
            state = self._try_load_file(self.backup_file)
            if state:
                logging.warning(f"Loaded from backup file: {self.backup_file}")
                return state

            logging.info("No existing state file found")
            return None

    def _try_load_file(self, filepath: str) -> Optional[Dict[str, Any]]:
        """Try to load state from a specific file"""
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                state_with_meta = json.load(f)

            # Validate structure
            if not isinstance(state_with_meta, dict):
                logging.warning(f"Invalid state file format: {filepath}")
                return None

            # Extract data (handle both old and new format)
            if "data" in state_with_meta:
                return state_with_meta["data"]
            elif "_version" not in state_with_meta:
                # Legacy format (raw state)
                return state_with_meta

            return None

        except json.JSONDecodeError as e:
            logging.warning(f"JSON decode error in {filepath}: {e}")
            return None
        except Exception as e:
            logging.warning(f"Error loading {filepath}: {e}")
            return None

    def clear(self):
        """Clear persistent state (for testing/reset)"""
        with self._lock:
            try:
                if os.path.exists(self.state_file):
                    os.unlink(self.state_file)
                if os.path.exists(self.backup_file):
                    os.unlink(self.backup_file)
                logging.info("Persistent state cleared")
            except Exception as e:
                logging.error(f"Failed to clear state: {e}")

    def get_state_age(self) -> Optional[float]:
        """Get age of saved state in seconds, or None if no state"""
        with self._lock:
            if not os.path.exists(self.state_file):
                return None

            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                saved_at = state.get("_saved_at", 0)
                if saved_at:
                    return time.time() - saved_at
            except:
                pass

            # Fallback to file modification time
            try:
                return time.time() - os.path.getmtime(self.state_file)
            except:
                return None

    def should_restore(self, max_age_hours: float = 24.0) -> bool:
        """
        Check if state should be restored.

        Args:
            max_age_hours: Maximum age in hours to consider state valid

        Returns:
            True if state exists and is recent enough to restore
        """
        age = self.get_state_age()
        if age is None:
            return False
        return age < (max_age_hours * 3600)


# ==================== GLOBAL INSTANCE ====================
_persistence_manager: Optional[PersistenceManager] = None
_persistence_lock = threading.Lock()


def get_persistence_manager() -> PersistenceManager:
    """Get or create persistence manager"""
    global _persistence_manager
    with _persistence_lock:
        if _persistence_manager is None:
            _persistence_manager = PersistenceManager()
        return _persistence_manager


def save_state_now():
    """Immediately save current state"""
    from engine.state_manager import get_state_manager
    pm = get_persistence_manager()
    sm = get_state_manager()
    pm.save(sm.get_persistent_state())


def restore_state_if_available(max_age_hours: float = 24.0) -> bool:
    """
    Restore state from persistence if available and recent.

    Returns:
        True if state was restored
    """
    from engine.state_manager import get_state_manager
    pm = get_persistence_manager()

    if not pm.should_restore(max_age_hours):
        return False

    state = pm.load()
    if state:
        sm = get_state_manager()
        sm.restore_persistent_state(state)
        logging.info("State restored from persistence")
        return True

    return False
