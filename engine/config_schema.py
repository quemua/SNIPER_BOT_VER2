"""
Endgame Sniper Bot - Configuration Schema & Validation
Provides robust config management with:
- Schema validation
- Atomic save (write temp -> rename)
- Live reload without restart
- Thread-safe access
"""
import os
import json
import tempfile
import threading
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Callable
from enum import Enum


# ==================== CONSTANTS ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LEGACY_CONFIG_FILE = os.path.join(BASE_DIR, "sniper_config.json")

# Config change event - listeners can subscribe to be notified on config changes
CONFIG_CHANGE_EVENT = threading.Event()
_config_listeners: List[Callable] = []
_config_lock = threading.RLock()


# ==================== SUPPORTED MARKETS ====================
SUPPORTED_MARKETS = [
    {"symbol": "BTC", "prefix": "btc-updown-15m", "name": "Bitcoin"},
    {"symbol": "ETH", "prefix": "eth-updown-15m", "name": "Ethereum"},
    {"symbol": "SOL", "prefix": "sol-updown-15m", "name": "Solana"},
    {"symbol": "XRP", "prefix": "xrp-updown-15m", "name": "Ripple"},
]


# ==================== CONFIG SCHEMA ====================
class PollingTier(Enum):
    """Polling tier based on time to expiry"""
    FAR = "far"       # >10 minutes: 30-60s interval
    NEAR = "near"     # 2-10 minutes: 5-10s interval
    SNIPER = "sniper" # <=2 minutes: 0.7-2s interval


@dataclass
class PollingConfig:
    """Polling configuration for 3-tier system"""
    # Far tier (>10 minutes to expiry)
    far_min_interval: float = 30.0
    far_max_interval: float = 60.0

    # Near tier (2-10 minutes to expiry)
    near_min_interval: float = 5.0
    near_max_interval: float = 10.0

    # Sniper tier (<=2 minutes to expiry)
    sniper_min_interval: float = 0.7
    sniper_max_interval: float = 2.0

    # Tier boundaries (in seconds)
    far_threshold: float = 600.0   # 10 minutes
    near_threshold: float = 120.0  # 2 minutes

    # Jitter percentage (0.0 - 1.0)
    jitter_percent: float = 0.20


@dataclass
class SettlementConfig:
    """Settlement watcher configuration"""
    # Initial delay after expiry before first check
    initial_delay_min: float = 8.0
    initial_delay_max: float = 15.0

    # Backoff settings
    min_interval: float = 10.0
    max_interval: float = 60.0
    backoff_multiplier: float = 1.5

    # Give up settings (active polling)
    max_wait_minutes: float = 30.0
    max_attempts: int = 200  # Max attempts before marking stale
    notify_once_on_timeout: bool = True

    # Stale state settings (reduced polling for very old settlements)
    stale_check_interval: float = 300.0  # 5 minutes between checks for stale
    stale_max_age_hours: float = 24.0  # Mark as abandoned after this
    cleanup_abandoned_hours: float = 48.0  # Remove abandoned records after this


@dataclass
class FillMonitorConfig:
    """Fill monitor adaptive configuration"""
    # Dense polling phase (0-120s after order placed)
    dense_duration: float = 120.0
    dense_min_interval: float = 5.0
    dense_max_interval: float = 10.0

    # Sparse polling phase (after dense phase)
    sparse_min_interval: float = 30.0
    sparse_max_interval: float = 60.0

    # Stop monitoring after this duration (seconds)
    max_monitor_duration: float = 900.0  # 15 minutes

    # Notify once per side (anti-spam)
    notify_once_per_side: bool = True


@dataclass
class MarketConfig:
    """Per-market configuration"""
    prefix: str
    enabled: bool = True


@dataclass
class SniperConfigSchema:
    """
    Main configuration schema for Endgame Sniper Bot
    All fields have validation ranges defined in validate()
    """

    # ===== Trading Parameters =====
    # Sniper price - the limit buy price
    sniper_price: float = 0.01

    # Order size in shares (not USDC)
    order_size_shares: float = 100.0

    # Trigger threshold - both UP & DOWN best_bid must exceed this
    trigger_threshold: float = 0.30

    # Minutes before expiry to start monitoring (sniper window)
    trigger_minutes_before_expiry: float = 2.0

    # Legacy: check interval (now replaced by 3-tier polling)
    check_interval_seconds: float = 5.0

    # ===== Polling Configuration =====
    polling: PollingConfig = field(default_factory=PollingConfig)

    # ===== Settlement Configuration =====
    settlement: SettlementConfig = field(default_factory=SettlementConfig)

    # ===== Fill Monitor Configuration =====
    fill_monitor: FillMonitorConfig = field(default_factory=FillMonitorConfig)

    # ===== Per-market settings =====
    markets: Dict[str, MarketConfig] = field(default_factory=dict)

    # ===== Runtime Settings =====
    # Headless mode (no UI)
    headless: bool = False

    # Logging level
    log_level: str = "INFO"

    # Balance update interval (hours)
    balance_update_hours: float = 2.0

    def __post_init__(self):
        """Initialize markets if not set"""
        if not self.markets:
            for m in SUPPORTED_MARKETS:
                self.markets[m["symbol"]] = MarketConfig(
                    prefix=m["prefix"],
                    enabled=True
                )
        # Convert dict to MarketConfig if needed
        for symbol, cfg in list(self.markets.items()):
            if isinstance(cfg, dict):
                self.markets[symbol] = MarketConfig(**cfg)

    def get_enabled_markets(self) -> List[dict]:
        """Get list of enabled markets"""
        result = []
        for symbol, cfg in self.markets.items():
            if cfg.enabled:
                for m in SUPPORTED_MARKETS:
                    if m["symbol"] == symbol:
                        result.append({
                            "symbol": symbol,
                            "prefix": cfg.prefix,
                            "name": m["name"],
                        })
                        break
        return result

    def is_market_enabled(self, symbol: str) -> bool:
        """Check if a market is enabled"""
        if symbol in self.markets:
            return self.markets[symbol].enabled
        return False

    def set_market_enabled(self, symbol: str, enabled: bool):
        """Set market enabled state"""
        if symbol in self.markets:
            self.markets[symbol].enabled = enabled

    def validate(self) -> 'SniperConfigSchema':
        """
        Validate and sanitize all config values.
        Returns self for chaining.
        """
        # Sniper price: 0.01 - 0.10
        self.sniper_price = max(0.01, min(0.10, self.sniper_price))

        # Order size: 5 - 10000 shares
        self.order_size_shares = max(5.0, min(10000.0, self.order_size_shares))

        # Trigger threshold: 0.20 - 0.50
        self.trigger_threshold = max(0.20, min(0.50, self.trigger_threshold))

        # Trigger minutes: 1 - 5
        self.trigger_minutes_before_expiry = max(1.0, min(5.0, self.trigger_minutes_before_expiry))

        # Check interval: 2 - 30 seconds
        self.check_interval_seconds = max(2.0, min(30.0, self.check_interval_seconds))

        # Balance update: 0.5 - 24 hours
        self.balance_update_hours = max(0.5, min(24.0, self.balance_update_hours))

        # Log level validation
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if self.log_level.upper() not in valid_levels:
            self.log_level = "INFO"
        else:
            self.log_level = self.log_level.upper()

        # Polling config validation
        self._validate_polling()

        # Settlement config validation
        self._validate_settlement()

        # Fill monitor config validation
        self._validate_fill_monitor()

        # Ensure all supported markets exist
        for m in SUPPORTED_MARKETS:
            if m["symbol"] not in self.markets:
                self.markets[m["symbol"]] = MarketConfig(
                    prefix=m["prefix"],
                    enabled=True
                )

        return self

    def _validate_polling(self):
        """Validate polling configuration"""
        p = self.polling

        # Far tier: 30-60s
        p.far_min_interval = max(20.0, min(120.0, p.far_min_interval))
        p.far_max_interval = max(p.far_min_interval, min(180.0, p.far_max_interval))

        # Near tier: 5-10s
        p.near_min_interval = max(3.0, min(30.0, p.near_min_interval))
        p.near_max_interval = max(p.near_min_interval, min(60.0, p.near_max_interval))

        # Sniper tier: 0.7-2s
        p.sniper_min_interval = max(0.5, min(5.0, p.sniper_min_interval))
        p.sniper_max_interval = max(p.sniper_min_interval, min(10.0, p.sniper_max_interval))

        # Thresholds
        p.far_threshold = max(300.0, min(900.0, p.far_threshold))
        p.near_threshold = max(60.0, min(p.far_threshold, p.near_threshold))

        # Jitter
        p.jitter_percent = max(0.0, min(0.5, p.jitter_percent))

    def _validate_settlement(self):
        """Validate settlement configuration"""
        s = self.settlement

        s.initial_delay_min = max(5.0, min(30.0, s.initial_delay_min))
        s.initial_delay_max = max(s.initial_delay_min, min(60.0, s.initial_delay_max))

        s.min_interval = max(5.0, min(30.0, s.min_interval))
        s.max_interval = max(s.min_interval, min(120.0, s.max_interval))

        s.backoff_multiplier = max(1.1, min(3.0, s.backoff_multiplier))

        s.max_wait_minutes = max(10.0, min(60.0, s.max_wait_minutes))
        s.max_attempts = max(50, min(500, s.max_attempts))

        s.stale_check_interval = max(60.0, min(600.0, s.stale_check_interval))
        s.stale_max_age_hours = max(6.0, min(48.0, s.stale_max_age_hours))
        s.cleanup_abandoned_hours = max(s.stale_max_age_hours, min(168.0, s.cleanup_abandoned_hours))

    def _validate_fill_monitor(self):
        """Validate fill monitor configuration"""
        f = self.fill_monitor

        f.dense_duration = max(60.0, min(300.0, f.dense_duration))
        f.dense_min_interval = max(3.0, min(30.0, f.dense_min_interval))
        f.dense_max_interval = max(f.dense_min_interval, min(60.0, f.dense_max_interval))

        f.sparse_min_interval = max(15.0, min(120.0, f.sparse_min_interval))
        f.sparse_max_interval = max(f.sparse_min_interval, min(180.0, f.sparse_max_interval))

        f.max_monitor_duration = max(300.0, min(3600.0, f.max_monitor_duration))

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        return {
            "sniper_price": self.sniper_price,
            "order_size_shares": self.order_size_shares,
            "trigger_threshold": self.trigger_threshold,
            "trigger_minutes_before_expiry": self.trigger_minutes_before_expiry,
            "check_interval_seconds": self.check_interval_seconds,
            "headless": self.headless,
            "log_level": self.log_level,
            "balance_update_hours": self.balance_update_hours,
            "polling": asdict(self.polling),
            "settlement": asdict(self.settlement),
            "fill_monitor": asdict(self.fill_monitor),
            "markets": {
                symbol: asdict(cfg) for symbol, cfg in self.markets.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SniperConfigSchema':
        """Create from dictionary"""
        config = cls()

        # Simple fields
        if "sniper_price" in data:
            config.sniper_price = float(data["sniper_price"])
        if "order_size_shares" in data:
            config.order_size_shares = float(data["order_size_shares"])
        if "trigger_threshold" in data:
            config.trigger_threshold = float(data["trigger_threshold"])
        if "trigger_minutes_before_expiry" in data:
            config.trigger_minutes_before_expiry = float(data["trigger_minutes_before_expiry"])
        if "check_interval_seconds" in data:
            config.check_interval_seconds = float(data["check_interval_seconds"])
        if "headless" in data:
            config.headless = bool(data["headless"])
        if "log_level" in data:
            config.log_level = str(data["log_level"])
        if "balance_update_hours" in data:
            config.balance_update_hours = float(data["balance_update_hours"])

        # Nested configs
        if "polling" in data and isinstance(data["polling"], dict):
            config.polling = PollingConfig(**data["polling"])
        if "settlement" in data and isinstance(data["settlement"], dict):
            config.settlement = SettlementConfig(**data["settlement"])
        if "fill_monitor" in data and isinstance(data["fill_monitor"], dict):
            config.fill_monitor = FillMonitorConfig(**data["fill_monitor"])

        # Markets
        if "markets" in data and isinstance(data["markets"], dict):
            for symbol, cfg in data["markets"].items():
                if isinstance(cfg, dict):
                    config.markets[symbol] = MarketConfig(**cfg)
                elif isinstance(cfg, MarketConfig):
                    config.markets[symbol] = cfg

        return config.validate()


# ==================== GLOBAL CONFIG INSTANCE ====================
_config: Optional[SniperConfigSchema] = None


def get_config() -> SniperConfigSchema:
    """Get current config (thread-safe)"""
    global _config
    with _config_lock:
        if _config is None:
            _config = load_config()
        return _config


def load_config() -> SniperConfigSchema:
    """
    Load config from file.
    Tries config.json first, falls back to legacy sniper_config.json
    """
    global _config

    with _config_lock:
        # Try new config file first
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                _config = SniperConfigSchema.from_dict(data)
                logging.debug(f"Loaded config from {CONFIG_FILE}")
                return _config
            except Exception as e:
                logging.warning(f"Failed to load {CONFIG_FILE}: {e}")

        # Try legacy config file
        if os.path.exists(LEGACY_CONFIG_FILE):
            try:
                with open(LEGACY_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                _config = SniperConfigSchema.from_dict(data)
                logging.debug(f"Loaded config from legacy {LEGACY_CONFIG_FILE}")
                # Migrate to new config file
                save_config(_config)
                return _config
            except Exception as e:
                logging.warning(f"Failed to load legacy config: {e}")

        # Create default config
        _config = SniperConfigSchema().validate()
        save_config(_config)
        logging.info("Created default config")
        return _config


def save_config(config: SniperConfigSchema) -> bool:
    """
    Save config to file with atomic write (write temp -> rename)
    Returns True on success
    """
    global _config

    with _config_lock:
        try:
            config.validate()

            # Atomic write: write to temp file first, then rename
            dir_path = os.path.dirname(CONFIG_FILE)
            with tempfile.NamedTemporaryFile(
                mode='w',
                dir=dir_path,
                suffix='.tmp',
                delete=False,
                encoding='utf-8'
            ) as tmp_file:
                json.dump(config.to_dict(), tmp_file, indent=2)
                tmp_path = tmp_file.name

            # Atomic rename (on POSIX systems)
            os.replace(tmp_path, CONFIG_FILE)

            _config = config
            CONFIG_CHANGE_EVENT.set()
            CONFIG_CHANGE_EVENT.clear()

            # Notify listeners
            for listener in _config_listeners:
                try:
                    listener(config)
                except Exception as e:
                    logging.error(f"Config listener error: {e}")

            logging.debug(f"Config saved to {CONFIG_FILE}")
            return True

        except Exception as e:
            logging.error(f"Failed to save config: {e}")
            # Clean up temp file if it exists
            try:
                if 'tmp_path' in locals():
                    os.unlink(tmp_path)
            except:
                pass
            return False


def reload_config() -> SniperConfigSchema:
    """
    Reload config from file (live reload without restart)
    """
    global _config
    with _config_lock:
        _config = None
        return load_config()


def validate_config(config: SniperConfigSchema) -> SniperConfigSchema:
    """Validate config and return sanitized version"""
    return config.validate()


def add_config_listener(callback: Callable[[SniperConfigSchema], None]):
    """Add a listener to be notified on config changes"""
    with _config_lock:
        if callback not in _config_listeners:
            _config_listeners.append(callback)


def remove_config_listener(callback: Callable[[SniperConfigSchema], None]):
    """Remove a config change listener"""
    with _config_lock:
        if callback in _config_listeners:
            _config_listeners.remove(callback)
