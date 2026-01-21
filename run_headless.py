#!/usr/bin/env python3
"""
Endgame Sniper Bot - Headless Entry Point
For VPS 24/7 operation without UI

Usage:
    python run_headless.py [options]

Options:
    --config PATH     Path to config file (default: config.json)
    --log-level LEVEL Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)
    --no-restore      Don't restore state from previous session
    --daemon          Run in daemon mode (not implemented yet)

Environment Variables:
    HEADLESS=1        Alternative to command line
    PRIVATE_KEY       Required: Wallet private key
    PROXY_URL         Optional: Proxy URL
    TELEGRAM_BOT_TOKEN  Optional: Telegram notifications
    TELEGRAM_CHAT_ID    Optional: Telegram chat ID

Windows Service:
    To run as Windows service, use NSSM or Task Scheduler.
    See docs/windows_service.md for details.
"""
import os
import sys
import argparse
import signal
import time
import logging
import platform
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Endgame Sniper Bot - Headless Mode (VPS 24/7)"
    )
    parser.add_argument(
        '--config', '-c',
        type=str,
        default=None,
        help='Path to config file (default: config.json)'
    )
    parser.add_argument(
        '--log-level', '-l',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Log level (default: INFO)'
    )
    parser.add_argument(
        '--no-restore',
        action='store_true',
        help="Don't restore state from previous session"
    )
    parser.add_argument(
        '--daemon', '-d',
        action='store_true',
        help='Run in daemon mode (background)'
    )
    return parser.parse_args()


def main():
    """Main entry point for headless mode"""
    args = parse_args()

    # Set headless flag
    os.environ['HEADLESS'] = '1'

    # Import engine modules
    from engine.logging_config import setup_headless_logging
    from engine.config_schema import load_config, get_config
    from engine.state_manager import get_state_manager, RunMode
    from engine.trade_executor import init_trade_executor, is_proxy_enabled
    from engine.sniper_engine import get_sniper_engine, start_engine, stop_engine
    from engine.notifier import get_notifier, init_notifier
    from engine.persistence import get_persistence_manager, restore_state_if_available

    # Setup logging
    setup_headless_logging(args.log_level)
    logging.info("=" * 60)
    logging.info("ENDGAME SNIPER BOT - HEADLESS MODE")
    logging.info("=" * 60)

    # Load config
    config = load_config()
    config.headless = True
    config.log_level = args.log_level
    logging.info(f"Config loaded: sniper_price=${config.sniper_price:.2f}, "
                f"order_size={config.order_size_shares}, "
                f"trigger={config.trigger_threshold}")

    # Initialize state manager
    state = get_state_manager(mode=RunMode.HEADLESS)
    logging.info("State manager initialized")

    # Restore previous state if available
    if not args.no_restore:
        if restore_state_if_available(max_age_hours=24):
            logging.info("Previous state restored")
        else:
            logging.info("No previous state to restore")

    # Initialize notifier with proxy config
    from engine.trade_executor import get_proxy_config
    proxy_config = get_proxy_config()
    notifier = init_notifier(proxy_config=proxy_config)
    logging.info(f"Notifier initialized (Telegram: {'enabled' if notifier.telegram_enabled else 'disabled'})")

    # Initialize trade executor
    logging.info("Initializing trade executor...")
    executor = init_trade_executor()

    if not executor.ready:
        logging.error("Trade executor initialization failed!")
        logging.error("Check your PRIVATE_KEY and network connection")
        sys.exit(1)

    balance = executor.update_balance()
    logging.info(f"Trade executor ready. Balance: ${balance:,.2f}")

    # Initialize engine
    engine = get_sniper_engine(mode=RunMode.HEADLESS)

    # Setup signal handlers for graceful shutdown
    shutdown_requested = False

    def graceful_shutdown(reason: str = "signal"):
        nonlocal shutdown_requested
        if shutdown_requested:
            logging.warning("Force shutdown...")
            sys.exit(1)

        shutdown_requested = True
        logging.info(f"Shutdown requested ({reason}), stopping gracefully...")

        # Stop engine
        stop_engine()

        # Send stop notification
        stats = state.get_settlement_stats()
        notifier.notify_bot_stop(
            orders=state._state.get("total_orders_placed", 0),
            fills=state.get_fill_count(),
            wins=stats.get("total_wins", 0),
            losses=stats.get("total_losses", 0),
            pnl=stats.get("total_pnl", 0.0)
        )

        logging.info("Shutdown complete")

    def signal_handler(signum, frame):
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)
        graceful_shutdown(f"signal {sig_name}")
        sys.exit(0)

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)

    # SIGTERM may not work on Windows depending on how process is started
    if platform.system() != "Windows":
        signal.signal(signal.SIGTERM, signal_handler)
    else:
        # Windows: Use SIGBREAK for console close events
        try:
            signal.signal(signal.SIGBREAK, signal_handler)
        except (AttributeError, ValueError):
            pass  # SIGBREAK not available

    # Windows: Register console control handler for more events
    if platform.system() == "Windows":
        try:
            import win32api
            import win32con

            def windows_console_handler(ctrl_type):
                if ctrl_type in (win32con.CTRL_C_EVENT,
                                win32con.CTRL_BREAK_EVENT,
                                win32con.CTRL_CLOSE_EVENT,
                                win32con.CTRL_LOGOFF_EVENT,
                                win32con.CTRL_SHUTDOWN_EVENT):
                    graceful_shutdown(f"windows_ctrl_{ctrl_type}")
                    return True
                return False

            win32api.SetConsoleCtrlHandler(windows_console_handler, True)
            logging.info("Windows console handler registered")
        except ImportError:
            logging.debug("pywin32 not installed, using basic signal handling")

    # Start engine
    logging.info("Starting sniper engine...")
    start_engine()

    # Send start notification
    notifier.notify_bot_start(
        version="V2.0 Headless",
        proxy_enabled=is_proxy_enabled(),
        mode="headless"
    )

    # Log enabled markets
    enabled = config.get_enabled_markets()
    logging.info(f"Enabled markets: {[m['symbol'] for m in enabled]}")

    # Log polling config
    p = config.polling
    logging.info(f"Polling config:")
    logging.info(f"  Far (>{p.far_threshold}s): {p.far_min_interval}-{p.far_max_interval}s")
    logging.info(f"  Near ({p.near_threshold}-{p.far_threshold}s): {p.near_min_interval}-{p.near_max_interval}s")
    logging.info(f"  Sniper (<{p.near_threshold}s): {p.sniper_min_interval}-{p.sniper_max_interval}s")
    logging.info(f"  Jitter: ±{p.jitter_percent*100:.0f}%")

    logging.info("=" * 60)
    logging.info("Bot is running. Press Ctrl+C to stop.")
    logging.info("=" * 60)

    # Main loop - just keep running
    try:
        while not shutdown_requested:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)


if __name__ == "__main__":
    main()
