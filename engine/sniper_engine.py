"""
Endgame Sniper Bot - Core Sniper Engine
Main trading engine with:
- 3-tier polling system with jitter
- Optimized settlement watcher (expiry-based)
- Adaptive fill monitor
- Support for both Headless and UI modes
"""
import time
import logging
import threading
import json
import random
import pytz
from datetime import datetime, timedelta
from dateutil import parser
from typing import Optional, Dict, List

from engine.config_schema import (
    get_config,
    SniperConfigSchema,
    PollingTier,
    SUPPORTED_MARKETS,
)
from engine.state_manager import (
    get_state_manager,
    StateManager,
    RunMode,
)
from engine.trade_executor import (
    get_trade_executor,
    TradeExecutor,
    GLOBAL_SESSION,
)
from engine.notifier import get_notifier, NotificationManager
from engine.persistence import get_persistence_manager
from engine.logging_config import log_throttled


# ==================== CONSTANTS ====================
GAMMA_API = "https://gamma-api.polymarket.com"


def robust_api_get(url: str, timeout: int = 10, max_retries: int = 2):
    """Make API request with retry"""
    for attempt in range(max_retries):
        try:
            resp = GLOBAL_SESSION.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if attempt == max_retries - 1:
                logging.debug(f"API returned status {resp.status_code}: {url[:50]}...")
        except Exception as e:
            if attempt == max_retries - 1:
                logging.debug(f"API request failed: {str(e)[:50]}")
            time.sleep(0.5 * (attempt + 1))
    return None


class SniperEngine:
    """
    Core Sniper Engine with optimizations for VPS headless mode.

    Features:
    - 3-tier polling system (far/near/sniper) with jitter
    - Settlement watcher based on expiry timestamp
    - Adaptive fill monitoring
    - Thread-safe state management
    """

    def __init__(self, mode: RunMode = RunMode.HEADLESS):
        self.mode = mode
        self._lock = threading.Lock()
        self._workers: Dict[str, threading.Thread] = {}
        self._running = False

        # Background workers
        self._cleanup_thread: Optional[threading.Thread] = None
        self._fill_monitor_thread: Optional[threading.Thread] = None
        self._settlement_thread: Optional[threading.Thread] = None

        # Components
        self._state: StateManager = get_state_manager(mode)
        self._notifier: NotificationManager = get_notifier()
        self._persistence = get_persistence_manager()

        # Initialize market status
        self._state.init_market_status([m["symbol"] for m in SUPPORTED_MARKETS])

        logging.info(f"SniperEngine initialized (mode: {mode.value})")

    def start(self):
        """Start the sniper engine"""
        if self._running:
            return

        self._running = True
        self._state.set_running(True)
        self._state.clear_stop()

        config = get_config()

        # Take snapshot of current market enabled states for restart detection
        config.snapshot_markets_for_engine()

        # Start API cache
        from engine.api_cache import get_api_cache
        get_api_cache().start()

        # Start market workers only for enabled markets
        # Stagger starts to avoid thundering herd
        enabled_count = 0
        for idx, market in enumerate(SUPPORTED_MARKETS):
            symbol = market["symbol"]
            prefix = market["prefix"]

            # Skip disabled markets to save threads
            if not config.is_market_enabled(symbol):
                logging.info(f"[{symbol}] Market disabled, skipping worker")
                self._state.set_market_status(symbol, "status", "Disabled")
                continue

            worker = threading.Thread(
                target=self._market_worker,
                args=(symbol, prefix, idx),  # Pass index for initial jitter
                daemon=True,
                name=f"Sniper-{symbol}"
            )
            self._workers[symbol] = worker
            worker.start()
            enabled_count += 1

            # Stagger worker starts by 0.5-1.5 seconds
            if idx < len(SUPPORTED_MARKETS) - 1:
                time.sleep(random.uniform(0.5, 1.5))

        # Start cleanup worker
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            daemon=True,
            name="Sniper-Cleanup"
        )
        self._cleanup_thread.start()

        # Start fill monitor
        self._fill_monitor_thread = threading.Thread(
            target=self._fill_monitor_worker,
            daemon=True,
            name="Sniper-FillMonitor"
        )
        self._fill_monitor_thread.start()

        # Start settlement watcher
        self._settlement_thread = threading.Thread(
            target=self._settlement_watcher_worker,
            daemon=True,
            name="Sniper-Settlement"
        )
        self._settlement_thread.start()

        # Start persistence auto-save
        self._persistence.start_auto_save()

        logging.info(f"Sniper Engine STARTED for {enabled_count}/{len(SUPPORTED_MARKETS)} enabled markets")

    def stop(self):
        """Stop the sniper engine"""
        self._running = False
        self._state.request_stop()
        self._workers.clear()
        self._persistence.stop_auto_save()

        # Final state save
        from engine.persistence import save_state_now
        save_state_now()

        logging.info("Sniper Engine STOPPED")

    def is_running(self) -> bool:
        """Check if engine is running"""
        return self._running

    # ==================== 3-TIER POLLING ====================

    def _get_polling_tier(self, time_to_expiry: float) -> PollingTier:
        """Determine polling tier based on time to expiry"""
        config = get_config()

        if time_to_expiry > config.polling.far_threshold:
            return PollingTier.FAR
        elif time_to_expiry > config.polling.near_threshold:
            return PollingTier.NEAR
        else:
            return PollingTier.SNIPER

    def _get_polling_interval(self, tier: PollingTier) -> float:
        """Get polling interval with jitter for a tier"""
        config = get_config()
        p = config.polling

        if tier == PollingTier.FAR:
            base = random.uniform(p.far_min_interval, p.far_max_interval)
        elif tier == PollingTier.NEAR:
            base = random.uniform(p.near_min_interval, p.near_max_interval)
        else:  # SNIPER
            base = random.uniform(p.sniper_min_interval, p.sniper_max_interval)

        # Apply jitter (±jitter_percent)
        jitter = base * p.jitter_percent
        return base + random.uniform(-jitter, jitter)

    # ==================== MARKET DETECTION ====================

    def _get_current_market(self, prefix: str) -> Optional[Dict]:
        """Find the current active market (with expiry info)"""
        try:
            config = get_config()
            now_dt = datetime.now(pytz.UTC)
            now_ts = time.time()

            # Calculate 15-minute windows
            floored = (now_dt.minute // 15) * 15
            start = now_dt.replace(minute=floored, second=0, microsecond=0)

            # Check current and next windows
            slugs = [
                f"{prefix}-{int((start + timedelta(minutes=15*i)).timestamp())}"
                for i in range(2)
            ]

            for slug in slugs:
                try:
                    resp = robust_api_get(f"{GAMMA_API}/markets?slug={slug}")
                    if not resp:
                        continue

                    res = resp.json()
                    if not res or not isinstance(res, list) or len(res) == 0:
                        continue

                    market = res[0]

                    if market.get("closed"):
                        continue

                    # Parse expiry
                    expiry_dt = parser.isoparse(market["endDate"])
                    expiry_ts = expiry_dt.timestamp()
                    time_to_expiry = expiry_ts - now_ts

                    if time_to_expiry <= 0:
                        continue

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

    # ==================== ORDER PLACEMENT ====================

    def _check_and_place_orders(self, symbol: str, market: Dict) -> bool:
        """Check conditions and place orders if met"""
        config = get_config()
        slug = market["slug"]
        token_up = market["token_up"]
        token_down = market["token_down"]
        time_left = market["time_to_expiry"]

        # Try to acquire order slot (atomic)
        if not self._state.try_acquire_order_slot(slug):
            logging.debug(f"[{symbol}] Order slot already taken for {slug}")
            return False

        try:
            executor = get_trade_executor()
            if not executor or not executor.ready:
                self._state.release_order_slot(slug, success=False)
                return False

            # Get best bids
            bid_up = executor.get_best_bid(token_up)
            bid_down = executor.get_best_bid(token_down)

            # Update market status
            self._state.set_market_status(symbol, "up_bid", bid_up)
            self._state.set_market_status(symbol, "down_bid", bid_down)
            self._state.set_market_status(symbol, "last_check", time.time())
            self._state.set_market_status(symbol, "time_to_expiry", time_left)

            trigger_threshold = config.trigger_threshold
            sniper_price = config.sniper_price
            order_size = config.order_size_shares

            logging.debug(f"[{symbol}] Check: UP={bid_up:.2f}, DOWN={bid_down:.2f}, threshold={trigger_threshold:.2f}, {time_left:.0f}s")

            # Check trigger condition
            if bid_up > trigger_threshold and bid_down > trigger_threshold:
                logging.info(f"[{symbol}] TRIGGER! UP={bid_up:.2f} DOWN={bid_down:.2f} > {trigger_threshold:.2f}")
                self._state.set_market_status(symbol, "status", "TRIGGERED!")

                # Place UP order
                up_order_id = executor.place_buy_order(token_up, sniper_price, order_size)
                self._notifier.notify_order_placed(
                    symbol, "UP", sniper_price, order_size, up_order_id,
                    bid_up, bid_down, time_left
                )

                # Delay between UP and DOWN to avoid Cloudflare rate limiting
                time.sleep(0.5 + random.uniform(0, 0.3))

                # Place DOWN order
                down_order_id = executor.place_buy_order(token_down, sniper_price, order_size)
                self._notifier.notify_order_placed(
                    symbol, "DOWN", sniper_price, order_size, down_order_id,
                    bid_up, bid_down, time_left
                )

                # Log results
                up_status = f"OK {up_order_id[:16]}..." if up_order_id else "FAILED"
                down_status = f"OK {down_order_id[:16]}..." if down_order_id else "FAILED"
                logging.info(f"[{symbol}] Sniper orders: UP={up_status}, DOWN={down_status}")

                # Handle partial success - retry failed order once (with longer delay for Cloudflare)
                if up_order_id and not down_order_id:
                    logging.warning(f"[{symbol}] UP ok but DOWN failed. Retrying DOWN...")
                    time.sleep(1.0 + random.uniform(0, 0.5))  # Longer delay to avoid Cloudflare
                    down_order_id = executor.place_buy_order(token_down, sniper_price, order_size)
                    if down_order_id:
                        self._notifier.notify_order_placed(
                            symbol, "DOWN", sniper_price, order_size, down_order_id,
                            bid_up, bid_down, time_left
                        )

                elif down_order_id and not up_order_id:
                    logging.warning(f"[{symbol}] DOWN ok but UP failed. Retrying UP...")
                    time.sleep(1.0 + random.uniform(0, 0.5))  # Longer delay to avoid Cloudflare
                    up_order_id = executor.place_buy_order(token_up, sniper_price, order_size)
                    if up_order_id:
                        self._notifier.notify_order_placed(
                            symbol, "UP", sniper_price, order_size, up_order_id,
                            bid_up, bid_down, time_left
                        )

                # Record orders if at least one succeeded
                if up_order_id or down_order_id:
                    self._state.record_order_placed(slug, up_order_id, down_order_id)
                    self._state.set_market_status(symbol, "status", f"Orders @ ${sniper_price}")

                    # Record for settlement
                    condition_id = market.get("condition_id")
                    expiry_ts = market.get("expiry", 0)
                    self._state.record_pending_settlement(
                        slug=slug,
                        condition_id=condition_id,
                        expiry_ts=expiry_ts,
                        token_up=token_up,
                        token_down=token_down,
                        up_order_id=up_order_id,
                        down_order_id=down_order_id,
                        sniper_price=sniper_price,
                        symbol=symbol
                    )

                    # Mark persistence dirty
                    self._persistence.mark_dirty()

                    return True
                else:
                    logging.error(f"[{symbol}] Both orders failed!")
                    self._state.release_order_slot(slug, success=False)
                    self._state.set_market_status(symbol, "status", "Orders FAILED")
                    return False
            else:
                self._state.release_order_slot(slug, success=False)
                return False

        except Exception as e:
            self._state.release_order_slot(slug, success=False)
            log_throttled(f"check_place_{symbol}", str(e))
            return False

    # ==================== MARKET WORKER ====================

    def _market_worker(self, symbol: str, prefix: str, worker_idx: int = 0):
        """Worker thread for a single market with 3-tier polling"""
        logging.info(f"[{symbol}] Sniper worker started (idx={worker_idx})")

        last_check_slug = None

        # Add initial jitter based on worker index to spread out first checks
        initial_jitter = worker_idx * 0.3 + random.uniform(0, 0.5)
        time.sleep(initial_jitter)

        while self._running and not self._state.should_stop():
            try:
                config = get_config()

                # Check if market is enabled
                if not config.is_market_enabled(symbol):
                    self._state.set_market_status(symbol, "status", "Disabled")
                    time.sleep(config.check_interval_seconds)
                    continue

                self._state.set_market_status(symbol, "status", "Scanning...")

                # Find market
                market = self._get_current_market(prefix)

                if not market:
                    self._state.set_market_status(symbol, "status", "Waiting")
                    self._state.set_market_status(symbol, "polling_tier", "far")
                    # Far tier polling when no market
                    time.sleep(self._get_polling_interval(PollingTier.FAR))
                    continue

                slug = market["slug"]
                time_left = market["time_to_expiry"]

                # Determine polling tier
                tier = self._get_polling_tier(time_left)
                self._state.set_market_status(symbol, "polling_tier", tier.value)

                # Check if within trigger window
                trigger_window = config.trigger_minutes_before_expiry * 60

                if time_left > trigger_window:
                    # Not yet in trigger window
                    self._state.set_market_status(symbol, "status", f"Wait {time_left:.0f}s")
                    self._state.set_market_status(symbol, "current_market", slug[-20:])
                    time.sleep(self._get_polling_interval(tier))
                    continue

                # In trigger window
                self._state.set_market_status(symbol, "current_market", slug[-20:])

                # Log when entering window (once per market)
                if slug != last_check_slug:
                    logging.info(f"[{symbol}] Entering trigger window: {slug[-20:]} ({time_left:.0f}s left)")
                    last_check_slug = slug
                    self._state.set_market_status(symbol, "status", f"In window ({time_left:.0f}s)")

                # Skip if already placed
                if self._state.is_order_placed_for_market(slug):
                    self._state.set_market_status(symbol, "status", "Orders placed")
                    time.sleep(self._get_polling_interval(tier))
                    continue

                # Check and place orders
                self._check_and_place_orders(symbol, market)

                # Wait with tier-appropriate interval
                time.sleep(self._get_polling_interval(tier))

            except Exception as e:
                log_throttled(f"sniper_{symbol}", str(e))
                self._state.set_market_status(symbol, "status", f"Error: {str(e)[:20]}")
                time.sleep(5)

        logging.info(f"[{symbol}] Sniper worker stopped")

    # ==================== ADAPTIVE FILL MONITOR ====================

    def _fill_monitor_worker(self):
        """
        Adaptive fill monitor:
        - Dense polling 0-120s after order placed
        - Sparse polling after
        - Notify once per side
        - Rate limited to prevent API burst
        """
        logging.info("Fill monitor started")
        config = get_config()
        fm_config = config.fill_monitor

        # Track order placement time and last check time
        order_times: Dict[str, float] = {}
        last_check_times: Dict[str, float] = {}

        # Get rate limiter
        from engine.api_cache import get_api_cache
        api_cache = get_api_cache()

        while self._running and not self._state.should_stop():
            try:
                executor = get_trade_executor()
                if not executor or not executor.ready:
                    time.sleep(10)
                    continue

                order_ids = self._state.get_order_ids()
                now = time.time()

                # Limit how many orders we check per cycle to prevent burst
                orders_checked_this_cycle = 0
                max_orders_per_cycle = 10  # Prevent API burst

                # Shuffle order list each cycle for fairness (prevent starvation)
                order_items = list(order_ids.items())
                random.shuffle(order_items)

                for slug, orders in order_items:
                    if self._state.should_stop():
                        break

                    # Limit orders per cycle
                    if orders_checked_this_cycle >= max_orders_per_cycle:
                        break

                    up_id = orders.get("up")
                    down_id = orders.get("down")

                    # Track when we first saw this order
                    if slug not in order_times:
                        order_times[slug] = now

                    order_age = now - order_times[slug]

                    # Skip if too old
                    if order_age > fm_config.max_monitor_duration:
                        continue

                    # Determine polling phase and interval
                    if order_age <= fm_config.dense_duration:
                        # Dense phase
                        interval = random.uniform(fm_config.dense_min_interval, fm_config.dense_max_interval)
                    else:
                        # Sparse phase
                        interval = random.uniform(fm_config.sparse_min_interval, fm_config.sparse_max_interval)

                    # Check if enough time has passed since last check for this slug
                    last_check = last_check_times.get(slug, 0)
                    if now - last_check < interval:
                        continue

                    # Acquire rate limit before API calls
                    if not api_cache.acquire_rate_limit("clob.polymarket.com", timeout=2.0):
                        logging.debug("Fill monitor rate limited, waiting...")
                        time.sleep(0.5)
                        continue

                    symbol = self._state.get_settlement_symbol(slug)
                    last_check_times[slug] = now
                    orders_checked_this_cycle += 1

                    # Check UP order
                    if up_id:
                        try:
                            status, filled, total, avg_price = executor.get_order_with_avg_price(up_id)
                            if status in ["FILLED", "MATCHED"] or (filled > 0 and filled >= total * 0.99):
                                fill_price = avg_price if avg_price > 0 else config.sniper_price
                                price_source = "api" if avg_price > 0 else "fallback"

                                should_notify = self._state.update_settlement_fills(
                                    slug, "UP", filled, fill_price, price_source
                                )

                                if should_notify:
                                    self._state.record_fill()
                                    self._notifier.notify_fill(symbol, "UP", fill_price, total, filled)
                                    logging.info(f"[{symbol}] UP FILLED! {filled:.0f}/{total:.0f} @ ${fill_price:.3f}")
                                    self._persistence.mark_dirty()
                        except Exception as e:
                            log_throttled("fill_up", str(e))

                    # Small delay between UP and DOWN check
                    time.sleep(0.1)

                    # Check DOWN order
                    if down_id:
                        try:
                            status, filled, total, avg_price = executor.get_order_with_avg_price(down_id)
                            if status in ["FILLED", "MATCHED"] or (filled > 0 and filled >= total * 0.99):
                                fill_price = avg_price if avg_price > 0 else config.sniper_price
                                price_source = "api" if avg_price > 0 else "fallback"

                                should_notify = self._state.update_settlement_fills(
                                    slug, "DOWN", filled, fill_price, price_source
                                )

                                if should_notify:
                                    self._state.record_fill()
                                    self._notifier.notify_fill(symbol, "DOWN", fill_price, total, filled)
                                    logging.info(f"[{symbol}] DOWN FILLED! {filled:.0f}/{total:.0f} @ ${fill_price:.3f}")
                                    self._persistence.mark_dirty()
                        except Exception as e:
                            log_throttled("fill_down", str(e))

                    # Delay between slugs to spread out API calls
                    time.sleep(0.3)

                # Cleanup old order times and last check times
                old_slugs = [s for s, t in order_times.items() if now - t > fm_config.max_monitor_duration + 60]
                for s in old_slugs:
                    del order_times[s]
                    if s in last_check_times:
                        del last_check_times[s]

                # Base sleep
                time.sleep(5)

            except Exception as e:
                log_throttled("fill_monitor", str(e))
                time.sleep(30)

        logging.info("Fill monitor stopped")

    # ==================== SETTLEMENT WATCHER (EXPIRY-BASED) ====================

    def _settlement_watcher_worker(self):
        """
        Settlement watcher optimized for VPS:
        - Don't poll before expiry
        - next_check = expiry_ts + 8-15s
        - Backoff cap 60s
        - Give up after 30 minutes + notify once
        - Stale state after max_attempts (reduced polling)
        - Abandoned state after stale_max_age_hours
        """
        logging.info("Settlement watcher started")

        while self._running and not self._state.should_stop():
            try:
                config = get_config()
                s_config = config.settlement

                executor = get_trade_executor()
                if not executor or not executor.ready:
                    time.sleep(10)
                    continue

                now = time.time()
                pending = self._state.get_pending_settlements()

                for slug, settlement in pending.items():
                    if self._state.should_stop():
                        break

                    # Skip if already resolved or abandoned
                    if settlement.status in ["resolved", "redeemed", "abandoned"]:
                        continue

                    symbol = settlement.symbol
                    expiry_ts = settlement.expiry_ts

                    # Calculate time since expiry
                    time_since_expiry = now - expiry_ts

                    # Before expiry - schedule first poll time and skip
                    if time_since_expiry < 0:
                        # Schedule first poll if not already scheduled
                        if settlement.poll_attempts == 0 and settlement.last_poll_time == 0:
                            # Set scheduled first poll time: expiry + random initial delay
                            scheduled_time = expiry_ts + random.uniform(
                                s_config.initial_delay_min, s_config.initial_delay_max
                            )
                            self._schedule_first_poll(slug, scheduled_time)
                            logging.debug(f"[{symbol}] Scheduled first poll for {scheduled_time - now:.1f}s from now")
                        continue

                    # Check if should transition to stale
                    if settlement.status == "pending":
                        if settlement.poll_attempts >= s_config.max_attempts:
                            self._mark_settlement_stale(slug, settlement)
                            logging.warning(f"[{symbol}] Settlement marked STALE after {settlement.poll_attempts} attempts")
                            continue
                        elif time_since_expiry > s_config.max_wait_minutes * 60:
                            # Timeout - notify and mark stale
                            if not settlement.timeout_notified:
                                logging.warning(f"[{symbol}] Settlement timeout after {settlement.poll_attempts} attempts")
                                if s_config.notify_once_on_timeout:
                                    self._notifier.notify_settlement_timeout(symbol, slug, settlement.poll_attempts)
                                self._mark_settlement_timeout_notified(slug)
                            self._mark_settlement_stale(slug, settlement)
                            continue

                    # Check if stale should become abandoned
                    if settlement.status == "stale":
                        age_hours = (now - settlement.created_at) / 3600
                        if age_hours > s_config.stale_max_age_hours:
                            self._mark_settlement_abandoned(slug)
                            logging.info(f"[{symbol}] Settlement marked ABANDONED after {age_hours:.1f}h")
                            continue

                    # Determine poll interval based on status
                    if settlement.status == "stale":
                        poll_interval = s_config.stale_check_interval
                    elif settlement.poll_attempts == 0:
                        # First poll - use scheduled time if available
                        if settlement.last_poll_time > 0:
                            # We have a scheduled first poll time
                            if now < settlement.last_poll_time:
                                continue  # Not yet time for first poll
                            # Time for first poll - proceed
                            poll_interval = 0  # Will poll now
                        else:
                            # No scheduled time - use initial delay from now
                            poll_interval = random.uniform(s_config.initial_delay_min, s_config.initial_delay_max)
                    elif settlement.poll_attempts <= 5:
                        poll_interval = s_config.min_interval
                    else:
                        # Exponential backoff capped at max_interval
                        poll_interval = min(s_config.max_interval,
                                          s_config.min_interval * (s_config.backoff_multiplier ** (settlement.poll_attempts - 5)))

                    # Check if it's time to poll (skip for first poll with scheduled time)
                    if settlement.poll_attempts > 0 or settlement.last_poll_time == 0:
                        next_poll_time = settlement.last_poll_time + poll_interval
                        if settlement.last_poll_time > 0 and now < next_poll_time:
                            continue

                    # Record poll attempt
                    self._record_poll_attempt(slug, now)

                    # Check resolution
                    is_resolved, winner_outcome, winner_token = executor.get_market_resolution(slug)

                    if is_resolved and winner_outcome:
                        # Market resolved!
                        self._handle_settlement_resolved(executor, slug, settlement, winner_outcome)
                    else:
                        # Log progress for stale (reduced frequency)
                        if settlement.status == "stale" and settlement.poll_attempts % 12 == 0:
                            logging.debug(f"[{symbol}] Stale settlement still unresolved after {settlement.poll_attempts} attempts")

                # Cleanup old settlements (abandoned + old redeemed)
                self._cleanup_old_settlements(s_config.cleanup_abandoned_hours)

                time.sleep(5)

            except Exception as e:
                log_throttled("settlement_watcher", str(e))
                time.sleep(30)

        logging.info("Settlement watcher stopped")

    def _mark_settlement_stale(self, slug: str, settlement):
        """Mark settlement as stale (reduced polling) - uses StateManager for atomicity"""
        if self._state.mark_settlement_stale(slug):
            self._persistence.mark_dirty()

    def _mark_settlement_timeout_notified(self, slug: str):
        """Mark that timeout notification was sent - uses StateManager"""
        self._state.mark_settlement_timeout_notified(slug)

    def _mark_settlement_abandoned(self, slug: str):
        """Mark settlement as abandoned (stop polling) - uses StateManager"""
        if self._state.mark_settlement_abandoned(slug):
            self._persistence.mark_dirty()

    def _schedule_first_poll(self, slug: str, scheduled_time: float):
        """Schedule the first poll time for a settlement (before expiry)"""
        self._state.schedule_settlement_first_poll(slug, scheduled_time)

    def _record_poll_attempt(self, slug: str, timestamp: float):
        """Record a poll attempt for a settlement"""
        self._state.record_settlement_poll_attempt(slug, timestamp)

    def _handle_settlement_resolved(self, executor, slug: str, settlement, winner_outcome: str):
        """Handle a resolved settlement"""
        symbol = settlement.symbol

        # Get fills
        up_filled = settlement.up_filled
        down_filled = settlement.down_filled
        up_price = settlement.up_avg_price
        down_price = settlement.down_avg_price

        # Fetch fresh if no fills cached
        if up_filled == 0 and down_filled == 0:
            if settlement.up_order_id:
                fresh_up, fresh_up_price = executor.get_fills_for_order(settlement.up_order_id)
                if fresh_up > 0:
                    up_filled = fresh_up
                    if fresh_up_price > 0:
                        up_price = fresh_up_price

            if settlement.down_order_id:
                fresh_down, fresh_down_price = executor.get_fills_for_order(settlement.down_order_id)
                if fresh_down > 0:
                    down_filled = fresh_down
                    if fresh_down_price > 0:
                        down_price = fresh_down_price

        # Calculate PnL
        if winner_outcome == "UP":
            pnl = (up_filled * (1.0 - up_price)) - (down_filled * down_price)
        elif winner_outcome == "DOWN":
            pnl = (down_filled * (1.0 - down_price)) - (up_filled * up_price)
        else:
            pnl = 0.0

        # Mark resolved
        self._state.mark_settlement_resolved(slug, winner_outcome, pnl)

        logging.info(f"[{symbol}] RESOLVED: Winner={winner_outcome}, "
                    f"UP={up_filled:.0f}@${up_price:.3f}, DOWN={down_filled:.0f}@${down_price:.3f}, "
                    f"PnL=${pnl:+.2f}")

        # Notify
        self._notifier.notify_settlement_result(
            symbol=symbol, slug=slug, winner=winner_outcome,
            up_filled=up_filled, down_filled=down_filled,
            up_cost=up_price, down_cost=down_price, pnl=pnl
        )

        # Redeem if we have winning shares
        condition_id = settlement.condition_id
        has_winning = False
        if winner_outcome == "UP" and up_filled > 0:
            has_winning = True
        elif winner_outcome == "DOWN" and down_filled > 0:
            has_winning = True

        if condition_id and has_winning:
            logging.info(f"[{symbol}] Attempting redeem...")
            success, amount = executor.redeem_positions(condition_id)
            if success:
                self._state.mark_settlement_redeemed(slug)
                if amount > 0:
                    self._notifier.notify_redeem_success(symbol, slug, amount)
                logging.info(f"[{symbol}] Redeemed ${amount:.2f}")
            else:
                logging.warning(f"[{symbol}] Redeem failed - may need manual")
        else:
            self._state.mark_settlement_redeemed(slug)

        self._persistence.mark_dirty()

    def _cleanup_old_settlements(self, cleanup_hours: float):
        """Cleanup old settlements (abandoned + old redeemed) - uses StateManager"""
        cleaned = self._state.cleanup_old_settlements(max_age_seconds=cleanup_hours * 3600)
        if cleaned > 0:
            logging.debug(f"Cleaned up {cleaned} old settlement(s)")

    def _cleanup_worker(self):
        """Cleanup worker for old records"""
        while self._running and not self._state.should_stop():
            try:
                self._state.cleanup_old_orders()
                time.sleep(300)
            except Exception:
                time.sleep(60)

    def _extract_symbol_from_slug(self, slug: str) -> str:
        """Extract market symbol from slug"""
        slug_lower = slug.lower()

        for market in SUPPORTED_MARKETS:
            if market["prefix"].lower() in slug_lower:
                return market["symbol"]

        for market in SUPPORTED_MARKETS:
            if market["symbol"].lower() in slug_lower:
                return market["symbol"]

        parts = slug.split("-")
        for part in parts:
            if 2 <= len(part) <= 5 and part.isalpha():
                return part.upper()

        return "UNKNOWN"

    def get_status(self) -> Dict:
        """Get engine status for UI"""
        stats = self._state.get_settlement_stats()
        return {
            "running": self._running,
            "orders_placed": self._state._state.get("total_orders_placed", 0),
            "fills": self._state.get_fill_count(),
            "active_workers": len([w for w in self._workers.values() if w.is_alive()]),
            "wins": stats.get("total_wins", 0),
            "losses": stats.get("total_losses", 0),
            "total_pnl": stats.get("total_pnl", 0.0),
            "pending_settlements": stats.get("pending_count", 0),
        }


# ==================== GLOBAL INSTANCE ====================
_sniper_engine: Optional[SniperEngine] = None
_engine_lock = threading.Lock()


def get_sniper_engine(mode: RunMode = None) -> SniperEngine:
    """Get or create sniper engine"""
    global _sniper_engine
    with _engine_lock:
        if _sniper_engine is None:
            _sniper_engine = SniperEngine(mode=mode or RunMode.HEADLESS)
        return _sniper_engine


def start_engine():
    """Start the sniper engine"""
    engine = get_sniper_engine()
    engine.start()


def stop_engine():
    """Stop the sniper engine"""
    global _sniper_engine
    if _sniper_engine:
        _sniper_engine.stop()
