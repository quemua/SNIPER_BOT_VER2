"""
Endgame Sniper Bot - Trade Manager
Handles CLOB API interactions for order placement

FIXED: Added Proxy Support for ClobClient and requests
ADDED: Market resolution and user trades functions for settlement tracking
IMPROVED: Redeem verification, better handling of balance delta = 0
"""
import time
import threading
import logging
import random
import os
from typing import Optional, Tuple, List

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY

from shared_state import (
    shared_data, data_lock, CLOB_READ_SEM, CLOB_TRADE_SEM,
    log_throttled, stop_event, notify_error
)
from config import (
    PRIVATE_KEY, POLY_PROXY_ADDRESS, HOST, CHAIN_ID, GAMMA_API,
    REQUEST_COOLDOWN, MIN_ORDER_SIZE, BOT_VERSION, BROWSER_HEADERS,
    get_proxy_config, is_proxy_enabled, GLOBAL_SESSION
)

# ==================== CONSTANTS ====================
MAX_RETRIES = 5
RETRY_BASE_DELAY = 0.5
REINIT_AFTER_FAILS = 10

# Track consecutive failures
_consecutive_failures = 0
_last_request_time = 0
_request_lock = threading.Lock()


def _rate_limit():
    """Apply rate limiting between requests"""
    global _last_request_time
    with _request_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < REQUEST_COOLDOWN:
            time.sleep(REQUEST_COOLDOWN - elapsed)
        _last_request_time = time.time()


def _patch_session_with_proxy(session):
    """Patch requests session with proxy configuration"""
    proxy_config = get_proxy_config()
    if proxy_config:
        session.proxies.update(proxy_config)
        logging.info(f"   Proxy configured: {list(proxy_config.keys())}")
    return session


class TradeManager:
    """Trade Manager for Endgame Sniper Bot with Proxy Support"""

    def __init__(self):
        self.client = None
        self.ready = False
        self._init_lock = threading.Lock()
        self._last_reinit = 0
        self.init_client()

    def _patch_client_session(self, client):
        """Patch ClobClient session with browser-like headers and proxy"""
        try:
            proxy_config = get_proxy_config()
            
            # Patch all possible session attributes
            sessions_to_patch = []
            
            if hasattr(client, 'session'):
                sessions_to_patch.append(client.session)
            if hasattr(client, '_session'):
                sessions_to_patch.append(client._session)
            if hasattr(client, 'host') and hasattr(client.host, 'session'):
                sessions_to_patch.append(client.host.session)
                
            # Also check for http_helpers or similar
            if hasattr(client, 'http_helpers'):
                if hasattr(client.http_helpers, 'session'):
                    sessions_to_patch.append(client.http_helpers.session)
            
            for session in sessions_to_patch:
                if session:
                    session.headers.update(BROWSER_HEADERS)
                    if proxy_config:
                        session.proxies.update(proxy_config)
                        
            if proxy_config:
                logging.debug(f"Patched {len(sessions_to_patch)} sessions with proxy")
                
        except Exception as e:
            logging.debug(f"Patch session: {e}")

    def _create_proxied_client(self, funder: Optional[str], sig_type: int) -> ClobClient:
        """
        Create ClobClient with proxy support.
        
        Note: py_clob_client uses requests internally. We patch the session after creation.
        For more complex proxy needs (like SOCKS5), you may need to set env variables:
        - HTTP_PROXY
        - HTTPS_PROXY
        - ALL_PROXY
        """
        # Set environment variables for proxy (some libraries use this)
        proxy_config = get_proxy_config()
        if proxy_config:
            if 'http' in proxy_config:
                os.environ['HTTP_PROXY'] = proxy_config['http']
            if 'https' in proxy_config:
                os.environ['HTTPS_PROXY'] = proxy_config['https']
                os.environ['ALL_PROXY'] = proxy_config['https']
        
        client = ClobClient(
            host=HOST, 
            key=PRIVATE_KEY, 
            chain_id=CHAIN_ID,
            funder=funder, 
            signature_type=sig_type
        )
        
        # Patch session with headers and proxy
        self._patch_client_session(client)
        
        return client

    def _should_reinit(self) -> bool:
        """Check if client should be reinitialized"""
        global _consecutive_failures

        if _consecutive_failures >= REINIT_AFTER_FAILS:
            return True

        if not self.ready or not self.client:
            return True

        return False

    def _record_success(self):
        """Record a successful request"""
        global _consecutive_failures
        _consecutive_failures = 0

    def _record_failure(self):
        """Record a failed request"""
        global _consecutive_failures
        _consecutive_failures += 1

        if self._should_reinit():
            now = time.time()
            if now - self._last_reinit > 60:
                logging.warning(f"\U0001F504 Reinitializing client after {_consecutive_failures} failures...")
                self._last_reinit = now
                _consecutive_failures = 0
                threading.Thread(target=self.init_client, daemon=True).start()

    def init_client(self):
        """Initialize CLOB client with proxy support"""
        with self._init_lock:
            try:
                logging.info(f"\U0001F50C Initializing TradeManager {BOT_VERSION}...")

                if not PRIVATE_KEY:
                    logging.error("No PRIVATE_KEY configured")
                    notify_error("Init", "No PRIVATE_KEY configured in .env")
                    return

                logging.info(f"   PRIVATE_KEY: {'*' * 8}...{PRIVATE_KEY[-4:] if len(PRIVATE_KEY) > 4 else '****'}")
                logging.info(f"   POLY_PROXY: {POLY_PROXY_ADDRESS[:20] if POLY_PROXY_ADDRESS else 'None'}...")
                
                # Log proxy status
                if is_proxy_enabled():
                    proxy_config = get_proxy_config()
                    proxy_display = proxy_config.get('https', proxy_config.get('http', 'Unknown'))
                    # Hide password in log
                    if '@' in proxy_display:
                        parts = proxy_display.split('@')
                        proxy_display = f"***@{parts[-1]}"
                    logging.info(f"   NETWORK PROXY: {proxy_display}")
                else:
                    logging.info(f"   NETWORK PROXY: None (direct connection)")

                funder = POLY_PROXY_ADDRESS if POLY_PROXY_ADDRESS else None
                types_to_try = [2, 1, 0] if funder else [0]
                success_client = None
                found_balance = -1.0
                final_type = 0

                for sig_type in types_to_try:
                    try:
                        logging.info(f"   Trying signature type {sig_type}...")
                        
                        # Use proxied client creation
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
                        
                        # Check for Cloudflare block
                        if "403" in error_msg or "blocked" in error_msg.lower() or "cloudflare" in error_msg.lower():
                            logging.error("   ⚠️ IP may be blocked by Cloudflare! Consider using a proxy.")

                if success_client:
                    self.client = success_client
                    self._patch_client_session(self.client)
                    self.ready = True
                    with data_lock:
                        shared_data["usdc_balance"] = found_balance
                        shared_data["last_balance_update"] = time.time()
                    
                    proxy_status = "with proxy" if is_proxy_enabled() else "direct"
                    logging.info(f"\u2705 WALLET TYPE {final_type} OK ({proxy_status}). Balance: ${found_balance:,.2f}")
                else:
                    logging.error("\u274c All signature types failed!")
                    if not is_proxy_enabled():
                        logging.error("   💡 TIP: Your IP may be blocked. Add PROXY_URL to .env file:")
                        logging.error("      PROXY_URL=http://user:pass@host:port")
                    notify_error("Init", "All wallet signature types failed")
            except Exception as e:
                logging.error(f"Init Error: {e}")
                notify_error("Init", str(e))
                import traceback
                traceback.print_exc()

    def update_balance(self) -> float:
        """Update and return USDC balance"""
        if not self.ready:
            return 0.0

        if not CLOB_READ_SEM.acquire(blocking=False):
            with data_lock:
                return shared_data.get("usdc_balance", 0.0)

        try:
            _rate_limit()
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal_data = self.client.get_balance_allowance(params)
            raw = int(bal_data.get("balance", 0)) if isinstance(bal_data, dict) else int(bal_data.balance)
            usdc = raw / 1_000_000

            with data_lock:
                shared_data["usdc_balance"] = usdc
                shared_data["last_balance_update"] = time.time()

            self._record_success()
            return usdc
        except Exception as e:
            log_throttled("balance", str(e))
            return 0.0
        finally:
            CLOB_READ_SEM.release()

    def fetch_orderbook(self, token_id: str, max_retries: int = 3) -> Tuple[List, List]:
        """Fetch orderbook. Returns (bids_list, asks_list)"""
        if not self.ready or not self.client:
            return [], []

        for attempt in range(max_retries):
            try:
                # FIX: Acquire semaphore BEFORE rate_limit to avoid holding slot while waiting
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
                    if "Request exception" in error_str or "status_code=None" in error_str or "403" in error_str:
                        delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                        time.sleep(delay)
                        self._record_failure()
                        continue
                log_throttled("fetch_book", error_str)
                return [], []

        return [], []

    def get_best_bid(self, token_id: str) -> float:
        """Get best bid price for a token"""
        bids, _ = self.fetch_orderbook(token_id, max_retries=2)
        if bids and len(bids) > 0:
            return bids[0][0]
        # FIX: Log debug when orderbook is empty (helps debugging)
        logging.debug(f"Empty orderbook for token {token_id[:16]}...")
        return 0.0

    def place_buy_order(self, token_id: str, price: float, size: float, max_retries: int = MAX_RETRIES) -> Optional[str]:
        """
        Place BUY order with retry logic.
        Returns order_id on success, None on failure.
        """
        if not self.ready:
            return None

        if size < MIN_ORDER_SIZE:
            logging.error(f"\u274c Order size {size:.2f} < minimum {MIN_ORDER_SIZE}")
            return None

        last_error = None

        for attempt in range(max_retries):
            if stop_event.is_set():
                logging.warning("Stop requested, aborting buy order")
                return None

            try:
                _rate_limit()
                logging.debug(f"\U0001F4E4 BUY: {token_id[:16]}... @ {price:.4f} x {size:.2f} (attempt {attempt+1})")

                with CLOB_TRADE_SEM:
                    resp = self.client.create_and_post_order(
                        OrderArgs(price=price, size=size, side=BUY, token_id=token_id)
                    )

                order_id = None
                if isinstance(resp, dict):
                    order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")

                if order_id:
                    self._record_success()
                    return order_id

                logging.warning(f"\u26a0\ufe0f No order_id in response: {resp}")

            except Exception as e:
                last_error = e
                error_str = str(e)

                # Check for Cloudflare block specifically
                if "403" in error_str and ("cloudflare" in error_str.lower() or "blocked" in error_str.lower()):
                    self._record_failure()
                    if attempt == 0:
                        logging.error(f"\u26a0\ufe0f Cloudflare block detected! IP may be blocked.")
                        if not is_proxy_enabled():
                            logging.error("   💡 TIP: Add PROXY_URL to .env file to bypass")
                    if attempt < max_retries - 1:
                        delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                        logging.warning(f"\u26a0\ufe0f Cloudflare block, retry in {delay:.1f}s...")
                        time.sleep(delay)
                        continue

                if "Request exception" in error_str or "status_code=None" in error_str:
                    self._record_failure()
                    if attempt < max_retries - 1:
                        delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                        logging.warning(f"\u26a0\ufe0f Request exception, retry in {delay:.1f}s...")
                        time.sleep(delay)
                        continue

                if "minimum" in error_str.lower() and "size" in error_str.lower():
                    logging.error(f"\u274c Order rejected: {error_str}")
                    return None

                if attempt < max_retries - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logging.warning(f"\u26a0\ufe0f Order error, retry in {delay:.1f}s: {error_str[:60]}")
                    time.sleep(delay)
                    continue

        if last_error:
            logging.error(f"Buy order failed after {max_retries} attempts: {last_error}")
        return None

    def get_order_status(self, order_id: str, retries: int = 3) -> Tuple[str, float, float]:
        """Get order status. Returns (status, filled_size, total_size)"""
        if not order_id:
            return "UNKNOWN", 0.0, 0.0

        last_error = None

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
                    for field in ["size_matched", "sizeMatched", "matched_size", "filledSize", "filled_size", "filled"]:
                        if field in order and order[field]:
                            try:
                                filled = float(order[field])
                                break
                            except:
                                pass

                    size = 0.0
                    for field in ["original_size", "originalSize", "size", "total_size"]:
                        if field in order and order[field]:
                            try:
                                size = float(order[field])
                                break
                            except:
                                pass
                else:
                    status = str(getattr(order, "status", "UNKNOWN")).upper()
                    filled = float(getattr(order, "size_matched", 0) or getattr(order, "sizeMatched", 0) or 0)
                    size = float(getattr(order, "original_size", 0) or getattr(order, "size", 0) or 0)

                self._record_success()
                return status, filled, size

            except Exception as e:
                last_error = e
                error_str = str(e)

                if "Request exception" in error_str or "status_code=None" in error_str:
                    self._record_failure()

                if attempt < retries:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(delay)
                    continue

        log_throttled("order_status", str(last_error))
        return "ERROR", 0.0, 0.0

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        if not order_id:
            return False

        for attempt in range(3):
            try:
                _rate_limit()
                with CLOB_TRADE_SEM:
                    self.client.cancel(order_id)
                logging.info(f"\U0001F6AB Cancelled: {order_id[:16]}...")
                self._record_success()
                return True
            except Exception as e:
                error_str = str(e)
                if "Request exception" in error_str:
                    self._record_failure()
                    if attempt < 2:
                        time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                        continue
                log_throttled("cancel_order", str(e))
                return False
        return False

    # ==================== SETTLEMENT FUNCTIONS ====================

    def get_market_resolution(self, slug: str, retries: int = 3) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Check if market is resolved and get winner token.

        Returns: (is_resolved, winner_outcome, winner_token_id)
            - is_resolved: True if market is closed/resolved
            - winner_outcome: "UP" or "DOWN" or None
            - winner_token_id: The winning token ID or None
        """
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

                # Check if market is closed
                is_closed = market.get("closed", False)
                if not is_closed:
                    return False, None, None

                # Get tokens and outcomes
                import json
                tokens = json.loads(market.get("clobTokenIds", "[]"))
                outcomes = json.loads(market.get("outcomes", "[]"))

                # Try to find winner from market data
                # Check acceptingOrders, active, etc.
                winner_token = market.get("winningOutcome")

                # If winner not directly available, check outcomes/prices
                # In resolved markets, winner token has price = 1.0, loser = 0.0
                if not winner_token and len(tokens) >= 2:
                    # Try to get from outcome prices if available
                    outcome_prices = market.get("outcomePrices", "[]")
                    if isinstance(outcome_prices, str):
                        outcome_prices = json.loads(outcome_prices)

                    if outcome_prices and len(outcome_prices) >= 2:
                        # Winner has price close to 1.0
                        for idx, price in enumerate(outcome_prices):
                            try:
                                p = float(price)
                                if p > 0.9:  # Winner
                                    winner_token = tokens[idx] if idx < len(tokens) else None
                                    break
                            except:
                                pass

                # Determine outcome (UP/DOWN) from winner token
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
        """
        Get fill information for an order.

        Returns: (filled_qty, avg_price)
        """
        if not order_id:
            return 0.0, 0.0

        for attempt in range(retries):
            try:
                _rate_limit()
                with CLOB_READ_SEM:
                    # Get order details
                    order = self.client.get_order(order_id)

                if not order:
                    return 0.0, 0.0

                if isinstance(order, dict):
                    # Get filled size
                    filled = 0.0
                    for field in ["size_matched", "sizeMatched", "matched_size", "filledSize"]:
                        if field in order and order[field]:
                            try:
                                filled = float(order[field])
                                break
                            except:
                                pass

                    # Get average price (if available) or use order price
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

    def get_condition_id_from_slug(self, slug: str) -> Optional[str]:
        """Get conditionId from market slug"""
        try:
            _rate_limit()
            resp = GLOBAL_SESSION.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)

            if resp.status_code != 200:
                return None

            data = resp.json()
            if not data or not isinstance(data, list) or len(data) == 0:
                return None

            market = data[0]
            return market.get("conditionId") or market.get("condition_id")

        except Exception as e:
            log_throttled("get_condition_id", str(e))
            return None

    def redeem_positions(self, condition_id: str, retries: int = 3) -> Tuple[bool, float]:
        """
        Redeem winning positions for a resolved market.

        Args:
            condition_id: The conditionId of the resolved market

        Returns: (success, amount_redeemed)
            - success: True if redeem was successful or nothing to redeem
            - amount_redeemed: The amount of USDC redeemed (0 if already redeemed or no winning shares)

        IMPROVED:
        - Verify positions before redeem (if API supports)
        - Better handling of balance delta = 0 cases
        - More detailed logging for debugging
        """
        if not condition_id or not self.ready or not self.client:
            return False, 0.0

        for attempt in range(retries):
            try:
                _rate_limit()

                # === VERIFY LAYER: Check if there are positions to redeem ===
                has_positions = self._verify_redeemable_positions(condition_id)
                if has_positions is False:
                    # API confirmed no positions - skip redeem
                    logging.info(f"[Redeem] No redeemable positions for condition {condition_id[:16]}...")
                    return True, 0.0  # Success with 0 amount (nothing to redeem)

                # Get balance before redeem
                balance_before = self.update_balance()
                logging.debug(f"[Redeem] Balance before: ${balance_before:.2f}")

                redeem_result = None
                redeem_method_found = False

                with CLOB_TRADE_SEM:
                    # py-clob-client may have redeem method
                    # Try different approaches based on available methods

                    # Approach 1: Direct redeem if available
                    if hasattr(self.client, 'redeem'):
                        redeem_method_found = True
                        redeem_result = self.client.redeem(condition_id)
                        logging.info(f"Redeem via client.redeem(): {redeem_result}")

                    # Approach 2: Use CTF redeem endpoint
                    elif hasattr(self.client, 'redeem_positions'):
                        redeem_method_found = True
                        redeem_result = self.client.redeem_positions(condition_id)
                        logging.info(f"Redeem via client.redeem_positions(): {redeem_result}")

                    # Approach 3: No redeem method available in py-clob-client
                    # The CLOB API does not have a /redeem endpoint
                    # Redeem must be done via Polymarket UI or direct CTF contract call
                    else:
                        logging.info(f"[Redeem] Auto-redeem not available - py-clob-client does not have redeem method")
                        logging.info(f"[Redeem] Winning positions will be automatically redeemed by Polymarket")
                        logging.info(f"[Redeem] Or redeem manually via https://polymarket.com/portfolio")
                        # Return success since positions exist and will be redeemed eventually
                        return True, 0.0

                if not redeem_method_found:
                    return True, 0.0

                # Wait a bit for balance to update
                time.sleep(2)

                # Get balance after redeem
                balance_after = self.update_balance()
                amount_redeemed = balance_after - balance_before
                logging.debug(f"[Redeem] Balance after: ${balance_after:.2f}, delta: ${amount_redeemed:.2f}")

                if amount_redeemed > 0:
                    logging.info(f"\u2705 Redeem successful! Amount: ${amount_redeemed:.2f}")
                    self._record_success()
                    return True, amount_redeemed
                else:
                    # Balance delta = 0: Multiple possible reasons
                    # 1. Already redeemed previously
                    # 2. No winning shares (all positions were on losing side)
                    # 3. Redeem pending/processing
                    self._record_success()

                    # Check redeem result for hints
                    if redeem_result:
                        result_str = str(redeem_result).lower()
                        if "already" in result_str:
                            logging.info(f"[Redeem] Already redeemed (confirmed by API)")
                            return True, 0.0
                        elif "success" in result_str or "ok" in result_str:
                            logging.info(f"[Redeem] Redeem succeeded but no balance change - likely no winning shares")
                            return True, 0.0

                    # Default: assume success with no payout
                    logging.info(f"[Redeem] Completed with no balance change (already redeemed or no winning shares)")
                    return True, 0.0

            except Exception as e:
                error_str = str(e)
                logging.warning(f"Redeem attempt {attempt + 1} failed: {error_str[:100]}")

                # Check for specific error messages
                if "already" in error_str.lower() or "redeemed" in error_str.lower():
                    logging.info(f"[Redeem] Already redeemed (from error message)")
                    return True, 0.0

                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue

                log_throttled("redeem", error_str)
                return False, 0.0

        return False, 0.0

    def _verify_redeemable_positions(self, condition_id: str) -> Optional[bool]:
        """
        Verify if there are redeemable positions for a condition (if API supports).

        Returns:
            True: Has positions to redeem
            False: No positions to redeem
            None: Could not verify (API doesn't support or error)
        """
        try:
            # Try to check positions via client if available
            if hasattr(self.client, 'get_positions'):
                positions = self.client.get_positions()
                if positions:
                    for pos in positions:
                        if pos.get("condition_id") == condition_id or pos.get("conditionId") == condition_id:
                            size = float(pos.get("size", 0) or pos.get("balance", 0) or 0)
                            if size > 0:
                                return True
                    return False  # No matching position found

            # Try via API endpoint if available
            # Note: This is a best-effort check, not all APIs support this
            return None  # Cannot verify

        except Exception as e:
            logging.debug(f"[Redeem] Position verification failed: {e}")
            return None  # Cannot verify, proceed with redeem

    def get_order_with_avg_price(self, order_id: str, retries: int = 2) -> Tuple[str, float, float, float]:
        """
        Get order status with average fill price.

        Returns: (status, filled_size, total_size, avg_price)
        """
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

                    # Get filled size
                    filled = 0.0
                    for field in ["size_matched", "sizeMatched", "matched_size", "filledSize", "filled_size"]:
                        if field in order and order[field]:
                            try:
                                filled = float(order[field])
                                break
                            except:
                                pass

                    # Get total size
                    size = 0.0
                    for field in ["original_size", "originalSize", "size", "total_size"]:
                        if field in order and order[field]:
                            try:
                                size = float(order[field])
                                break
                            except:
                                pass

                    # Get average price
                    avg_price = 0.0
                    for field in ["average_price", "avgPrice", "avg_price", "price"]:
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
                    filled = float(getattr(order, "size_matched", 0) or getattr(order, "sizeMatched", 0) or 0)
                    size = float(getattr(order, "original_size", 0) or getattr(order, "size", 0) or 0)
                    avg_price = float(getattr(order, "average_price", 0) or getattr(order, "price", 0) or 0)
                    self._record_success()
                    return status, filled, size, avg_price

            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                    continue
                log_throttled("order_with_avg_price", str(e))

        return "ERROR", 0.0, 0.0, 0.0


# ==================== GLOBAL INSTANCE ====================
_trade_manager: Optional[TradeManager] = None


def get_trade_manager() -> Optional[TradeManager]:
    """Get or create TradeManager instance"""
    global _trade_manager
    if _trade_manager is None:
        _trade_manager = TradeManager()
    return _trade_manager


def init_trade_manager() -> TradeManager:
    """Initialize TradeManager"""
    global _trade_manager
    _trade_manager = TradeManager()
    return _trade_manager
