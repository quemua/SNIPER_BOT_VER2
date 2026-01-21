"""
Endgame Sniper Bot - API Cache & Rate Limiter
Optimizes API calls for VPS operation:
- Cache API results (0.5-2s TTL)
- Rate limiter per host
- Request budget management
"""
import time
import threading
from typing import Optional, Any, Dict, Tuple
from functools import wraps
import logging


class TTLCache:
    """
    Simple TTL cache for API responses.
    Thread-safe with configurable TTL per key.
    """

    def __init__(self, default_ttl: float = 1.0):
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Tuple[bool, Any]:
        """
        Get value from cache.
        Returns (hit, value) - hit is True if cache hit, False if miss/expired.
        """
        with self._lock:
            if key in self._cache:
                value, expires_at = self._cache[key]
                if time.time() < expires_at:
                    self._hits += 1
                    return True, value
                else:
                    del self._cache[key]

            self._misses += 1
            return False, None

    def set(self, key: str, value: Any, ttl: float = None):
        """Set value in cache with TTL"""
        with self._lock:
            expires_at = time.time() + (ttl or self.default_ttl)
            self._cache[key] = (value, expires_at)

    def delete(self, key: str):
        """Delete a key from cache"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]

    def clear(self):
        """Clear all cache entries"""
        with self._lock:
            self._cache.clear()

    def cleanup(self):
        """Remove expired entries"""
        now = time.time()
        with self._lock:
            expired = [k for k, (_, exp) in self._cache.items() if now >= exp]
            for k in expired:
                del self._cache[k]

    def stats(self) -> Dict[str, int]:
        """Get cache statistics"""
        with self._lock:
            total = self._hits + self._misses
            hit_rate = (self._hits / total * 100) if total > 0 else 0
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "size": len(self._cache),
            }


class HostRateLimiter:
    """
    Rate limiter per host.
    Prevents burst when multiple markets enter SNIPER tier simultaneously.
    """

    def __init__(self, requests_per_second: float = 5.0, burst: int = 10):
        self._hosts: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self.default_rps = requests_per_second
        self.default_burst = burst

    def _get_host_state(self, host: str) -> dict:
        """Get or create host state"""
        if host not in self._hosts:
            self._hosts[host] = {
                "tokens": self.default_burst,
                "last_update": time.time(),
                "rps": self.default_rps,
                "burst": self.default_burst,
            }
        return self._hosts[host]

    def acquire(self, host: str, timeout: float = 5.0) -> bool:
        """
        Acquire permission to make a request to host.
        Blocks until permission granted or timeout.
        Returns True if acquired, False if timeout.
        """
        start = time.time()

        while time.time() - start < timeout:
            with self._lock:
                state = self._get_host_state(host)
                now = time.time()

                # Refill tokens based on time elapsed
                elapsed = now - state["last_update"]
                state["tokens"] = min(
                    state["burst"],
                    state["tokens"] + elapsed * state["rps"]
                )
                state["last_update"] = now

                # Check if we can acquire
                if state["tokens"] >= 1.0:
                    state["tokens"] -= 1.0
                    return True

            # Wait a bit before retry
            time.sleep(0.05)

        return False

    def set_host_limits(self, host: str, rps: float, burst: int):
        """Set custom limits for a host"""
        with self._lock:
            state = self._get_host_state(host)
            state["rps"] = rps
            state["burst"] = burst


class APICache:
    """
    Combined cache and rate limiter for API calls.
    """

    def __init__(self):
        # Cache with different TTLs for different data types
        self._market_cache = TTLCache(default_ttl=2.0)  # Market data: 2s
        self._orderbook_cache = TTLCache(default_ttl=0.5)  # Orderbook: 0.5s (more real-time)
        self._order_cache = TTLCache(default_ttl=5.0)  # Order status: 5s

        # Rate limiters per host
        self._rate_limiter = HostRateLimiter(requests_per_second=5.0, burst=10)

        # Configure host-specific limits
        self._rate_limiter.set_host_limits("gamma-api.polymarket.com", rps=10.0, burst=20)
        self._rate_limiter.set_host_limits("clob.polymarket.com", rps=5.0, burst=10)

        # Cleanup thread
        self._cleanup_thread = None
        self._running = False

    def start(self):
        """Start cache cleanup thread"""
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            return

        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            daemon=True,
            name="APICache-Cleanup"
        )
        self._cleanup_thread.start()

    def stop(self):
        """Stop cache cleanup"""
        self._running = False

    def _cleanup_worker(self):
        """Periodically cleanup expired cache entries"""
        while self._running:
            try:
                time.sleep(60)
                self._market_cache.cleanup()
                self._orderbook_cache.cleanup()
                self._order_cache.cleanup()
            except Exception:
                pass

    # ==================== MARKET DATA CACHE ====================

    def get_market(self, slug: str) -> Tuple[bool, Any]:
        """Get cached market data"""
        return self._market_cache.get(f"market:{slug}")

    def set_market(self, slug: str, data: Any, ttl: float = 2.0):
        """Cache market data"""
        self._market_cache.set(f"market:{slug}", data, ttl)

    # ==================== ORDERBOOK CACHE ====================

    def get_orderbook(self, token_id: str) -> Tuple[bool, Any]:
        """Get cached orderbook"""
        return self._orderbook_cache.get(f"book:{token_id}")

    def set_orderbook(self, token_id: str, data: Any, ttl: float = 0.5):
        """Cache orderbook"""
        self._orderbook_cache.set(f"book:{token_id}", data, ttl)

    def get_best_bid(self, token_id: str) -> Tuple[bool, float]:
        """Get cached best bid"""
        return self._orderbook_cache.get(f"bid:{token_id}")

    def set_best_bid(self, token_id: str, bid: float, ttl: float = 0.5):
        """Cache best bid"""
        self._orderbook_cache.set(f"bid:{token_id}", bid, ttl)

    # ==================== ORDER CACHE ====================

    def get_order(self, order_id: str) -> Tuple[bool, Any]:
        """Get cached order status"""
        return self._order_cache.get(f"order:{order_id}")

    def set_order(self, order_id: str, data: Any, ttl: float = 5.0):
        """Cache order status"""
        self._order_cache.set(f"order:{order_id}", data, ttl)

    def invalidate_order(self, order_id: str):
        """Invalidate order cache (e.g., after fill)"""
        self._order_cache.delete(f"order:{order_id}")

    # ==================== RATE LIMITING ====================

    def acquire_rate_limit(self, host: str, timeout: float = 5.0) -> bool:
        """Acquire rate limit permission for host"""
        return self._rate_limiter.acquire(host, timeout)

    # ==================== STATS ====================

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            "market_cache": self._market_cache.stats(),
            "orderbook_cache": self._orderbook_cache.stats(),
            "order_cache": self._order_cache.stats(),
        }


# ==================== GLOBAL INSTANCE ====================
_api_cache: Optional[APICache] = None
_cache_lock = threading.Lock()


def get_api_cache() -> APICache:
    """Get or create API cache"""
    global _api_cache
    with _cache_lock:
        if _api_cache is None:
            _api_cache = APICache()
            _api_cache.start()
        return _api_cache


def cached_api_call(cache_type: str, ttl: float = 1.0):
    """
    Decorator for caching API calls.

    Usage:
        @cached_api_call("market", ttl=2.0)
        def get_market(slug: str) -> dict:
            ...
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            cache = get_api_cache()

            # Generate cache key from function name and args
            key = f"{func.__name__}:{':'.join(str(a) for a in args)}"

            # Check cache
            if cache_type == "market":
                hit, value = cache._market_cache.get(key)
            elif cache_type == "orderbook":
                hit, value = cache._orderbook_cache.get(key)
            elif cache_type == "order":
                hit, value = cache._order_cache.get(key)
            else:
                hit, value = False, None

            if hit:
                return value

            # Call actual function
            result = func(*args, **kwargs)

            # Cache result
            if result is not None:
                if cache_type == "market":
                    cache._market_cache.set(key, result, ttl)
                elif cache_type == "orderbook":
                    cache._orderbook_cache.set(key, result, ttl)
                elif cache_type == "order":
                    cache._order_cache.set(key, result, ttl)

            return result
        return wrapper
    return decorator
