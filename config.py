"""
Endgame Sniper Bot - Configuration
Standalone bot for catching price crashes in final minutes before market expiry

FIXED: Added Proxy Support for VPS deployment
- Supports both single URL format and separate parameters format
- Uses socks5h:// for DNS resolution via proxy
"""
import os
import sys
import json
import requests
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# NOTE: SSL verification is ALWAYS enabled in production.
# If you need to debug SSL issues on older Windows, update your CA certificates
# or Python/OpenSSL version instead of disabling verification.
# NEVER set verify=False or CERT_NONE in production - MITM attack risk!

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

# Also check parent directory for .env
if not os.getenv("PRIVATE_KEY"):
    load_dotenv(os.path.join(os.path.dirname(BASE_DIR), '.env'))

BOT_VERSION = "V1.1 - Endgame Sniper (Proxy + Fix Double Order)"

# ==================== API ENDPOINTS ====================
HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

# ==================== PROXY CONFIGURATION ====================
# Method 1: Single URL format
# PROXY_URL=socks5://user:pass@host:port

# Method 2: Separate parameters (like other bot)
# PROXY_ENABLED=true
# PROXY_TYPE=socks5
# PROXY_HOST=66.245.167.148
# PROXY_PORT=45497
# PROXY_USERNAME=user
# PROXY_PASSWORD=pass

PROXY_CONFIG = {
    # Method 1: Single URL
    "url": os.getenv("PROXY_URL", "").strip(),
    
    # Method 2: Separate parameters
    "enabled": os.getenv("PROXY_ENABLED", "false").lower() == "true",
    "type": os.getenv("PROXY_TYPE", "socks5"),
    "host": os.getenv("PROXY_HOST", ""),
    "port": int(os.getenv("PROXY_PORT", "1080") or "1080"),
    "username": os.getenv("PROXY_USERNAME", ""),
    "password": os.getenv("PROXY_PASSWORD", ""),
}


def is_proxy_enabled() -> bool:
    """Check if proxy is configured"""
    # Method 1: Single URL
    if PROXY_CONFIG["url"]:
        return True
    # Method 2: Separate parameters
    return PROXY_CONFIG["enabled"] and bool(PROXY_CONFIG["host"])


def get_proxy_url() -> Optional[str]:
    """
    Get proxy URL in standard format.
    Uses socks5h:// for SOCKS5 to resolve DNS via proxy (important!)
    """
    # Method 1: Single URL provided
    if PROXY_CONFIG["url"]:
        url = PROXY_CONFIG["url"]
        # Convert socks5:// to socks5h:// for DNS resolution via proxy
        if url.startswith("socks5://"):
            url = "socks5h://" + url[9:]
        return url
    
    # Method 2: Build from separate parameters
    if not (PROXY_CONFIG["enabled"] and PROXY_CONFIG["host"]):
        return None
    
    proxy_type = PROXY_CONFIG["type"].lower()
    host = PROXY_CONFIG["host"]
    port = PROXY_CONFIG["port"]
    username = PROXY_CONFIG["username"]
    password = PROXY_CONFIG["password"]
    
    # Build auth string
    auth = f"{username}:{password}@" if username and password else ""
    
    # Map proxy types - socks5h resolves DNS via proxy (important!)
    scheme_map = {
        "socks5": "socks5h",
        "socks5h": "socks5h",
        "socks4": "socks4",
        "https": "http",
        "http": "http",
    }
    scheme = scheme_map.get(proxy_type, "http")
    
    return f"{scheme}://{auth}{host}:{port}"


def get_proxy_config() -> Optional[Dict[str, str]]:
    """
    Get proxy configuration for requests library.
    Returns dict like {"http": "...", "https": "..."}
    """
    proxy_url = get_proxy_url()
    if not proxy_url:
        return None
    
    return {
        "http": proxy_url,
        "https": proxy_url
    }


def setup_environment_proxy():
    """Set environment variables for libraries that read proxy from env"""
    proxy_url = get_proxy_url()
    
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["ALL_PROXY"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        os.environ["https_proxy"] = proxy_url
        os.environ["all_proxy"] = proxy_url
    else:
        # Clear proxy env vars
        for var in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"]:
            os.environ.pop(var, None)


def check_socks_support() -> bool:
    """Check if PySocks is installed"""
    try:
        import socks
        return True
    except ImportError:
        return False


def get_proxy_display() -> str:
    """Get proxy info for display (hide password)"""
    if not is_proxy_enabled():
        return "None (direct)"
    
    if PROXY_CONFIG["url"]:
        url = PROXY_CONFIG["url"]
        # Hide password
        if "@" in url:
            parts = url.split("@")
            return f"***@{parts[-1]}"
        return url
    
    # From separate params
    host = PROXY_CONFIG["host"]
    port = PROXY_CONFIG["port"]
    ptype = PROXY_CONFIG["type"]
    has_auth = "with auth" if PROXY_CONFIG["username"] else "no auth"
    return f"{ptype}://{host}:{port} ({has_auth})"


# NOTE: Proxy environment setup is NOT done on import to avoid side effects.
# Call setup_environment_proxy() explicitly in your entrypoint (run_headless.py, run_ui.py)
# if you need to set proxy environment variables for external libraries.
#
# Recommended: Pass proxies directly to Session/requests instead of setting global env vars.

def init_proxy_if_needed():
    """
    Initialize proxy environment if enabled.
    Call this explicitly from entrypoints, not on import.
    """
    if is_proxy_enabled():
        setup_environment_proxy()

        # Warn if SOCKS without PySocks
        proxy_type = PROXY_CONFIG["type"].lower()
        if PROXY_CONFIG["url"]:
            proxy_type = "socks5" if "socks" in PROXY_CONFIG["url"].lower() else "http"

        if proxy_type.startswith("socks") and not check_socks_support():
            print("⚠️ SOCKS proxy requires: pip install PySocks requests[socks]")


# ==================== SESSION MANAGEMENT ====================
GLOBAL_SESSION = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS"]
)
adapter = HTTPAdapter(
    max_retries=retries,
    pool_connections=10,
    pool_maxsize=20
)
GLOBAL_SESSION.mount('https://', adapter)
GLOBAL_SESSION.mount('http://', adapter)
GLOBAL_SESSION.verify = True

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
GLOBAL_SESSION.headers.update(BROWSER_HEADERS)

# Apply proxy to global session
proxy_config = get_proxy_config()
if proxy_config:
    GLOBAL_SESSION.proxies.update(proxy_config)

# ==================== FILES ====================
CONFIG_FILE = os.path.join(BASE_DIR, "sniper_config.json")
LOG_FILE = os.path.join(BASE_DIR, "sniper.log")

# ==================== TIMING ====================
UI_UPDATE_MS = 1000
BALANCE_UPDATE_HOURS = 2  # Update balance every 2 hours

# ==================== RATE LIMITING ====================
MIN_ORDER_SIZE = 5.0
REQUEST_COOLDOWN = 0.15

# ==================== CREDENTIALS ====================
def strict_clean(val):
    if not val:
        return ""
    return str(val).strip().replace('"', '').replace("'", "").replace('\n', '').replace('\r', '')

PRIVATE_KEY = strict_clean(os.getenv("PRIVATE_KEY"))
POLY_PROXY_ADDRESS = strict_clean(os.getenv("POLY_PROXY_ADDRESS"))
TG_TOKEN = strict_clean(os.getenv("TELEGRAM_BOT_TOKEN"))
TG_CHAT_ID = strict_clean(os.getenv("TELEGRAM_CHAT_ID"))

# ==================== SUPPORTED MARKETS ====================
SUPPORTED_MARKETS = [
    {"symbol": "BTC", "prefix": "btc-updown-15m", "name": "Bitcoin"},
    {"symbol": "ETH", "prefix": "eth-updown-15m", "name": "Ethereum"},
    {"symbol": "SOL", "prefix": "sol-updown-15m", "name": "Solana"},
    {"symbol": "XRP", "prefix": "xrp-updown-15m", "name": "Ripple"},
]


@dataclass
class SniperConfig:
    """
    Endgame Sniper Configuration

    Strategy:
    - Monitor markets in final N minutes before expiry
    - If both UP and DOWN have best_bid > trigger_threshold, place cheap limit orders
    - Orders at sniper_price will only fill if price crashes near 0
    """

    # Sniper price - the limit buy price (default 0.01)
    sniper_price: float = 0.01

    # Order size in shares (not USDC)
    order_size_shares: float = 100.0

    # Trigger threshold - both UP & DOWN best_bid must exceed this
    trigger_threshold: float = 0.30

    # Minutes before expiry to start monitoring
    trigger_minutes_before_expiry: float = 2.0

    # Check interval in seconds
    check_interval_seconds: float = 5.0

    # Per-market enabled settings
    markets: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        if not self.markets:
            for m in SUPPORTED_MARKETS:
                self.markets[m["symbol"]] = {
                    "prefix": m["prefix"],
                    "enabled": True,  # All enabled by default
                }

    def get_enabled_markets(self) -> List[dict]:
        result = []
        for symbol, cfg in self.markets.items():
            if cfg.get("enabled", False):
                for m in SUPPORTED_MARKETS:
                    if m["symbol"] == symbol:
                        result.append({
                            "symbol": symbol,
                            "prefix": cfg.get("prefix", m["prefix"]),
                        })
                        break
        return result

    def is_market_enabled(self, symbol: str) -> bool:
        return self.markets.get(symbol, {}).get("enabled", False)

    def set_market_enabled(self, symbol: str, enabled: bool):
        if symbol in self.markets:
            self.markets[symbol]["enabled"] = enabled

    def validate(self):
        # Sniper price: 0.01 - 0.10
        if self.sniper_price < 0.01:
            self.sniper_price = 0.01
        elif self.sniper_price > 0.10:
            self.sniper_price = 0.10

        # Order size: minimum 5 shares
        if self.order_size_shares < MIN_ORDER_SIZE:
            self.order_size_shares = MIN_ORDER_SIZE
        elif self.order_size_shares > 10000:
            self.order_size_shares = 10000

        # Trigger threshold: 0.20 - 0.50
        if self.trigger_threshold < 0.20:
            self.trigger_threshold = 0.20
        elif self.trigger_threshold > 0.50:
            self.trigger_threshold = 0.50

        # Trigger minutes: 1 - 5
        if self.trigger_minutes_before_expiry < 1.0:
            self.trigger_minutes_before_expiry = 1.0
        elif self.trigger_minutes_before_expiry > 5.0:
            self.trigger_minutes_before_expiry = 5.0

        # Check interval: 2 - 30 seconds
        if self.check_interval_seconds < 2.0:
            self.check_interval_seconds = 2.0
        elif self.check_interval_seconds > 30.0:
            self.check_interval_seconds = 30.0

    def save(self):
        self.validate()
        try:
            data = {
                "sniper_price": self.sniper_price,
                "order_size_shares": self.order_size_shares,
                "trigger_threshold": self.trigger_threshold,
                "trigger_minutes_before_expiry": self.trigger_minutes_before_expiry,
                "check_interval_seconds": self.check_interval_seconds,
                "markets": self.markets,
            }
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Config save error: {e}")

    @classmethod
    def load(cls) -> 'SniperConfig':
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    config = cls()
                    config.sniper_price = data.get("sniper_price", 0.01)
                    config.order_size_shares = data.get("order_size_shares", 100.0)
                    config.trigger_threshold = data.get("trigger_threshold", 0.30)
                    config.trigger_minutes_before_expiry = data.get("trigger_minutes_before_expiry", 2.0)
                    config.check_interval_seconds = data.get("check_interval_seconds", 5.0)

                    if "markets" in data:
                        config.markets = data["markets"]

                    # Ensure all markets exist
                    for m in SUPPORTED_MARKETS:
                        if m["symbol"] not in config.markets:
                            config.markets[m["symbol"]] = {
                                "prefix": m["prefix"],
                                "enabled": True,
                            }

                    config.validate()
                    return config
        except Exception as e:
            print(f"Config load error: {e}")
        return cls()


# Global config instance
sniper_config = SniperConfig.load()


# ==================== TEST PROXY ====================
def test_proxy(verbose: bool = True) -> bool:
    """Test proxy connection"""
    if not is_proxy_enabled():
        if verbose:
            print("ℹ️ Proxy not enabled")
        return True
    
    # Check SOCKS support
    proxy_type = PROXY_CONFIG["type"].lower()
    if PROXY_CONFIG["url"]:
        proxy_type = "socks5" if "socks" in PROXY_CONFIG["url"].lower() else "http"
    
    if proxy_type.startswith("socks") and not check_socks_support():
        print("❌ SOCKS proxy requires: pip install PySocks requests[socks]")
        return False
    
    proxies = get_proxy_config()
    
    if verbose:
        print(f"🔌 Testing proxy: {get_proxy_display()}")
    
    test_urls = [
        ("https://api.ipify.org?format=json", "IP Check"),
        ("https://gamma-api.polymarket.com/markets?limit=1", "Polymarket API"),
    ]

    # FIX: Test ALL URLs instead of returning after first success
    all_passed = True
    for url, name in test_urls:
        try:
            resp = requests.get(url, proxies=proxies, timeout=15)
            if resp.status_code == 200:
                if "ipify" in url:
                    ip = resp.json().get("ip", "unknown")
                    if verbose:
                        print(f"✅ {name}: {ip}")
                else:
                    if verbose:
                        print(f"✅ {name}: OK")
            else:
                if verbose:
                    print(f"❌ {name}: HTTP {resp.status_code}")
                all_passed = False
        except Exception as e:
            if verbose:
                print(f"❌ {name} failed: {e}")
            all_passed = False

    return all_passed


if __name__ == "__main__":
    print("=" * 50)
    print("🔌 Endgame Sniper - Proxy Configuration")
    print("=" * 50)
    print(f"\nProxy Enabled: {is_proxy_enabled()}")
    print(f"Proxy URL: {get_proxy_display()}")
    print(f"PySocks installed: {check_socks_support()}")
    
    if is_proxy_enabled():
        print("\n" + "-" * 50)
        print("Testing connection...")
        if test_proxy():
            print("\n✅ Proxy is working!")
        else:
            print("\n❌ Proxy test failed!")
