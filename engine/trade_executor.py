"""
Endgame Sniper Bot - Trade Executor
Handles CLOB API interactions for order placement and management.
Refactored from trade_manager.py with proxy support and better error handling.
"""
import os
import time
import threading
import logging
import random
import json
from typing import Optional, Tuple, List

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY

from engine.state_manager import get_state_manager
from engine.logging_config import log_throttled
from engine.api_cache import get_api_cache


# ==================== CONSTANTS ====================
MAX_RETRIES = 5
RETRY_BASE_DELAY = 0.5
REINIT_AFTER_FAILS = 10
REQUEST_COOLDOWN = 0.15
MIN_ORDER_SIZE = 5.0

# API endpoints
HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

# Browser headers for anti-fingerprinting
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Origin': 'https://polymarket.com',
    'Referer': 'https://polymarket.com/',
    'Connection': 'keep-alive',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}


def _get_env(key: str, default: str = "") -> str:
    """Get environment variable with cleanup"""
    val = os.getenv(key, default)
    if val:
        return str(val).strip().replace('"', '').replace("'", "").replace('\n', '').replace('\r', '')
    return default


# Credentials from environment
PRIVATE_KEY = _get_env("PRIVATE_KEY")
POLY_PROXY_ADDRESS = _get_env("POLY_PROXY_ADDRESS")


# ==================== RATE LIMITING ====================
_consecutive_failures = 0
_last_request_time = 0.0
_request_lock = threading.Lock()

# Cloudflare cooldown management
_cloudflare_cooldown_until = 0.0
_cloudflare_lock = threading.Lock()
CLOUDFLARE_INITIAL_COOLDOWN = 3.0  # 3 seconds after first block
CLOUDFLARE_MAX_COOLDOWN = 30.0  # Max 30 seconds cooldown
_cloudflare_consecutive_blocks = 0

# Semaphores for concurrent requests
CLOB_READ_SEM = threading.BoundedSemaphore(5)
CLOB_TRADE_SEM = threading.BoundedSemaphore(2)


def _check_cloudflare_cooldown() -> bool:
    """Check if we're in Cloudflare cooldown. Returns True if should wait."""
    global _cloudflare_cooldown_until
    with _cloudflare_lock:
        now = time.time()
        if now < _cloudflare_cooldown_until:
            wait_time = _cloudflare_cooldown_until - now
            logging.debug(f"In Cloudflare cooldown, waiting {wait_time:.1f}s...")
            time.sleep(wait_time)
            return True
        return False


def _record_cloudflare_block():
    """Record a Cloudflare block and set cooldown"""
    global _cloudflare_cooldown_until, _cloudflare_consecutive_blocks
    with _cloudflare_lock:
        _cloudflare_consecutive_blocks += 1
        # Exponential backoff with cap
        cooldown = min(
            CLOUDFLARE_MAX_COOLDOWN,
            CLOUDFLARE_INITIAL_COOLDOWN * (2 ** (_cloudflare_consecutive_blocks - 1))
        )
        # Add jitter
        cooldown += random.uniform(0.5, 2.0)
        _cloudflare_cooldown_until = time.time() + cooldown
        logging.warning(f"Cloudflare block #{_cloudflare_consecutive_blocks}, cooldown {cooldown:.1f}s")


def _clear_cloudflare_cooldown():
    """Clear Cloudflare cooldown on successful request"""
    global _cloudflare_consecutive_blocks
    with _cloudflare_lock:
        if _cloudflare_consecutive_blocks > 0:
            _cloudflare_consecutive_blocks = 0
            logging.debug("Cloudflare cooldown cleared after success")


def _rate_limit():
    """Apply rate limiting between requests"""
    global _last_request_time
    with _request_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < REQUEST_COOLDOWN:
            time.sleep(REQUEST_COOLDOWN - elapsed)
        _last_request_time = time.time()


# ==================== PROXY CONFIGURATION ====================
def get_proxy_config() -> Optional[dict]:
    """Get proxy configuration from environment"""
    proxy_url = _get_env("PROXY_URL")

    if not proxy_url:
        # Try separate params
        enabled = _get_env("PROXY_ENABLED", "false").lower() == "true"
        if not enabled:
            return None

        host = _get_env("PROXY_HOST")
        if not host:
            return None

        port = _get_env("PROXY_PORT", "1080")
        username = _get_env("PROXY_USERNAME")
        password = _get_env("PROXY_PASSWORD")
        proxy_type = _get_env("PROXY_TYPE", "socks5")

        auth = f"{username}:{password}@" if username and password else ""
        scheme = "socks5h" if proxy_type.startswith("socks") else "http"
        proxy_url = f"{scheme}://{auth}{host}:{port}"

    # Convert socks5 to socks5h for DNS resolution via proxy
    if proxy_url.startswith("socks5://"):
        proxy_url = "socks5h://" + proxy_url[9:]

    return {
        "http": proxy_url,
        "https": proxy_url
    }


def is_proxy_enabled() -> bool:
    """Check if proxy is configured"""
    return get_proxy_config() is not None


# ==================== GLOBAL SESSION ====================
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

GLOBAL_SESSION = requests.Session()
_retries = Retry(
    total=5,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"]
)
_adapter = HTTPAdapter(max_retries=_retries, pool_connections=10, pool_maxsize=20)
GLOBAL_SESSION.mount('https://', _adapter)
GLOBAL_SESSION.mount('http://', _adapter)
GLOBAL_SESSION.verify = True
GLOBAL_SESSION.headers.update(BROWSER_HEADERS)

# Apply proxy
_proxy_config = get_proxy_config()
if _proxy_config:
    GLOBAL_SESSION.proxies.update(_proxy_config)


# ==================== TRADE EXECUTOR ====================
class TradeExecutor:
    """Trade executor with proxy support and robust error handling"""

    def __init__(self):
        self.client = None
        self.ready = False
        self._init_lock = threading.Lock()
        self._last_reinit = 0

        self.init_client()

    def _patch_client_session(self, client):
        """Patch ClobClient session with browser headers and proxy"""
        try:
            proxy_config = get_proxy_config()

            sessions = []
            for attr in ['session', '_session']:
                if hasattr(client, attr):
                    sessions.append(getattr(client, attr))

            for session in sessions:
                if session:
                    session.headers.update(BROWSER_HEADERS)
                    if proxy_config:
                        session.proxies.update(proxy_config)

        except Exception as e:
            logging.debug(f"Patch session: {e}")

    def _create_proxied_client(self, funder: Optional[str], sig_type: int) -> ClobClient:
        """Create ClobClient with proxy support"""
        proxy_config = get_proxy_config()
        if proxy_config:
            os.environ['HTTP_PROXY'] = proxy_config.get('http', '')
            os.environ['HTTPS_PROXY'] = proxy_config.get('https', '')
            os.environ['ALL_PROXY'] = proxy_config.get('https', '')

        client = ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID,
            funder=funder,
            signature_type=sig_type
        )

        self._patch_client_session(client)
        return client

    def _should_reinit(self) -> bool:
        """Check if client needs reinitialization"""
        global _consecutive_failures
        if _consecutive_failures >= REINIT_AFTER_FAILS:
            return True
        if not self.ready or not self.client:
            return True
        return False

    def _record_success(self):
        """Record successful request"""
        global _consecutive_failures
        _consecutive_failures = 0

    def _record_failure(self):
        """Record failed request"""
        global _consecutive_failures
        _consecutive_failures += 1

        if self._should_reinit():
            now = time.time()
            if now - self._last_reinit > 60:
                logging.warning(f"Reinitializing client after {_consecutive_failures} failures...")
                self._last_reinit = now
                _consecutive_failures = 0
                threading.Thread(target=self.init_client, daemon=True).start()

    def init_client(self):
        """Initialize CLOB client"""
        with self._init_lock:
            try:
                logging.info("Initializing TradeExecutor...")

                if not PRIVATE_KEY:
                    logging.error("No PRIVATE_KEY configured")
                    return

                logging.info(f"   PRIVATE_KEY: {'*' * 8}...{PRIVATE_KEY[-4:] if len(PRIVATE_KEY) > 4 else '****'}")
                logging.info(f"   POLY_PROXY: {POLY_PROXY_ADDRESS[:20] if POLY_PROXY_ADDRESS else 'None'}...")

                if is_proxy_enabled():
                    proxy_config = get_proxy_config()
                    proxy_display = proxy_config.get('https', 'Unknown')
                    if '@' in proxy_display:
                        parts = proxy_display.split('@')
                        proxy_display = f"***@{parts[-1]}"
                    logging.info(f"   NETWORK PROXY: {proxy_display}")
                else:
                    logging.info("   NETWORK PROXY: None (direct)")

                funder = POLY_PROXY_ADDRESS if POLY_PROXY_ADDRESS else None
                types_to_try = [2, 1, 0] if funder else [0]
                success_client = None
                found_balance = -1.0
                final_type = 0

                for sig_type in types_to_try:
                    try:
                        logging.info(f"   Trying signature type {sig_type}...")

                        temp_client = self._create_proxied_client(funder, sig_type)
                        creds = temp_client.create_or_derive_api_creds()
                        temp_client.set_api_creds(creds)

                        with CLOB_READ_SEM:
                            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                            bal_data = temp_client.get_balance_allowance(params)
                            raw_bal = int(bal_data.get("balance", 0)) if isinstance(bal_data, dict) else int(bal_data.balance)
                            usdc = raw_bal / 1_000_000

                        logging.info(f"   Type {sig_type}: Balance = ${usdc:.2f}")

                        if usdc > 0:
                            success_client = temp_client
                            found_balance = usdc
                            final_type = sig_type
                            break
                        if success_client is None:
                            success_client = temp_client
                            found_balance = usdc
                            final_type = sig_type
                    except Exception as e:
                        error_msg = str(e)[:100]
                        logging.warning(f"   Type {sig_type} failed: {error_msg}")
                        if "403" in error_msg or "cloudflare" in error_msg.lower():
                            logging.error("   IP may be blocked! Consider using proxy.")

                if success_client:
                    self.client = success_client
                    self._patch_client_session(self.client)
                    self.ready = True

                    state = get_state_manager()
                    state.update_balance(found_balance)

                    proxy_status = "with proxy" if is_proxy_enabled() else "direct"
                    logging.info(f"WALLET TYPE {final_type} OK ({proxy_status}). Balance: ${found_balance:,.2f}")
                else:
                    logging.error("All signature types failed!")
                    if not is_proxy_enabled():
                        logging.error("   TIP: Your IP may be blocked. Add PROXY_URL to .env")

            except Exception as e:
                logging.error(f"Init Error: {e}")
                import traceback
                traceback.print_exc()

    def update_balance(self) -> float:
        """Update and return USDC balance"""
        if not self.ready:
            return 0.0

        if not CLOB_READ_SEM.acquire(blocking=False):
            return get_state_manager().get_balance()

        try:
            _rate_limit()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal_data = self.client.get_balance_allowance(params)
            raw = int(bal_data.get("balance", 0)) if isinstance(bal_data, dict) else int(bal_data.balance)
            usdc = raw / 1_000_000

            get_state_manager().update_balance(usdc)
            self._record_success()
            return usdc
        except Exception as e:
            log_throttled("balance", str(e))
            return 0.0
        finally:
            CLOB_READ_SEM.release()

    def fetch_orderbook(self, token_id: str, max_retries: int = 3) -> Tuple[List, List]:
        """Fetch orderbook. Returns (bids, asks)"""
        if not self.ready or not self.client:
            return [], []

        for attempt in range(max_retries):
            try:
                with CLOB_READ_SEM:
                    _rate_limit()
                    book = self.client.get_order_book(token_id)

                bids = []
                asks = []

                if hasattr(book, 'bids') and book.bids:
                    for b in book.bids:
                        bids.append((float(b.price), float(b.size)))

                if hasattr(book, 'asks') and book.asks:
                    for a in book.asks:
                        asks.append((float(a.price), float(a.size)))

                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])

                self._record_success()
                return bids, asks

            except Exception as e:
                error_str = str(e)
                if attempt < max_retries - 1:
                    if "Request exception" in error_str or "403" in error_str:
                        delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                        time.sleep(delay)
                        self._record_failure()
                        continue
                log_throttled("fetch_book", error_str)
                return [], []

        return [], []

    def get_best_bid(self, token_id: str) -> float:
        """Get best bid price for a token (with cache)"""
        # Check cache first
        cache = get_api_cache()
        hit, cached_bid = cache.get_best_bid(token_id)
        if hit:
            return cached_bid

        bids, _ = self.fetch_orderbook(token_id, max_retries=2)
        if bids and len(bids) > 0:
            best_bid = bids[0][0]
            cache.set_best_bid(token_id, best_bid, ttl=0.5)
            return best_bid
        return 0.0

    def place_buy_order(self, token_id: str, price: float, size: float, max_retries: int = MAX_RETRIES) -> Optional[str]:
        """Place BUY order. Returns order_id on success."""
        if not self.ready:
            return None

        if size < MIN_ORDER_SIZE:
            logging.error(f"Order size {size:.2f} < minimum {MIN_ORDER_SIZE}")
            return None

        last_error = None
        state = get_state_manager()

        # Check Cloudflare cooldown before attempting
        _check_cloudflare_cooldown()

        for attempt in range(max_retries):
            if state.should_stop():
                logging.warning("Stop requested, aborting buy order")
                return None

            try:
                _rate_limit()
                logging.debug(f"BUY: {token_id[:16]}... @ {price:.4f} x {size:.2f} (attempt {attempt+1})")

                with CLOB_TRADE_SEM:
                    resp = self.client.create_and_post_order(
                        OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
                    )

                order_id = None
                if isinstance(resp, dict):
                    order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")

                if order_id:
                    self._record_success()
                    _clear_cloudflare_cooldown()  # Clear cooldown on success
                    return order_id

                logging.warning(f"No order_id in response: {resp}")

            except Exception as e:
                last_error = e
                error_str = str(e)

                # Cloudflare block - use global cooldown system
                if "403" in error_str and ("cloudflare" in error_str.lower() or "<!doctype html>" in error_str.lower()):
                    self._record_failure()
                    _record_cloudflare_block()  # Set global cooldown
                    if attempt < max_retries - 1:
                        _check_cloudflare_cooldown()  # Wait for cooldown
                        continue
                    else:
                        # Final attempt failed, return early to allow other markets to try later
                        logging.error(f"Buy order blocked by Cloudflare after {attempt+1} attempts")
                        return None

                if "Request exception" in error_str:
                    self._record_failure()
                    if attempt < max_retries - 1:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                        time.sleep(delay)
                        continue

                if "minimum" in error_str.lower() and "size" in error_str.lower():
                    logging.error(f"Order rejected: {error_str}")
                    return None

                if attempt < max_retries - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
                    continue

        if last_error:
            logging.error(f"Buy order failed after {max_retries} attempts: {last_error}")
        return None

    def get_order_status(self, order_id: str, retries: int = 3) -> Tuple[str, float, float]:
        """Get order status. Returns (status, filled, total)"""
        if not order_id:
            return "UNKNOWN", 0.0, 0.0

        for attempt in range(retries + 1):
            try:
                _rate_limit()
                with CLOB_READ_SEM:
                    order = self.client.get_order(order_id)

                if not order:
                    return "NOT_FOUND", 0.0, 0.0

                if isinstance(order, dict):
                    status = str(order.get("status", "UNKNOWN")).upper()
                    filled = 0.0
                    for field in ["size_matched", "sizeMatched", "matched_size", "filledSize"]:
                        if field in order and order[field]:
                            try:
                                filled = float(order[field])
                                break
                            except:
                                pass

                    size = 0.0
                    for field in ["original_size", "originalSize", "size"]:
                        if field in order and order[field]:
                            try:
                                size = float(order[field])
                                break
                            except:
                                pass
                else:
                    status = str(getattr(order, "status", "UNKNOWN")).upper()
                    filled = float(getattr(order, "size_matched", 0) or 0)
                    size = float(getattr(order, "original_size", 0) or getattr(order, "size", 0) or 0)

                self._record_success()
                return status, filled, size

            except Exception as e:
                if attempt < retries:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue

        return "ERROR", 0.0, 0.0

    def get_order_with_avg_price(self, order_id: str, retries: int = 2) -> Tuple[str, float, float, float]:
        """Get order status with average price. Returns (status, filled, total, avg_price)"""
        if not order_id:
            return "UNKNOWN", 0.0, 0.0, 0.0

        for attempt in range(retries):
            try:
                _rate_limit()
                with CLOB_READ_SEM:
                    order = self.client.get_order(order_id)

                if not order:
                    return "NOT_FOUND", 0.0, 0.0, 0.0

                if isinstance(order, dict):
                    status = str(order.get("status", "UNKNOWN")).upper()

                    filled = 0.0
                    for field in ["size_matched", "sizeMatched", "filledSize"]:
                        if field in order and order[field]:
                            try:
                                filled = float(order[field])
                                break
                            except:
                                pass

                    size = 0.0
                    for field in ["original_size", "originalSize", "size"]:
                        if field in order and order[field]:
                            try:
                                size = float(order[field])
                                break
                            except:
                                pass

                    avg_price = 0.0
                    for field in ["average_price", "avgPrice", "price"]:
                        if field in order and order[field]:
                            try:
                                avg_price = float(order[field])
                                break
                            except:
                                pass

                    self._record_success()
                    return status, filled, size, avg_price
                else:
                    status = str(getattr(order, "status", "UNKNOWN")).upper()
                    filled = float(getattr(order, "size_matched", 0) or 0)
                    size = float(getattr(order, "original_size", 0) or 0)
                    avg_price = float(getattr(order, "price", 0) or 0)
                    self._record_success()
                    return status, filled, size, avg_price

            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                log_throttled("order_avg_price", str(e))

        return "ERROR", 0.0, 0.0, 0.0

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        if not order_id:
            return False

        for attempt in range(3):
            try:
                _rate_limit()
                with CLOB_TRADE_SEM:
                    self.client.cancel(order_id)
                logging.info(f"Cancelled: {order_id[:16]}...")
                self._record_success()
                return True
            except Exception as e:
                if attempt < 2:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                log_throttled("cancel_order", str(e))
                return False
        return False

    def get_market_resolution(self, slug: str, retries: int = 3) -> Tuple[bool, Optional[str], Optional[str]]:
        """Check if market is resolved. Returns (is_resolved, winner_outcome, winner_token)"""
        for attempt in range(retries):
            try:
                _rate_limit()
                resp = GLOBAL_SESSION.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)

                if resp.status_code != 200:
                    continue

                data = resp.json()
                if not data or not isinstance(data, list) or len(data) == 0:
                    continue

                market = data[0]
                is_closed = market.get("closed", False)
                if not is_closed:
                    return False, None, None

                tokens = json.loads(market.get("clobTokenIds", "[]"))
                outcomes = json.loads(market.get("outcomes", "[]"))
                winner_token = market.get("winningOutcome")

                if not winner_token and len(tokens) >= 2:
                    outcome_prices = market.get("outcomePrices", "[]")
                    if isinstance(outcome_prices, str):
                        outcome_prices = json.loads(outcome_prices)

                    if outcome_prices and len(outcome_prices) >= 2:
                        for idx, price in enumerate(outcome_prices):
                            try:
                                p = float(price)
                                if p > 0.9:
                                    winner_token = tokens[idx] if idx < len(tokens) else None
                                    break
                            except:
                                pass

                winner_outcome = None
                if winner_token and len(tokens) >= 2 and len(outcomes) >= 2:
                    for idx, token in enumerate(tokens):
                        if token == winner_token:
                            outcome_str = str(outcomes[idx]).lower()
                            if outcome_str in ["yes", "up", "high"]:
                                winner_outcome = "UP"
                            elif outcome_str in ["no", "down", "low"]:
                                winner_outcome = "DOWN"
                            break

                self._record_success()
                return True, winner_outcome, winner_token

            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                log_throttled("market_resolution", str(e))

        return False, None, None

    def get_fills_for_order(self, order_id: str, retries: int = 2) -> Tuple[float, float]:
        """Get fill info. Returns (filled_qty, avg_price)"""
        if not order_id:
            return 0.0, 0.0

        for attempt in range(retries):
            try:
                _rate_limit()
                with CLOB_READ_SEM:
                    order = self.client.get_order(order_id)

                if not order:
                    return 0.0, 0.0

                if isinstance(order, dict):
                    filled = 0.0
                    for field in ["size_matched", "sizeMatched", "filledSize"]:
                        if field in order and order[field]:
                            try:
                                filled = float(order[field])
                                break
                            except:
                                pass

                    avg_price = 0.0
                    for field in ["average_price", "avgPrice", "price"]:
                        if field in order and order[field]:
                            try:
                                avg_price = float(order[field])
                                break
                            except:
                                pass

                    self._record_success()
                    return filled, avg_price
                else:
                    filled = float(getattr(order, "size_matched", 0) or 0)
                    avg_price = float(getattr(order, "price", 0) or 0)
                    self._record_success()
                    return filled, avg_price

            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                log_throttled("get_fills", str(e))

        return 0.0, 0.0

    def redeem_positions(self, condition_id: str, retries: int = 3) -> Tuple[bool, float]:
        """Redeem winning positions. Returns (success, amount_redeemed)"""
        if not condition_id or not self.ready or not self.client:
            return False, 0.0

        for attempt in range(retries):
            try:
                _rate_limit()

                balance_before = self.update_balance()

                with CLOB_TRADE_SEM:
                    if hasattr(self.client, 'redeem'):
                        self.client.redeem(condition_id)
                    elif hasattr(self.client, 'redeem_positions'):
                        self.client.redeem_positions(condition_id)
                    else:
                        logging.info("Auto-redeem not available - will be redeemed by Polymarket")
                        return True, 0.0

                time.sleep(2)
                balance_after = self.update_balance()
                amount = balance_after - balance_before

                if amount > 0:
                    logging.info(f"Redeem successful! Amount: ${amount:.2f}")
                    self._record_success()
                    return True, amount
                else:
                    self._record_success()
                    logging.info("Redeem completed with no balance change")
                    return True, 0.0

            except Exception as e:
                error_str = str(e)
                if "already" in error_str.lower() or "redeemed" in error_str.lower():
                    return True, 0.0

                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue

                log_throttled("redeem", error_str)
                return False, 0.0

        return False, 0.0


# ==================== GLOBAL INSTANCE ====================
_trade_executor: Optional[TradeExecutor] = None
_executor_lock = threading.Lock()


def get_trade_executor() -> Optional[TradeExecutor]:
    """Get or create trade executor"""
    global _trade_executor
    with _executor_lock:
        if _trade_executor is None:
            _trade_executor = TradeExecutor()
        return _trade_executor


def init_trade_executor() -> TradeExecutor:
    """Initialize trade executor"""
    global _trade_executor
    with _executor_lock:
        _trade_executor = TradeExecutor()
        return _trade_executor
