"""
Endgame Sniper Bot - State Manager
Thread-safe state management for both Headless and UI modes

Key responsibilities:
- Maintain runtime state (running, balance, orders, etc.)
- Provide thread-safe access via locks
- Support both headless and UI modes
- Publish state changes for UI updates
"""
import threading
import time
import queue
from enum import Enum
from typing import Dict, Optional, Any, Callable, List
from dataclasses import dataclass, field


class RunMode(Enum):
    """Running mode of the bot"""
    HEADLESS = "headless"
    UI = "ui"


@dataclass
class MarketStatus:
    """Status for a single market"""
    status: str = "Idle"
    last_check: float = 0.0
    current_market: Optional[str] = None
    up_bid: float = 0.0
    down_bid: float = 0.0
    time_to_expiry: float = 0.0
    polling_tier: str = "far"


@dataclass
class SettlementRecord:
    """Record for pending settlement"""
    slug: str
    condition_id: str
    symbol: str
    expiry_ts: float
    token_up: str
    token_down: str
    up_order_id: Optional[str] = None
    down_order_id: Optional[str] = None
    up_filled: float = 0.0
    down_filled: float = 0.0
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0
    up_price_source: str = "sniper"
    down_price_source: str = "sniper"
    up_notified: bool = False
    down_notified: bool = False
    status: str = "pending"  # pending, resolved, redeemed
    winner: Optional[str] = None
    pnl: float = 0.0
    created_at: float = field(default_factory=time.time)


class StateManager:
    """
    Thread-safe state manager for the sniper bot.
    Supports both headless (VPS) and UI (desktop) modes.
    """

    def __init__(self, mode: RunMode = RunMode.HEADLESS):
        self.mode = mode

        # Main lock hierarchy (acquire in this order to avoid deadlock):
        # 1. order_placement_lock (outermost)
        # 2. data_lock (innermost)
        self._data_lock = threading.RLock()
        self._order_placement_lock = threading.Lock()

        # Stop event for graceful shutdown
        self._stop_event = threading.Event()

        # Log queue for UI mode
        self._log_queue: Optional[queue.Queue] = None
        if mode == RunMode.UI:
            self._log_queue = queue.Queue(maxsize=2000)

        # State change callbacks (for UI updates)
        self._state_listeners: List[Callable[[str, Any], None]] = []

        # Initialize state
        self._state = {
            "is_running": False,

            # Balance
            "usdc_balance": 0.0,
            "last_balance_update": 0.0,

            # Per-market status
            "market_status": {},

            # Orders placed tracking
            "orders_placed": {},  # {slug: timestamp}
            "order_ids": {},  # {slug: {"up": id, "down": id}}
            "orders_in_progress": set(),  # Slugs currently being processed

            # Statistics
            "total_orders_placed": 0,
            "total_fills": 0,

            # Error logs (for throttling)
            "error_logs": {},

            # Settlement tracking
            "pending_settlements": {},

            # Win/Loss stats
            "total_wins": 0,
            "total_losses": 0,
            "total_pnl": 0.0,
        }

    @property
    def log_queue(self) -> Optional[queue.Queue]:
        """Get log queue (only available in UI mode)"""
        return self._log_queue

    @property
    def stop_event(self) -> threading.Event:
        """Get stop event"""
        return self._stop_event

    def should_stop(self) -> bool:
        """Check if bot should stop"""
        if self._stop_event.is_set():
            return True
        with self._data_lock:
            return not self._state.get("is_running", False)

    def request_stop(self):
        """Request the bot to stop"""
        self._stop_event.set()
        with self._data_lock:
            self._state["is_running"] = False

    def clear_stop(self):
        """Clear stop request"""
        self._stop_event.clear()

    # ==================== RUNNING STATE ====================

    def set_running(self, running: bool):
        """Set running state"""
        with self._data_lock:
            self._state["is_running"] = running
            self._notify_change("is_running", running)

    def is_running(self) -> bool:
        """Check if bot is running"""
        with self._data_lock:
            return self._state.get("is_running", False)

    # ==================== BALANCE ====================

    def update_balance(self, balance: float):
        """Update USDC balance"""
        with self._data_lock:
            self._state["usdc_balance"] = balance
            self._state["last_balance_update"] = time.time()
            self._notify_change("usdc_balance", balance)

    def get_balance(self) -> float:
        """Get current USDC balance"""
        with self._data_lock:
            return self._state.get("usdc_balance", 0.0)

    # ==================== MARKET STATUS ====================

    def init_market_status(self, symbols: List[str]):
        """Initialize market status for all symbols"""
        with self._data_lock:
            for symbol in symbols:
                if symbol not in self._state["market_status"]:
                    self._state["market_status"][symbol] = MarketStatus()

    def set_market_status(self, symbol: str, key: str, value: Any):
        """Set a market status field"""
        with self._data_lock:
            if symbol not in self._state["market_status"]:
                self._state["market_status"][symbol] = MarketStatus()

            status = self._state["market_status"][symbol]
            if hasattr(status, key):
                setattr(status, key, value)

            self._notify_change(f"market_status.{symbol}.{key}", value)

    def get_market_status(self, symbol: str) -> Dict[str, Any]:
        """Get market status as dict"""
        with self._data_lock:
            status = self._state["market_status"].get(symbol, MarketStatus())
            return {
                "status": status.status,
                "last_check": status.last_check,
                "current_market": status.current_market,
                "up_bid": status.up_bid,
                "down_bid": status.down_bid,
                "time_to_expiry": status.time_to_expiry,
                "polling_tier": status.polling_tier,
            }

    # ==================== ORDER TRACKING ====================

    def try_acquire_order_slot(self, slug: str) -> bool:
        """
        Atomically try to acquire exclusive right to place orders for a market.
        Returns True if acquired, False if already taken.
        """
        with self._order_placement_lock:
            if slug in self._state["orders_placed"]:
                return False
            if slug in self._state["orders_in_progress"]:
                return False
            self._state["orders_in_progress"].add(slug)
            return True

    def release_order_slot(self, slug: str, success: bool = False):
        """Release order slot after attempt"""
        with self._order_placement_lock:
            self._state["orders_in_progress"].discard(slug)

    def record_order_placed(
        self,
        slug: str,
        up_order_id: Optional[str],
        down_order_id: Optional[str]
    ):
        """Record that orders were placed for a market"""
        with self._order_placement_lock:
            self._state["orders_in_progress"].discard(slug)

            with self._data_lock:
                self._state["orders_placed"][slug] = time.time()
                self._state["order_ids"][slug] = {
                    "up": up_order_id,
                    "down": down_order_id
                }

                if up_order_id:
                    self._state["total_orders_placed"] += 1
                if down_order_id:
                    self._state["total_orders_placed"] += 1

                self._notify_change("total_orders_placed", self._state["total_orders_placed"])

    def is_order_placed_for_market(self, slug: str) -> bool:
        """Check if orders already placed for this market"""
        with self._data_lock:
            return slug in self._state["orders_placed"]

    def get_order_ids(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Get copy of all order IDs"""
        with self._data_lock:
            return dict(self._state.get("order_ids", {}))

    def cleanup_old_orders(self, max_age_seconds: float = 3600):
        """Clean up old order records"""
        now = time.time()
        cutoff = now - max_age_seconds

        with self._order_placement_lock:
            with self._data_lock:
                old_slugs = [
                    slug for slug, ts in self._state["orders_placed"].items()
                    if ts < cutoff
                ]
                for slug in old_slugs:
                    del self._state["orders_placed"][slug]
                    if slug in self._state["order_ids"]:
                        del self._state["order_ids"][slug]

    # ==================== FILLS ====================

    def record_fill(self):
        """Increment fill counter"""
        with self._data_lock:
            self._state["total_fills"] += 1
            self._notify_change("total_fills", self._state["total_fills"])

    def get_fill_count(self) -> int:
        """Get total fills"""
        with self._data_lock:
            return self._state.get("total_fills", 0)

    # ==================== SETTLEMENT TRACKING ====================

    def record_pending_settlement(
        self,
        slug: str,
        condition_id: str,
        expiry_ts: float,
        token_up: str,
        token_down: str,
        up_order_id: Optional[str],
        down_order_id: Optional[str],
        sniper_price: float,
        symbol: str = "UNKNOWN"
    ):
        """Record a market for settlement tracking"""
        with self._data_lock:
            self._state["pending_settlements"][slug] = SettlementRecord(
                slug=slug,
                condition_id=condition_id,
                symbol=symbol,
                expiry_ts=expiry_ts,
                token_up=token_up,
                token_down=token_down,
                up_order_id=up_order_id,
                down_order_id=down_order_id,
                up_avg_price=sniper_price,
                down_avg_price=sniper_price,
            )

    def get_pending_settlements(self) -> Dict[str, SettlementRecord]:
        """Get all pending settlements"""
        with self._data_lock:
            return dict(self._state.get("pending_settlements", {}))

    def get_settlement(self, slug: str) -> Optional[SettlementRecord]:
        """Get a specific settlement"""
        with self._data_lock:
            return self._state["pending_settlements"].get(slug)

    def update_settlement_fills(
        self,
        slug: str,
        side: str,  # "UP" or "DOWN"
        filled: float,
        avg_price: float,
        price_source: str = "api"
    ) -> bool:
        """
        Update fill info for a settlement side.
        Returns True if this is the first fill notification (for anti-spam).
        """
        with self._data_lock:
            if slug not in self._state["pending_settlements"]:
                return False

            settlement = self._state["pending_settlements"][slug]
            side_lower = side.lower()

            if side_lower == "up":
                settlement.up_filled = filled
                settlement.up_avg_price = avg_price
                settlement.up_price_source = price_source
                was_notified = settlement.up_notified
                settlement.up_notified = True
                return not was_notified
            elif side_lower == "down":
                settlement.down_filled = filled
                settlement.down_avg_price = avg_price
                settlement.down_price_source = price_source
                was_notified = settlement.down_notified
                settlement.down_notified = True
                return not was_notified

            return False

    def get_settlement_symbol(self, slug: str) -> str:
        """Get symbol for a settlement"""
        with self._data_lock:
            settlement = self._state["pending_settlements"].get(slug)
            return settlement.symbol if settlement else "UNKNOWN"

    def mark_settlement_resolved(self, slug: str, winner: str, pnl: float):
        """Mark settlement as resolved"""
        with self._data_lock:
            if slug in self._state["pending_settlements"]:
                settlement = self._state["pending_settlements"][slug]
                settlement.status = "resolved"
                settlement.winner = winner
                settlement.pnl = pnl

                if pnl > 0:
                    self._state["total_wins"] += 1
                elif pnl < 0:
                    self._state["total_losses"] += 1
                self._state["total_pnl"] += pnl

                self._notify_change("settlement_resolved", {
                    "slug": slug, "winner": winner, "pnl": pnl
                })

    def mark_settlement_redeemed(self, slug: str):
        """Mark settlement as redeemed"""
        with self._data_lock:
            if slug in self._state["pending_settlements"]:
                self._state["pending_settlements"][slug].status = "redeemed"

            # Cleanup order_ids
            if slug in self._state["order_ids"]:
                del self._state["order_ids"][slug]

    def cleanup_old_settlements(self, max_age_seconds: float = 86400):
        """Clean up old settled records"""
        now = time.time()
        cutoff = now - max_age_seconds

        with self._data_lock:
            old_slugs = [
                slug for slug, data in self._state["pending_settlements"].items()
                if data.status == "redeemed" and data.created_at < cutoff
            ]
            for slug in old_slugs:
                del self._state["pending_settlements"][slug]

    def get_settlement_stats(self) -> Dict[str, Any]:
        """Get settlement statistics"""
        with self._data_lock:
            pending_count = len([
                s for s in self._state["pending_settlements"].values()
                if s.status == "pending"
            ])
            return {
                "total_wins": self._state.get("total_wins", 0),
                "total_losses": self._state.get("total_losses", 0),
                "total_pnl": self._state.get("total_pnl", 0.0),
                "pending_count": pending_count,
            }

    # ==================== ERROR THROTTLING ====================

    def should_log_error(self, key: str, cooldown: float = 10.0) -> bool:
        """Check if error should be logged (throttling)"""
        now = time.time()
        with self._data_lock:
            last = self._state["error_logs"].get(key, 0)
            if now - last > cooldown:
                self._state["error_logs"][key] = now
                return True
            return False

    # ==================== STATE LISTENERS ====================

    def add_listener(self, callback: Callable[[str, Any], None]):
        """Add state change listener"""
        if callback not in self._state_listeners:
            self._state_listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, Any], None]):
        """Remove state change listener"""
        if callback in self._state_listeners:
            self._state_listeners.remove(callback)

    def _notify_change(self, key: str, value: Any):
        """Notify listeners of state change"""
        for listener in self._state_listeners:
            try:
                listener(key, value)
            except Exception:
                pass  # Don't let listener errors crash the bot

    # ==================== SERIALIZATION (for persistence) ====================

    def get_persistent_state(self) -> Dict[str, Any]:
        """Get state that should be persisted for safe restart"""
        with self._order_placement_lock:
            with self._data_lock:
                return {
                    "orders_placed": dict(self._state["orders_placed"]),
                    "order_ids": dict(self._state["order_ids"]),
                    "pending_settlements": {
                        slug: {
                            "slug": s.slug,
                            "condition_id": s.condition_id,
                            "symbol": s.symbol,
                            "expiry_ts": s.expiry_ts,
                            "token_up": s.token_up,
                            "token_down": s.token_down,
                            "up_order_id": s.up_order_id,
                            "down_order_id": s.down_order_id,
                            "up_filled": s.up_filled,
                            "down_filled": s.down_filled,
                            "up_avg_price": s.up_avg_price,
                            "down_avg_price": s.down_avg_price,
                            "up_notified": s.up_notified,
                            "down_notified": s.down_notified,
                            "status": s.status,
                            "winner": s.winner,
                            "pnl": s.pnl,
                            "created_at": s.created_at,
                        }
                        for slug, s in self._state["pending_settlements"].items()
                    },
                    "total_wins": self._state["total_wins"],
                    "total_losses": self._state["total_losses"],
                    "total_pnl": self._state["total_pnl"],
                    "total_orders_placed": self._state["total_orders_placed"],
                    "total_fills": self._state["total_fills"],
                }

    def restore_persistent_state(self, data: Dict[str, Any]):
        """Restore state from persistence"""
        with self._order_placement_lock:
            with self._data_lock:
                if "orders_placed" in data:
                    self._state["orders_placed"] = dict(data["orders_placed"])
                if "order_ids" in data:
                    self._state["order_ids"] = dict(data["order_ids"])
                if "pending_settlements" in data:
                    for slug, s_data in data["pending_settlements"].items():
                        self._state["pending_settlements"][slug] = SettlementRecord(
                            slug=s_data.get("slug", slug),
                            condition_id=s_data.get("condition_id", ""),
                            symbol=s_data.get("symbol", "UNKNOWN"),
                            expiry_ts=s_data.get("expiry_ts", 0),
                            token_up=s_data.get("token_up", ""),
                            token_down=s_data.get("token_down", ""),
                            up_order_id=s_data.get("up_order_id"),
                            down_order_id=s_data.get("down_order_id"),
                            up_filled=s_data.get("up_filled", 0),
                            down_filled=s_data.get("down_filled", 0),
                            up_avg_price=s_data.get("up_avg_price", 0),
                            down_avg_price=s_data.get("down_avg_price", 0),
                            up_notified=s_data.get("up_notified", False),
                            down_notified=s_data.get("down_notified", False),
                            status=s_data.get("status", "pending"),
                            winner=s_data.get("winner"),
                            pnl=s_data.get("pnl", 0),
                            created_at=s_data.get("created_at", time.time()),
                        )
                if "total_wins" in data:
                    self._state["total_wins"] = data["total_wins"]
                if "total_losses" in data:
                    self._state["total_losses"] = data["total_losses"]
                if "total_pnl" in data:
                    self._state["total_pnl"] = data["total_pnl"]
                if "total_orders_placed" in data:
                    self._state["total_orders_placed"] = data["total_orders_placed"]
                if "total_fills" in data:
                    self._state["total_fills"] = data["total_fills"]


# ==================== GLOBAL INSTANCE ====================
_state_manager: Optional[StateManager] = None
_state_lock = threading.Lock()


def get_state_manager(mode: RunMode = None) -> StateManager:
    """Get or create state manager"""
    global _state_manager
    with _state_lock:
        if _state_manager is None:
            _state_manager = StateManager(mode=mode or RunMode.HEADLESS)
        return _state_manager


def reset_state_manager(mode: RunMode = None):
    """Reset state manager (for testing)"""
    global _state_manager
    with _state_lock:
        _state_manager = StateManager(mode=mode or RunMode.HEADLESS)
