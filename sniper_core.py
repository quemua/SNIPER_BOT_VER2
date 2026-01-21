"""
Endgame Sniper Bot - Core Sniper Logic
Catches price crashes in the final minutes before market expiry

FIXED: Use atomic try_acquire_order_slot() to prevent double orders
ADDED: Order fill monitor worker to track order fills
ADDED: Settlement watcher for win/loss detection and PnL calculation
IMPROVED: Better handling when one order succeeds and one fails
IMPROVED: Anti-spam fill notifications, symbol stored in settlements
IMPROVED: Settlement watcher uses cached fills, better redeem verification
"""
import time
import logging
import threading
import json
import pytz
from datetime import datetime, timedelta
from dateutil import parser
from typing import Optional, Dict

from shared_state import (
    shared_data, data_lock, should_stop, log_throttled,
    set_market_status, is_order_placed_for_market, record_order_placed,
    cleanup_old_orders, notify_order_placed, notify_fill,
    try_acquire_order_slot, release_order_slot,
    # Settlement tracking
    record_pending_settlement, get_pending_settlements, update_settlement_fills,
    mark_settlement_resolved, mark_settlement_redeemed, cleanup_old_settlements,
    notify_settlement_result, notify_redeem_success, get_settlement_stats,
    # NEW: Anti-spam and symbol helpers
    update_fill_and_notify_status, get_settlement_symbol
)
from config import (
    sniper_config, SUPPORTED_MARKETS, GLOBAL_SESSION, GAMMA_API
)
from trade_manager import get_trade_manager


def robust_api_get(url: str, timeout: int = 10, max_retries: int = 2):
    """Make API request with retry and better error handling"""
    for attempt in range(max_retries):
        try:
            resp = GLOBAL_SESSION.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            # Log non-200 status codes
            if attempt == max_retries - 1:
                logging.debug(f"API returned status {resp.status_code}: {url[:50]}...")
        except Exception as e:
            if attempt == max_retries - 1:
                logging.debug(f"API request failed after {max_retries} attempts: {str(e)[:50]}")
            time.sleep(0.5 * (attempt + 1))  # Small backoff between retries
    return None


class EndgameSniper:
    """
    Endgame Sniper - Catches price crashes in final minutes

    Strategy:
    - Monitor markets in their final N minutes (configurable)
    - If both UP and DOWN have best_bid > trigger_threshold, place cheap limit orders
    - Orders at sniper_price will only fill if price crashes near 0
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._workers: Dict[str, threading.Thread] = {}
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None
        self._fill_monitor_thread: Optional[threading.Thread] = None
        self._settlement_thread: Optional[threading.Thread] = None  # NEW: Settlement watcher

        logging.info("\u2705 EndgameSniper initialized")

    def start(self):
        """Start the endgame sniper"""
        if self._running:
            return

        self._running = True

        # Start worker for each supported market
        for market in SUPPORTED_MARKETS:
            symbol = market["symbol"]
            prefix = market["prefix"]

            worker = threading.Thread(
                target=self._market_worker,
                args=(symbol, prefix),
                daemon=True,
                name=f"Sniper-{symbol}"
            )
            self._workers[symbol] = worker
            worker.start()

        # Start cleanup thread
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            daemon=True,
            name="Sniper-Cleanup"
        )
        self._cleanup_thread.start()

        # Start fill monitor thread
        self._fill_monitor_thread = threading.Thread(
            target=self._fill_monitor_worker,
            daemon=True,
            name="Sniper-FillMonitor"
        )
        self._fill_monitor_thread.start()

        # NEW: Start settlement watcher thread
        self._settlement_thread = threading.Thread(
            target=self._settlement_watcher_worker,
            daemon=True,
            name="Sniper-Settlement"
        )
        self._settlement_thread.start()

        logging.info(f"\U0001F3AF Endgame Sniper STARTED for {len(SUPPORTED_MARKETS)} markets")

    def stop(self):
        """Stop the endgame sniper"""
        self._running = False
        self._workers.clear()
        logging.info("\U0001F6D1 Endgame Sniper STOPPED")

    def is_running(self) -> bool:
        """Check if sniper is running"""
        return self._running

    def _get_current_market(self, prefix: str) -> Optional[Dict]:
        """Find the current active market in trigger window"""
        try:
            now_dt = datetime.now(pytz.UTC)
            now_ts = time.time()

            # Calculate 15-minute window
            floored = (now_dt.minute // 15) * 15
            start = now_dt.replace(minute=floored, second=0, microsecond=0)

            # Check current and next windows
            slugs = [
                f"{prefix}-{int((start + timedelta(minutes=15*i)).timestamp())}"
                for i in range(2)
            ]

            trigger_window = sniper_config.trigger_minutes_before_expiry * 60

            for slug in slugs:
                try:
                    resp = robust_api_get(f"{GAMMA_API}/markets?slug={slug}")
                    if not resp:
                        continue

                    res = resp.json()
                    # FIX: Better validation - check for list and non-empty
                    if not res or not isinstance(res, list) or len(res) == 0:
                        continue

                    market = res[0]

                    if market.get("closed"):
                        continue

                    # Parse expiry
                    expiry_dt = parser.isoparse(market["endDate"])
                    expiry_ts = expiry_dt.timestamp()

                    # Check if within trigger window
                    time_to_expiry = expiry_ts - now_ts

                    if 0 < time_to_expiry <= trigger_window:
                        # Get tokens
                        tokens = json.loads(market.get("clobTokenIds", "[]"))
                        outcomes = json.loads(market.get("outcomes", "[]"))

                        if len(tokens) < 2:
                            continue

                        token_up, token_down = None, None
                        for idx, lbl in enumerate(outcomes):
                            lbl_str = str(lbl).lower()
                            if lbl_str in ["yes", "up", "high"]:
                                token_up = tokens[idx]
                            elif lbl_str in ["no", "down", "low"]:
                                token_down = tokens[idx]

                        if not token_up:
                            token_up = tokens[0]
                        if not token_down:
                            token_down = tokens[1]

                        return {
                            "slug": slug,
                            "token_up": token_up,
                            "token_down": token_down,
                            "expiry": expiry_ts,
                            "time_to_expiry": time_to_expiry,
                            "condition_id": market.get("conditionId") or market.get("condition_id"),
                        }

                except Exception:
                    continue

            return None

        except Exception as e:
            log_throttled("sniper_market", str(e))
            return None

    def _check_and_place_orders(self, symbol: str, market: Dict) -> bool:
        """Check conditions and place orders if met"""
        slug = market["slug"]
        token_up = market["token_up"]
        token_down = market["token_down"]
        time_left = market["time_to_expiry"]

        # FIXED: Use atomic try_acquire_order_slot instead of is_order_placed_for_market
        # This prevents race condition where 2 threads pass the check simultaneously
        if not try_acquire_order_slot(slug):
            # Another thread is already handling this market or orders already placed
            logging.debug(f"[{symbol}] Order slot already taken for {slug}")
            return False

        try:
            # Get trade manager
            executor = get_trade_manager()
            if not executor or not executor.ready:
                release_order_slot(slug, success=False)
                return False

            # Get best bids
            bid_up = executor.get_best_bid(token_up)
            bid_down = executor.get_best_bid(token_down)

            # Update market status
            set_market_status(symbol, "up_bid", bid_up)
            set_market_status(symbol, "down_bid", bid_down)
            set_market_status(symbol, "last_check", time.time())

            trigger_threshold = sniper_config.trigger_threshold
            sniper_price = sniper_config.sniper_price
            order_size = sniper_config.order_size_shares

            logging.debug(f"[{symbol}] Check: UP bid={bid_up:.2f}, DOWN bid={bid_down:.2f}, threshold={trigger_threshold:.2f}, {time_left:.0f}s left")

            # Check condition: both bids > threshold
            if bid_up > trigger_threshold and bid_down > trigger_threshold:
                logging.info(f"\U0001F3AF [{symbol}] TRIGGER! UP={bid_up:.2f} DOWN={bid_down:.2f} > {trigger_threshold:.2f}")

                set_market_status(symbol, "status", "TRIGGERED!")

                # Place UP order
                up_order_id = executor.place_buy_order(token_up, sniper_price, order_size)

                # Notify UP order
                notify_order_placed(symbol, "UP", sniper_price, order_size, up_order_id, bid_up, bid_down, time_left)

                # Small delay
                time.sleep(0.2)

                # Place DOWN order
                down_order_id = executor.place_buy_order(token_down, sniper_price, order_size)

                # Notify DOWN order
                notify_order_placed(symbol, "DOWN", sniper_price, order_size, down_order_id, bid_up, bid_down, time_left)

                # Log results
                up_status = f"\u2705 {up_order_id[:16]}..." if up_order_id else "\u274c FAILED"
                down_status = f"\u2705 {down_order_id[:16]}..." if down_order_id else "\u274c FAILED"

                logging.info(f"[{symbol}] Sniper orders placed:")
                logging.info(f"   UP   @ {sniper_price} x {order_size}: {up_status}")
                logging.info(f"   DOWN @ {sniper_price} x {order_size}: {down_status}")

                # IMPROVED: Handle partial success - retry failed order once
                if up_order_id and not down_order_id:
                    logging.warning(f"[{symbol}] UP succeeded but DOWN failed. Retrying DOWN...")
                    time.sleep(0.3)
                    down_order_id = executor.place_buy_order(token_down, sniper_price, order_size)
                    if down_order_id:
                        logging.info(f"[{symbol}] DOWN retry succeeded: {down_order_id[:16]}...")
                        notify_order_placed(symbol, "DOWN", sniper_price, order_size, down_order_id, bid_up, bid_down, time_left)
                    else:
                        logging.error(f"[{symbol}] DOWN retry also failed!")

                elif down_order_id and not up_order_id:
                    logging.warning(f"[{symbol}] DOWN succeeded but UP failed. Retrying UP...")
                    time.sleep(0.3)
                    up_order_id = executor.place_buy_order(token_up, sniper_price, order_size)
                    if up_order_id:
                        logging.info(f"[{symbol}] UP retry succeeded: {up_order_id[:16]}...")
                        notify_order_placed(symbol, "UP", sniper_price, order_size, up_order_id, bid_up, bid_down, time_left)
                    else:
                        logging.error(f"[{symbol}] UP retry also failed!")

                # Record orders (this also releases the slot)
                # Only record if at least one order succeeded
                if up_order_id or down_order_id:
                    record_order_placed(slug, up_order_id, down_order_id)
                    set_market_status(symbol, "status", f"Orders placed @ ${sniper_price}")

                    # Record for settlement tracking (with symbol to avoid re-extraction)
                    condition_id = market.get("condition_id")
                    expiry_ts = market.get("expiry", 0)
                    record_pending_settlement(
                        slug=slug,
                        condition_id=condition_id,
                        expiry_ts=expiry_ts,
                        token_up=token_up,
                        token_down=token_down,
                        up_order_id=up_order_id,
                        down_order_id=down_order_id,
                        sniper_price=sniper_price,
                        symbol=symbol  # Store symbol to avoid _extract_symbol_from_slug() later
                    )

                    return True
                else:
                    # Both orders failed, release slot so it can be retried
                    logging.error(f"[{symbol}] Both orders failed! Releasing slot for retry.")
                    release_order_slot(slug, success=False)
                    set_market_status(symbol, "status", "Orders FAILED - will retry")
                    return False
            else:
                # Condition not met, release slot
                # IMPROVED: Add debug log for why condition not met
                if bid_up <= trigger_threshold:
                    logging.debug(f"[{symbol}] Condition not met: UP bid {bid_up:.2f} <= threshold {trigger_threshold:.2f}")
                if bid_down <= trigger_threshold:
                    logging.debug(f"[{symbol}] Condition not met: DOWN bid {bid_down:.2f} <= threshold {trigger_threshold:.2f}")
                release_order_slot(slug, success=False)
                return False

        except Exception as e:
            # Error occurred, release slot
            release_order_slot(slug, success=False)
            log_throttled(f"check_place_{symbol}", str(e))
            return False

    def _market_worker(self, symbol: str, prefix: str):
        """Worker thread for a single market"""
        logging.info(f"\U0001F3AF [{symbol}] Sniper worker started")

        last_check_slug = None

        while self._running and not should_stop():
            try:
                # Check if market is enabled
                if not sniper_config.is_market_enabled(symbol):
                    set_market_status(symbol, "status", "Disabled")
                    time.sleep(sniper_config.check_interval_seconds)  # FIX: Use config interval instead of hardcoded 5
                    continue

                set_market_status(symbol, "status", "Scanning...")

                # Find market in trigger window
                market = self._get_current_market(prefix)

                if not market:
                    set_market_status(symbol, "status", "Waiting for market")
                    time.sleep(sniper_config.check_interval_seconds)
                    continue

                slug = market["slug"]
                time_left = market["time_to_expiry"]

                set_market_status(symbol, "current_market", slug[-20:])

                # Log when entering trigger window (once per market)
                if slug != last_check_slug:
                    logging.info(f"\U0001F3AF [{symbol}] Entering trigger window: {slug[-20:]} ({time_left:.0f}s left)")
                    last_check_slug = slug
                    set_market_status(symbol, "status", f"In window ({time_left:.0f}s)")

                # Skip if already placed orders (quick check without lock)
                if is_order_placed_for_market(slug):
                    set_market_status(symbol, "status", f"Orders placed")
                    time.sleep(sniper_config.check_interval_seconds)
                    continue

                # Check conditions and place orders (uses atomic lock internally)
                self._check_and_place_orders(symbol, market)

                # Wait before next check
                time.sleep(sniper_config.check_interval_seconds)

            except Exception as e:
                log_throttled(f"sniper_{symbol}", str(e))
                set_market_status(symbol, "status", f"Error: {str(e)[:20]}")
                time.sleep(5)

        logging.info(f"\U0001F3AF [{symbol}] Sniper worker stopped")

    def _cleanup_worker(self):
        """Cleanup worker - removes old order records"""
        while self._running and not should_stop():
            try:
                cleanup_old_orders()
                time.sleep(300)  # Every 5 minutes
            except Exception:
                time.sleep(60)

    def _fill_monitor_worker(self):
        """
        Monitor orders for fills.
        Checks order status every 10 seconds and notifies on fills.

        IMPROVED:
        - Uses actual avg fill price instead of sniper_price
        - Uses update_fill_and_notify_status() for anti-spam (tracks by slug+side)
        - Updates settlement record with avg_price immediately on fill
        - Uses stored symbol from settlement instead of re-extracting
        """
        logging.info("\U0001F50D Fill monitor started")

        while self._running and not should_stop():
            try:
                executor = get_trade_manager()
                if not executor or not executor.ready:
                    time.sleep(10)
                    continue

                # Get current order IDs
                with data_lock:
                    order_ids_copy = dict(shared_data.get("order_ids", {}))

                for slug, orders in order_ids_copy.items():
                    if should_stop():
                        break

                    up_id = orders.get("up")
                    down_id = orders.get("down")

                    # Get stored symbol (or fallback to extraction)
                    symbol = get_settlement_symbol(slug)
                    if symbol == "UNKNOWN":
                        symbol = self._extract_symbol_from_slug(slug)

                    # Check UP order - use get_order_with_avg_price for accurate fill price
                    if up_id:
                        try:
                            status, filled, total, avg_price = executor.get_order_with_avg_price(up_id)
                            if status in ["FILLED", "MATCHED"] or (filled > 0 and filled >= total * 0.99):
                                # Use actual avg_price, fallback to sniper_price only if 0
                                fill_price = avg_price if avg_price > 0 else sniper_config.sniper_price
                                price_source = "api" if avg_price > 0 else "fallback"

                                # Update settlement and check if should notify (anti-spam)
                                should_notify = update_fill_and_notify_status(
                                    slug, "UP", filled, fill_price, price_source
                                )

                                if should_notify:
                                    notify_fill(symbol, "UP", fill_price, total, filled)
                                    logging.info(f"\U0001F389 [{symbol}] UP order FILLED! {filled:.0f}/{total:.0f} shares @ ${fill_price:.3f}")
                        except Exception as e:
                            log_throttled("fill_check_up", str(e))

                    # Check DOWN order - use get_order_with_avg_price for accurate fill price
                    if down_id:
                        try:
                            status, filled, total, avg_price = executor.get_order_with_avg_price(down_id)
                            if status in ["FILLED", "MATCHED"] or (filled > 0 and filled >= total * 0.99):
                                # Use actual avg_price, fallback to sniper_price only if 0
                                fill_price = avg_price if avg_price > 0 else sniper_config.sniper_price
                                price_source = "api" if avg_price > 0 else "fallback"

                                # Update settlement and check if should notify (anti-spam)
                                should_notify = update_fill_and_notify_status(
                                    slug, "DOWN", filled, fill_price, price_source
                                )

                                if should_notify:
                                    notify_fill(symbol, "DOWN", fill_price, total, filled)
                                    logging.info(f"\U0001F389 [{symbol}] DOWN order FILLED! {filled:.0f}/{total:.0f} shares @ ${fill_price:.3f}")
                        except Exception as e:
                            log_throttled("fill_check_down", str(e))

                    # Small delay between checking orders
                    time.sleep(0.5)

                # Wait before next check cycle
                time.sleep(10)

            except Exception as e:
                log_throttled("fill_monitor", str(e))
                time.sleep(30)

        logging.info("\U0001F50D Fill monitor stopped")

    def _extract_symbol_from_slug(self, slug: str) -> str:
        """
        Extract market symbol from slug.

        Examples:
            'btc-updown-15m-1234567890' -> 'BTC'
            'eth-updown-15m-1234567890' -> 'ETH'
            'custom-btc-market-1234'    -> 'BTC' (fallback to symbol in slug)

        Strategy:
            1. First try matching SUPPORTED_MARKETS prefixes
            2. Fallback: check if any symbol name appears in slug
            3. Last resort: return first 3-4 letter segment as uppercase
        """
        slug_lower = slug.lower()

        # Strategy 1: Match known prefixes
        for market in SUPPORTED_MARKETS:
            if market["prefix"].lower() in slug_lower:
                return market["symbol"]

        # Strategy 2: Check if symbol name appears anywhere in slug
        for market in SUPPORTED_MARKETS:
            symbol_lower = market["symbol"].lower()
            if symbol_lower in slug_lower:
                return market["symbol"]

        # Strategy 3: Extract first segment that looks like a symbol (3-4 letters)
        # e.g., "doge-updown-15m-xxx" -> "DOGE"
        parts = slug.split("-")
        for part in parts:
            if 2 <= len(part) <= 5 and part.isalpha():
                return part.upper()

        return "UNKNOWN"

    def _settlement_watcher_worker(self):
        """
        Settlement Watcher - monitors markets after expiry for resolution.

        Strategy (based on Polymarket best practices):
        1. Wait 30-60s after market expiry before checking
        2. Poll market resolution status with increasing intervals
        3. When resolved, determine winner and calculate PnL
        4. Notify user and update statistics

        IMPROVED:
        - Uses cached fills from settlement record (updated by fill_monitor)
        - Only fetches fresh fills if cached data is empty
        - Uses stored symbol from settlement
        - Better redeem verification with detailed logging
        """
        logging.info("\U0001F4CA Settlement watcher started")

        # Track polling state per market
        poll_state = {}  # {slug: {"next_check": timestamp, "attempts": int}}

        while self._running and not should_stop():
            try:
                executor = get_trade_manager()
                if not executor or not executor.ready:
                    time.sleep(10)
                    continue

                now = time.time()
                pending = get_pending_settlements()

                for slug, settlement in pending.items():
                    if should_stop():
                        break

                    # Skip if already resolved
                    if settlement.get("status") != "pending":
                        continue

                    expiry_ts = settlement.get("expiry_ts", 0)

                    # Initialize poll state if needed
                    if slug not in poll_state:
                        # Start polling 45 seconds after expiry
                        poll_state[slug] = {
                            "next_check": expiry_ts + 45,
                            "attempts": 0,
                            "interval": 15  # Start with 15s interval
                        }

                    state = poll_state[slug]

                    # Skip if not time to check yet
                    if now < state["next_check"]:
                        continue

                    # Check market resolution
                    is_resolved, winner_outcome, winner_token = executor.get_market_resolution(slug)

                    if is_resolved and winner_outcome:
                        # Market resolved! Calculate PnL
                        # Use stored symbol (or fallback to extraction)
                        symbol = settlement.get("symbol", "UNKNOWN")
                        if symbol == "UNKNOWN":
                            symbol = self._extract_symbol_from_slug(slug)

                        # === USE CACHED FILLS FROM SETTLEMENT (updated by fill_monitor) ===
                        up_filled = settlement.get("up_filled", 0.0)
                        down_filled = settlement.get("down_filled", 0.0)
                        up_price = settlement.get("up_avg_price", sniper_config.sniper_price)
                        down_price = settlement.get("down_avg_price", sniper_config.sniper_price)
                        up_price_source = settlement.get("up_price_source", "cached")
                        down_price_source = settlement.get("down_price_source", "cached")

                        # Only fetch fresh fills if cached data is empty (fill_monitor may not have run)
                        if up_filled == 0 and down_filled == 0:
                            up_order_id = settlement.get("up_order_id")
                            down_order_id = settlement.get("down_order_id")

                            if up_order_id:
                                fresh_up_filled, fresh_up_price = executor.get_fills_for_order(up_order_id)
                                if fresh_up_filled > 0:
                                    up_filled = fresh_up_filled
                                    if fresh_up_price > 0:
                                        up_price = fresh_up_price
                                        up_price_source = "api"

                            if down_order_id:
                                fresh_down_filled, fresh_down_price = executor.get_fills_for_order(down_order_id)
                                if fresh_down_filled > 0:
                                    down_filled = fresh_down_filled
                                    if fresh_down_price > 0:
                                        down_price = fresh_down_price
                                        down_price_source = "api"

                            # Update settlement with fetched data
                            update_settlement_fills(slug, up_filled, down_filled, up_price, down_price,
                                                   up_price_source, down_price_source)

                        # Calculate PnL
                        # Winner token pays $1.00, loser token pays $0.00
                        # PnL = (winner_qty * (1.00 - avg_price)) - (loser_qty * avg_price)
                        pnl = 0.0
                        if winner_outcome == "UP":
                            # UP wins: profit from UP position, loss from DOWN position
                            pnl = (up_filled * (1.0 - up_price)) - (down_filled * down_price)
                        elif winner_outcome == "DOWN":
                            # DOWN wins: profit from DOWN position, loss from UP position
                            pnl = (down_filled * (1.0 - down_price)) - (up_filled * up_price)

                        # Mark as resolved
                        mark_settlement_resolved(slug, winner_outcome, pnl)

                        # Log with price source indicator
                        price_info = f"[UP:{up_price_source}, DOWN:{down_price_source}]"

                        logging.info(f"\U0001F4CA [{symbol}] RESOLVED: Winner={winner_outcome}, "
                                    f"UP={up_filled:.0f}@${up_price:.3f}, DOWN={down_filled:.0f}@${down_price:.3f}, "
                                    f"PnL=${pnl:+.2f} {price_info}")

                        # Notify via Telegram
                        notify_settlement_result(
                            symbol=symbol,
                            slug=slug,
                            winner=winner_outcome,
                            up_filled=up_filled,
                            down_filled=down_filled,
                            up_cost=up_price,
                            down_cost=down_price,
                            pnl=pnl
                        )

                        # === REDEEM WINNING TOKENS ===
                        # Only attempt redeem if we have WINNING shares
                        condition_id = settlement.get("condition_id")
                        has_winning_shares = False
                        winning_filled = 0.0

                        if winner_outcome == "UP" and up_filled > 0:
                            has_winning_shares = True
                            winning_filled = up_filled
                        elif winner_outcome == "DOWN" and down_filled > 0:
                            has_winning_shares = True
                            winning_filled = down_filled

                        if condition_id and has_winning_shares:
                            logging.info(f"[{symbol}] Attempting to redeem {winning_filled:.0f} winning {winner_outcome} shares...")
                            redeem_success, amount_redeemed = executor.redeem_positions(condition_id)

                            if redeem_success:
                                mark_settlement_redeemed(slug)
                                if amount_redeemed > 0:
                                    logging.info(f"\U0001F4B0 [{symbol}] Redeemed ${amount_redeemed:.2f}")
                                    notify_redeem_success(symbol, slug, amount_redeemed)
                                else:
                                    logging.info(f"[{symbol}] Redeem completed - may already be redeemed or pending")
                            else:
                                logging.warning(f"[{symbol}] Redeem failed - may need manual redeem via Polymarket UI")
                        else:
                            # No winning positions to redeem - mark as done
                            mark_settlement_redeemed(slug)
                            if up_filled == 0 and down_filled == 0:
                                logging.info(f"[{symbol}] No positions to redeem (no fills)")
                            else:
                                # We have positions but on the LOSING side - no redeem needed
                                losing_side = "UP" if winner_outcome == "DOWN" else "DOWN"
                                losing_filled = up_filled if winner_outcome == "DOWN" else down_filled
                                logging.info(f"[{symbol}] No winning shares to redeem ({losing_filled:.0f} {losing_side} shares lost, ${losing_filled * 0.01:.2f} cost)")

                        # Clean up poll state
                        del poll_state[slug]

                    else:
                        # Not resolved yet, schedule next check with backoff
                        state["attempts"] += 1

                        # Increase interval with backoff (max 60s)
                        if state["attempts"] > 5:
                            state["interval"] = min(60, state["interval"] * 1.5)

                        state["next_check"] = now + state["interval"]

                        if state["attempts"] <= 3 or state["attempts"] % 10 == 0:
                            logging.debug(f"[{slug[-20:]}] Waiting for resolution... (attempt {state['attempts']})")

                        # Give up after 30 minutes (market should resolve much faster)
                        if state["attempts"] > 120:
                            logging.warning(f"[{slug[-20:]}] Giving up on resolution after {state['attempts']} attempts")
                            del poll_state[slug]

                # Cleanup old settlements periodically
                cleanup_old_settlements()

                # Wait before next cycle
                time.sleep(5)

            except Exception as e:
                log_throttled("settlement_watcher", str(e))
                time.sleep(30)

        logging.info("\U0001F4CA Settlement watcher stopped")

    def get_status(self) -> Dict:
        """Get status for UI display"""
        with data_lock:
            settlement_stats = get_settlement_stats()
            return {
                "running": self._running,
                "orders_placed": shared_data.get("total_orders_placed", 0),
                "fills": shared_data.get("total_fills", 0),
                "active_workers": len([w for w in self._workers.values() if w.is_alive()]),
                # Settlement stats
                "wins": settlement_stats.get("total_wins", 0),
                "losses": settlement_stats.get("total_losses", 0),
                "total_pnl": settlement_stats.get("total_pnl", 0.0),
                "pending_settlements": settlement_stats.get("pending_count", 0),
            }


# ==================== GLOBAL INSTANCE ====================
_sniper_instance: Optional[EndgameSniper] = None


def get_sniper() -> EndgameSniper:
    """Get or create EndgameSniper instance"""
    global _sniper_instance
    if _sniper_instance is None:
        _sniper_instance = EndgameSniper()
    return _sniper_instance


def start_sniper():
    """Start the sniper"""
    sniper = get_sniper()
    sniper.start()


def stop_sniper():
    """Stop the sniper"""
    global _sniper_instance
    if _sniper_instance:
        _sniper_instance.stop()
