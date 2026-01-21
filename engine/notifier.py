"""
Endgame Sniper Bot - Notification Manager
Telegram notifications and event dispatching
"""
import os
import threading
import logging
import requests
from datetime import datetime
from typing import Optional, Callable, List


# ==================== ENVIRONMENT ====================
def _get_env(key: str, default: str = "") -> str:
    """Get environment variable with cleanup"""
    val = os.getenv(key, default)
    if val:
        return str(val).strip().replace('"', '').replace("'", "").replace('\n', '').replace('\r', '')
    return default


# Telegram config
TG_TOKEN = _get_env("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = _get_env("TELEGRAM_CHAT_ID")

# Bot version (will be set from config)
BOT_VERSION = "V2.0 - Headless Optimized"


class NotificationManager:
    """
    Manages all notifications (Telegram, callbacks).
    Thread-safe async sending.
    """

    def __init__(
        self,
        telegram_token: str = None,
        telegram_chat_id: str = None,
        proxy_config: dict = None
    ):
        self.telegram_token = telegram_token or TG_TOKEN
        self.telegram_chat_id = telegram_chat_id or TG_CHAT_ID
        self.proxy_config = proxy_config
        self._listeners: List[Callable[[str, dict], None]] = []
        self._lock = threading.Lock()

    @property
    def telegram_enabled(self) -> bool:
        """Check if Telegram is configured"""
        return bool(self.telegram_token and self.telegram_chat_id)

    def add_listener(self, callback: Callable[[str, dict], None]):
        """Add notification listener"""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, dict], None]):
        """Remove notification listener"""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _dispatch(self, event_type: str, data: dict):
        """Dispatch event to listeners"""
        for listener in self._listeners:
            try:
                listener(event_type, data)
            except Exception as e:
                logging.debug(f"Notification listener error: {e}")

    def _send_telegram_async(self, message: str, parse_mode: str = "HTML"):
        """Send Telegram message in background thread"""
        if not self.telegram_enabled:
            return

        def _send():
            try:
                url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                requests.post(
                    url,
                    data={
                        "chat_id": self.telegram_chat_id,
                        "text": message,
                        "parse_mode": parse_mode
                    },
                    timeout=10,
                    proxies=self.proxy_config
                )
            except Exception as e:
                logging.debug(f"Telegram send error: {e}")

        threading.Thread(target=_send, daemon=True).start()

    # ==================== NOTIFICATION METHODS ====================

    def notify_bot_start(self, version: str = None, proxy_enabled: bool = False, mode: str = "headless"):
        """Notify when bot starts"""
        version = version or BOT_VERSION
        proxy_status = "ON" if proxy_enabled else "OFF"
        mode_display = mode.upper()

        self._send_telegram_async(
            f"\U0001F680 <b>Endgame Sniper Started</b>\n"
            f"Version: {version}\n"
            f"Mode: {mode_display}\n"
            f"Proxy: {proxy_status}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("bot_start", {"version": version, "mode": mode, "proxy": proxy_enabled})

    def notify_bot_stop(self, orders: int = 0, fills: int = 0, wins: int = 0, losses: int = 0, pnl: float = 0):
        """Notify when bot stops"""
        self._send_telegram_async(
            f"\U0001F6D1 <b>Endgame Sniper Stopped</b>\n"
            f"Orders: {orders} | Fills: {fills}\n"
            f"W/L: {wins}/{losses} | PnL: ${pnl:+.2f}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("bot_stop", {"orders": orders, "fills": fills, "wins": wins, "losses": losses, "pnl": pnl})

    def notify_order_placed(
        self,
        symbol: str,
        side: str,
        price: float,
        size: float,
        order_id: Optional[str],
        bid_up: float,
        bid_down: float,
        time_left: float
    ):
        """Notify when order is placed"""
        status = f"\u2705 {order_id[:16]}..." if order_id else "\u274c FAILED"
        self._send_telegram_async(
            f"\U0001F3AF <b>[{symbol}] SNIPE ORDER</b>\n"
            f"Side: {side}\n"
            f"Price: ${price:.2f} x {size:.0f} shares\n"
            f"Status: {status}\n"
            f"UP bid: {bid_up:.2f} | DOWN bid: {bid_down:.2f}\n"
            f"Time left: {time_left:.0f}s"
        )
        self._dispatch("order_placed", {
            "symbol": symbol, "side": side, "price": price,
            "size": size, "order_id": order_id
        })

    def notify_fill(self, symbol: str, side: str, price: float, size: float, filled: float):
        """Notify when order is filled"""
        self._send_telegram_async(
            f"\U0001F389 <b>[{symbol}] ORDER FILLED!</b>\n"
            f"Side: {side}\n"
            f"Price: ${price:.3f}\n"
            f"Filled: {filled:.0f}/{size:.0f} shares\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("fill", {"symbol": symbol, "side": side, "price": price, "filled": filled})

    def notify_balance_update(self, balance: float, orders: int = 0, fills: int = 0):
        """Notify balance update"""
        self._send_telegram_async(
            f"\U0001F4B0 <b>Balance Update</b>\n"
            f"USDC Balance: ${balance:,.2f}\n"
            f"Total orders: {orders}\n"
            f"Total fills: {fills}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("balance_update", {"balance": balance})

    def notify_settlement_result(
        self,
        symbol: str,
        slug: str,
        winner: str,
        up_filled: float,
        down_filled: float,
        up_cost: float,
        down_cost: float,
        pnl: float
    ):
        """Notify settlement result"""
        if pnl > 0:
            emoji = "\U0001F389"  # Party
            result = "WIN"
        elif pnl < 0:
            emoji = "\U0001F61E"  # Sad
            result = "LOSS"
        else:
            emoji = "\U0001F937"  # Shrug
            result = "BREAK-EVEN"

        self._send_telegram_async(
            f"{emoji} <b>[{symbol}] SETTLEMENT - {result}</b>\n\n"
            f"Market: {slug[-25:]}\n"
            f"Winner: <b>{winner}</b>\n\n"
            f"Positions:\n"
            f"  UP: {up_filled:.0f} shares @ ${up_cost:.3f}\n"
            f"  DOWN: {down_filled:.0f} shares @ ${down_cost:.3f}\n\n"
            f"PnL: <b>${pnl:+.2f}</b>\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("settlement", {"symbol": symbol, "winner": winner, "pnl": pnl})

    def notify_redeem_success(self, symbol: str, slug: str, amount: float):
        """Notify successful redeem"""
        self._send_telegram_async(
            f"\U0001F4B0 <b>[{symbol}] REDEEM SUCCESS</b>\n"
            f"Market: {slug[-25:]}\n"
            f"Amount: ${amount:.2f}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("redeem", {"symbol": symbol, "amount": amount})

    def notify_error(self, error_type: str, message: str):
        """Notify on error"""
        self._send_telegram_async(
            f"\u274c <b>Error: {error_type}</b>\n"
            f"{message}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("error", {"type": error_type, "message": message})

    def notify_settlement_timeout(self, symbol: str, slug: str, attempts: int):
        """Notify when settlement watcher gives up (once per market)"""
        self._send_telegram_async(
            f"\u26a0\ufe0f <b>[{symbol}] SETTLEMENT TIMEOUT</b>\n"
            f"Market: {slug[-25:]}\n"
            f"Attempts: {attempts}\n"
            f"May need manual check on Polymarket\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._dispatch("settlement_timeout", {"symbol": symbol, "slug": slug})


# ==================== GLOBAL INSTANCE ====================
_notifier: Optional[NotificationManager] = None
_notifier_lock = threading.Lock()


def get_notifier() -> NotificationManager:
    """Get or create notification manager"""
    global _notifier
    with _notifier_lock:
        if _notifier is None:
            _notifier = NotificationManager()
        return _notifier


def init_notifier(telegram_token: str = None, telegram_chat_id: str = None, proxy_config: dict = None) -> NotificationManager:
    """Initialize notification manager with config"""
    global _notifier
    with _notifier_lock:
        _notifier = NotificationManager(
            telegram_token=telegram_token,
            telegram_chat_id=telegram_chat_id,
            proxy_config=proxy_config
        )
        return _notifier
