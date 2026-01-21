"""
Endgame Sniper Bot - Engine Module
Core trading engine that can run in both Headless (VPS) and UI modes

This module contains the core trading logic that is shared between both modes:
- Market scanner / sniper logic
- TradeManager / executor
- Fill monitor / settlement watcher / redeem
- Persistence / risk checks / rate limit

UI should only import engine_client for control, not engine directly.
"""

from engine.config_schema import (
    SniperConfigSchema,
    load_config,
    save_config,
    validate_config,
    get_config,
    reload_config,
    CONFIG_CHANGE_EVENT,
)

from engine.state_manager import (
    StateManager,
    get_state_manager,
    RunMode,
)

from engine.trade_executor import (
    TradeExecutor,
    get_trade_executor,
    init_trade_executor,
)

from engine.sniper_engine import (
    SniperEngine,
    get_sniper_engine,
    start_engine,
    stop_engine,
)

from engine.persistence import (
    PersistenceManager,
    get_persistence_manager,
)

from engine.logging_config import (
    setup_logging,
    setup_headless_logging,
    setup_ui_logging,
)

__all__ = [
    # Config
    'SniperConfigSchema',
    'load_config',
    'save_config',
    'validate_config',
    'get_config',
    'reload_config',
    'CONFIG_CHANGE_EVENT',

    # State
    'StateManager',
    'get_state_manager',
    'RunMode',

    # Trade
    'TradeExecutor',
    'get_trade_executor',
    'init_trade_executor',

    # Engine
    'SniperEngine',
    'get_sniper_engine',
    'start_engine',
    'stop_engine',

    # Persistence
    'PersistenceManager',
    'get_persistence_manager',

    # Logging
    'setup_logging',
    'setup_headless_logging',
    'setup_ui_logging',
]
