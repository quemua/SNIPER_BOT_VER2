"""
Endgame Sniper Bot - Shared State Management
Thread-safe state, logging, and Telegram notifications

FIXED: Added order placement lock to prevent double orders (race condition)
ADDED: Settlement tracking for win/loss detection and redeem
IMPROVED: Standardized locking patterns, anti-spam fill notifications, symbol in settlements
"""
import threading
import queue
import time
import logging
import logging.handlers
import requests
from datetime import datetime
from typing import Optional, Tuple

from config import LOG_FILE, TG_TOKEN, TG_CHAT_ID, SUPPORTED_MARKETS, BOT_VERSION, get_proxy_config, is_proxy_enabled

# ==================== LOCKS & QUEUES ====================
# LOCKING HIERARCHY (to avoid deadlock - always acquire in this order):
#   1. order_placement_lock (outermost)
#   2. data_lock (innermost)
# NEVER acquire order_placement_lock while holding data_lock!

data_lock = threading.RLock()

# Dedicated lock for order placement to prevent race condition
order_placement_lock = threading.Lock()

CLOB_READ_SEM = threading.BoundedSemaphore(5)
CLOB_TRADE_SEM = threading.BoundedSemaphore(2)

stop_event = threading.Event()
log_queue = queue.Queue(maxsize=2000)

# ==================== SHARED DATA ====================
shared_data = {
    "is_running": False,

    # Balance
    "usdc_balance": 0.0,
    "last_balance_update": 0.0,

    # Per-market status
    "market_status": {},

    # Orders placed tracking
    "orders_placed": {},  # {slug: timestamp}
    "order_ids": {},  # {slug: {"up": id, "down": id}}
    
    # NEW: Set of slugs currently being processed (for atomic check-and-set)
    "orders_in_progress": set(),

    # Statistics
    "total_orders_placed": 0,
    "total_fills": 0,

    # Error logs (for throttling)
    "error_logs": {},

    # Settlement tracking for win/loss detection
    # {slug: {
    #     "condition_id": str,
    #     "expiry_ts": float,
    #     "symbol": str,           # NEW: Store symbol to avoid re-extraction
    #     "token_up": str,
    #     "token_down": str,
    #     "up_order_id": str,
    #     "down_order_id": str,
    #     "up_filled": float,
    #     "down_filled": float,
    #     "up_avg_price": float,
    #     "down_avg_price": float,
    #     "up_notified": bool,     # NEW: Track fill notification to avoid spam
    #     "down_notified": bool,   # NEW: Track fill notification to avoid spam
    #     "status": str,           # "pending", "resolved", "redeemed"
    #     "winner": str,           # "UP" or "DOWN" or None
    #     "pnl": float,
    # }}
    "pending_settlements": {},

    # Statistics
    "total_wins": 0,
    "total_losses": 0,
    "total_pnl": 0.0,
}

# Initialize market status
for m in SUPPORTED_MARKETS:
    shared_data["market_status"][m["symbol"]] = {
        "status": "Idle",
        "last_check": 0,
        "current_market": None,
        "up_bid": 0.0,
        "down_bid": 0.0,
    }


# ==================== LOGGING ====================
class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        try:
            self.log_queue.put_nowait(self.format(record))
        except queue.Full:
            pass


def setup_logging():
    """Setup logging with UTF-8 encoding"""
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers = []

    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')

    # File handler with UTF-8 encoding
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # UI handler
    ui_handler = QueueHandler(log_queue)
    ui_handler.setFormatter(formatter)
    root_logger.addHandler(ui_handler)

    # Suppress noisy libraries
    for lib in ["urllib3", "requests", "websockets", "asyncio", "http.client",
                "httpx", "httpcore", "py_clob_client", "websocket"]:
        logging.getLogger(lib).setLevel(logging.CRITICAL)


# ==================== TELEGRAM NOTIFICATIONS ====================
def send_telegram(message: str, parse_mode: str = "HTML"):
    """Send message to Telegram asynchronously"""
    if not TG_TOKEN or not TG_CHAT_ID:
        return

    def _send():
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            proxy_config = get_proxy_config()
            requests.post(
                url, 
                data={
                    "chat_id": TG_CHAT_ID,
                    "text": message,
                    "parse_mode": parse_mode
                }, 
                timeout=10,
                proxies=proxy_config
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def notify_bot_start():
    """Notify when bot starts"""
    from config import is_proxy_enabled
    proxy_status = "✅ Proxy ON" if is_proxy_enabled() else "❌ Proxy OFF"
    
    send_telegram(
        f"\U0001F680 <b>Endgame Sniper Started</b>\n"
        f"Version: {BOT_VERSION}\n"
        f"Proxy: {proxy_status}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def notify_bot_stop():
    """Notify when bot stops"""
    with data_lock:
        orders = shared_data["total_orders_placed"]
        fills = shared_data["total_fills"]

    send_telegram(
        f"\U0001F6D1 <b>Endgame Sniper Stopped</b>\n"
        f"Orders placed: {orders}\n"
        f"Fills: {fills}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def notify_order_placed(symbol: str, side: str, price: float, size: float, order_id: Optional[str], bid_up: float, bid_down: float, time_left: float):
    """Notify when order is placed"""
    status = f"\u2705 {order_id[:16]}..." if order_id else "\u274c FAILED"
    send_telegram(
        f"\U0001F3AF <b>[{symbol}] SNIPE ORDER</b>\n"
        f"Side: {side}\n"
        f"Price: ${price:.2f} x {size:.0f} shares\n"
        f"Status: {status}\n"
        f"UP bid: {bid_up:.2f} | DOWN bid: {bid_down:.2f}\n"
        f"Time left: {time_left:.0f}s"
    )


def notify_balance_update(balance: float):
    """Notify balance update (every 2 hours)"""
    with data_lock:
        orders = shared_data["total_orders_placed"]
        fills = shared_data["total_fills"]

    send_telegram(
        f"\U0001F4B0 <b>Balance Update</b>\n"
        f"USDC Balance: ${balance:,.2f}\n"
        f"Total orders: {orders}\n"
        f"Total fills: {fills}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def notify_error(error_type: str, message: str):
    """Notify on error"""
    send_telegram(
        f"\u274c <b>Error: {error_type}</b>\n"
        f"{message}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def notify_fill(symbol: str, side: str, price: float, size: float, filled: float):
    """Notify when order is filled"""
    with data_lock:
        shared_data["total_fills"] += 1

    send_telegram(
        f"\U0001F389 <b>[{symbol}] ORDER FILLED!</b>\n"
        f"Side: {side}\n"
        f"Price: ${price:.2f}\n"
        f"Filled: {filled:.0f}/{size:.0f} shares\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


# ==================== HELPERS ====================
def log_throttled(key: str, msg: str, cooldown: int = 10):
    """Log error message with throttling to prevent spam"""
    now = time.time()
    with data_lock:
        last = shared_data["error_logs"].get(key, 0)
        if now - last > cooldown:
            logging.error(f"[{key}] {msg}")
            shared_data["error_logs"][key] = now


def should_stop() -> bool:
    """Check if bot should stop"""
    if stop_event.is_set():
        return True
    with data_lock:
        return not shared_data.get("is_running", False)


def get_market_status(symbol: str) -> dict:
    """Get market status"""
    with data_lock:
        return shared_data["market_status"].get(symbol, {}).copy()


def set_market_status(symbol: str, key: str, value):
    """Set market status"""
    with data_lock:
        if symbol not in shared_data["market_status"]:
            shared_data["market_status"][symbol] = {}
        shared_data["market_status"][symbol][key] = value


def is_order_placed_for_market(slug: str) -> bool:
    """Check if orders already placed for this market"""
    with data_lock:
        return slug in shared_data["orders_placed"]


def try_acquire_order_slot(slug: str) -> bool:
    """
    ATOMIC operation: Try to acquire exclusive right to place orders for a market.
    Returns True if this thread has exclusive right, False if another thread already has it.
    
    This prevents race condition where multiple threads check is_order_placed_for_market()
    at the same time and both pass the check.
    """
    with order_placement_lock:
        # Double-check under lock
        if slug in shared_data["orders_placed"]:
            return False
        if slug in shared_data["orders_in_progress"]:
            return False
        
        # Mark as in progress
        shared_data["orders_in_progress"].add(slug)
        return True


def release_order_slot(slug: str, success: bool = False):
    """
    Release order slot after order placement attempt.
    If success=True, mark as placed. Otherwise just release.
    """
    with order_placement_lock:
        shared_data["orders_in_progress"].discard(slug)
        # Note: If success, record_order_placed should be called separately


def record_order_placed(slug: str, up_order_id: Optional[str], down_order_id: Optional[str]):
    """
    Record that orders were placed for a market.

    Lock order: order_placement_lock -> data_lock (following hierarchy)
    """
    with order_placement_lock:
        # Remove from in_progress first (protected by order_placement_lock)
        shared_data["orders_in_progress"].discard(slug)

        # Then update shared_data (requires data_lock)
        with data_lock:
            shared_data["orders_placed"][slug] = time.time()
            shared_data["order_ids"][slug] = {
                "up": up_order_id,
                "down": down_order_id
            }
            # Count orders
            if up_order_id:
                shared_data["total_orders_placed"] += 1
            if down_order_id:
                shared_data["total_orders_placed"] += 1


def cleanup_old_orders():
    """Clean up old order records (older than 1 hour)"""
    now = time.time()
    cutoff = now - 3600  # 1 hour

    with order_placement_lock:
        with data_lock:
            old_slugs = [
                slug for slug, ts in shared_data["orders_placed"].items()
                if ts < cutoff
            ]

            for slug in old_slugs:
                del shared_data["orders_placed"][slug]
                if slug in shared_data["order_ids"]:
                    del shared_data["order_ids"][slug]


# ==================== SETTLEMENT TRACKING ====================
def record_pending_settlement(slug: str, condition_id: str, expiry_ts: float,
                               token_up: str, token_down: str,
                               up_order_id: Optional[str], down_order_id: Optional[str],
                               sniper_price: float, symbol: str = "UNKNOWN"):
    """
    Record a market for settlement tracking.

    Args:
        symbol: Market symbol (BTC, ETH, etc.) - stored to avoid re-extraction from slug
    """
    with data_lock:
        shared_data["pending_settlements"][slug] = {
            "condition_id": condition_id,
            "expiry_ts": expiry_ts,
            "symbol": symbol,  # Store symbol to avoid _extract_symbol_from_slug() later
            "token_up": token_up,
            "token_down": token_down,
            "up_order_id": up_order_id,
            "down_order_id": down_order_id,
            "up_filled": 0.0,
            "down_filled": 0.0,
            "up_avg_price": sniper_price,
            "down_avg_price": sniper_price,
            "up_notified": False,    # Track fill notification to avoid spam
            "down_notified": False,  # Track fill notification to avoid spam
            "status": "pending",
            "winner": None,
            "pnl": 0.0,
            "created_at": time.time(),
        }
        logging.info(f"[Settlement] Tracking [{symbol}] {slug} for settlement (expires {datetime.fromtimestamp(expiry_ts).strftime('%H:%M:%S')})")


def update_settlement_fills(slug: str, up_filled: float, down_filled: float,
                            up_avg_price: float = None, down_avg_price: float = None,
                            up_price_source: str = None, down_price_source: str = None):
    """Update fill information for settlement with price source tracking"""
    with data_lock:
        if slug in shared_data["pending_settlements"]:
            shared_data["pending_settlements"][slug]["up_filled"] = up_filled
            shared_data["pending_settlements"][slug]["down_filled"] = down_filled
            if up_avg_price is not None:
                shared_data["pending_settlements"][slug]["up_avg_price"] = up_avg_price
            if down_avg_price is not None:
                shared_data["pending_settlements"][slug]["down_avg_price"] = down_avg_price
            # Track price source for debugging (api vs fallback)
            if up_price_source is not None:
                shared_data["pending_settlements"][slug]["up_price_source"] = up_price_source
            if down_price_source is not None:
                shared_data["pending_settlements"][slug]["down_price_source"] = down_price_source


def update_fill_and_notify_status(slug: str, side: str, filled: float,
                                   avg_price: float, price_source: str = "api") -> bool:
    """
    Update fill info and check if notification should be sent (anti-spam).

    Args:
        slug: Market slug
        side: "UP" or "DOWN"
        filled: Filled quantity
        avg_price: Average fill price
        price_source: "api" or "fallback"

    Returns:
        True if notification should be sent (first time), False if already notified
    """
    with data_lock:
        if slug not in shared_data["pending_settlements"]:
            return False

        settlement = shared_data["pending_settlements"][slug]
        notify_key = f"{side.lower()}_notified"
        filled_key = f"{side.lower()}_filled"
        price_key = f"{side.lower()}_avg_price"
        source_key = f"{side.lower()}_price_source"

        # Update fill info immediately
        settlement[filled_key] = filled
        if avg_price > 0:
            settlement[price_key] = avg_price
            settlement[source_key] = price_source

        # Check if already notified
        if settlement.get(notify_key, False):
            return False  # Already notified, don't spam

        # Mark as notified
        settlement[notify_key] = True
        return True  # First time, should notify


def get_settlement_symbol(slug: str) -> str:
    """Get stored symbol for a settlement, or UNKNOWN if not found"""
    with data_lock:
        if slug in shared_data["pending_settlements"]:
            return shared_data["pending_settlements"][slug].get("symbol", "UNKNOWN")
        return "UNKNOWN"


def get_pending_settlements() -> dict:
    """Get all pending settlements"""
    with data_lock:
        return dict(shared_data.get("pending_settlements", {}))


def mark_settlement_resolved(slug: str, winner: str, pnl: float):
    """Mark a settlement as resolved with winner"""
    with data_lock:
        if slug in shared_data["pending_settlements"]:
            shared_data["pending_settlements"][slug]["status"] = "resolved"
            shared_data["pending_settlements"][slug]["winner"] = winner
            shared_data["pending_settlements"][slug]["pnl"] = pnl

            # Update stats
            if pnl > 0:
                shared_data["total_wins"] += 1
            elif pnl < 0:
                shared_data["total_losses"] += 1
            shared_data["total_pnl"] += pnl


def mark_settlement_redeemed(slug: str):
    """
    Mark a settlement as redeemed and clean up order_ids.

    This also removes the slug from order_ids since the market is fully settled.
    """
    with data_lock:
        if slug in shared_data["pending_settlements"]:
            shared_data["pending_settlements"][slug]["status"] = "redeemed"

        # Clean up order_ids - no longer needed after redeem
        if slug in shared_data["order_ids"]:
            del shared_data["order_ids"][slug]
            logging.debug(f"[Settlement] Cleaned up order_ids for {slug}")


def cleanup_old_settlements():
    """Clean up old settled records (older than 24 hours)"""
    now = time.time()
    cutoff = now - 86400  # 24 hours

    with data_lock:
        old_slugs = [
            slug for slug, data in shared_data["pending_settlements"].items()
            if data.get("status") == "redeemed" and data.get("created_at", now) < cutoff
        ]
        for slug in old_slugs:
            del shared_data["pending_settlements"][slug]


def get_settlement_stats() -> dict:
    """Get settlement statistics"""
    with data_lock:
        return {
            "total_wins": shared_data.get("total_wins", 0),
            "total_losses": shared_data.get("total_losses", 0),
            "total_pnl": shared_data.get("total_pnl", 0.0),
            "pending_count": len([s for s in shared_data.get("pending_settlements", {}).values()
                                  if s.get("status") == "pending"]),
        }


# ==================== SETTLEMENT NOTIFICATIONS ====================
def notify_settlement_result(symbol: str, slug: str, winner: str,
                             up_filled: float, down_filled: float,
                             up_cost: float, down_cost: float, pnl: float):
    """Notify settlement result via Telegram"""
    if pnl > 0:
        emoji = "\U0001F389"  # 🎉
        result = "WIN"
    elif pnl < 0:
        emoji = "\U0001F61E"  # 😞
        result = "LOSS"
    else:
        emoji = "\U0001F937"  # 🤷
        result = "BREAK-EVEN"

    send_telegram(
        f"{emoji} <b>[{symbol}] SETTLEMENT - {result}</b>\n\n"
        f"Market: {slug[-25:]}\n"
        f"Winner: <b>{winner}</b>\n\n"
        f"Positions:\n"
        f"  UP: {up_filled:.0f} shares @ ${up_cost:.3f}\n"
        f"  DOWN: {down_filled:.0f} shares @ ${down_cost:.3f}\n\n"
        f"PnL: <b>${pnl:+.2f}</b>\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def notify_redeem_success(symbol: str, slug: str, amount: float):
    """Notify successful redeem"""
    send_telegram(
        f"\U0001F4B0 <b>[{symbol}] REDEEM SUCCESS</b>\n"
        f"Market: {slug[-25:]}\n"
        f"Amount: ${amount:.2f}\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
