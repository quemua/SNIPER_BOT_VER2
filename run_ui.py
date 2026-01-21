#!/usr/bin/env python3
"""
Endgame Sniper Bot - UI Entry Point
Desktop mode with full GUI for configuration and monitoring

Usage:
    python run_ui.py [options]

Options:
    --config PATH     Path to config file (default: config.json)
    --log-level LEVEL Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)

This mode provides:
- Full UI for configuration
- Market enable/disable controls
- Real-time status and logs
- PnL tracking display
"""
import os
import sys
import time
import threading
import logging
import queue
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

import customtkinter as ctk
from datetime import datetime


def safe_float(val, default: float) -> float:
    """Safely convert to float"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class SniperApp(ctk.CTk):
    """Main UI for Endgame Sniper Bot with new engine"""

    def __init__(self):
        super().__init__()

        # Import engine modules
        from engine.logging_config import setup_ui_logging
        from engine.config_schema import load_config, save_config, get_config, SUPPORTED_MARKETS
        from engine.state_manager import get_state_manager, RunMode
        from engine.trade_executor import init_trade_executor, get_trade_executor, is_proxy_enabled
        from engine.sniper_engine import get_sniper_engine, start_engine, stop_engine
        from engine.notifier import init_notifier, get_notifier
        from engine.persistence import restore_state_if_available

        # Store imports for later use
        self._modules = {
            'load_config': load_config,
            'save_config': save_config,
            'get_config': get_config,
            'SUPPORTED_MARKETS': SUPPORTED_MARKETS,
            'get_state_manager': get_state_manager,
            'RunMode': RunMode,
            'init_trade_executor': init_trade_executor,
            'get_trade_executor': get_trade_executor,
            'is_proxy_enabled': is_proxy_enabled,
            'get_sniper_engine': get_sniper_engine,
            'start_engine': start_engine,
            'stop_engine': stop_engine,
            'init_notifier': init_notifier,
            'get_notifier': get_notifier,
            'restore_state_if_available': restore_state_if_available,
        }

        # Initialize state manager in UI mode
        self._state = get_state_manager(mode=RunMode.UI)
        self._log_queue = self._state.log_queue

        # Setup logging with UI queue
        setup_ui_logging(self._log_queue, "INFO")

        # Load config
        self._config = load_config()

        # Window setup
        self.title("Endgame Sniper Bot - V2.0")
        self.geometry("900x750")
        self.minsize(800, 650)

        # Theme
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # State
        self._running = False
        self._update_job = None
        self._balance_thread = None

        # Market checkboxes
        self._market_vars = {}
        self._market_status_labels = {}

        # Build UI
        self._setup_ui()

        # Restore previous state
        restore_state_if_available(max_age_hours=24)

        # Start UI update loop
        self._update_loop()

        logging.info("V2.0 - Endgame Sniper UI initialized")

    def _setup_ui(self):
        """Setup UI components"""
        SUPPORTED_MARKETS = self._modules['SUPPORTED_MARKETS']
        is_proxy_enabled = self._modules['is_proxy_enabled']
        get_notifier = self._modules['get_notifier']

        # Main container
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ==================== HEADER ====================
        header_frame = ctk.CTkFrame(self)
        header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))

        title_label = ctk.CTkLabel(
            header_frame,
            text="Endgame Sniper Bot",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        title_label.pack(side="left", padx=10, pady=10)

        self.version_label = ctk.CTkLabel(
            header_frame,
            text="V2.0 Headless Optimized",
            font=ctk.CTkFont(size=12)
        )
        self.version_label.pack(side="left", padx=5)

        # Balance display
        self.balance_label = ctk.CTkLabel(
            header_frame,
            text="Balance: $0.00",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.balance_label.pack(side="right", padx=20, pady=10)

        # ==================== CONFIG FRAME ====================
        config_frame = ctk.CTkFrame(self)
        config_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        config_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        # Row 1: Sniper Settings
        row = 0
        ctk.CTkLabel(config_frame, text="Sniper Price ($):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        self.sniper_price_entry = ctk.CTkEntry(config_frame, width=80)
        self.sniper_price_entry.insert(0, str(self._config.sniper_price))
        self.sniper_price_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(config_frame, text="Order Size (shares):").grid(row=row, column=2, padx=10, pady=5, sticky="e")
        self.order_size_entry = ctk.CTkEntry(config_frame, width=80)
        self.order_size_entry.insert(0, str(int(self._config.order_size_shares)))
        self.order_size_entry.grid(row=row, column=3, padx=5, pady=5, sticky="w")

        # Row 2: Trigger Settings
        row = 1
        ctk.CTkLabel(config_frame, text="Trigger Threshold:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        self.trigger_threshold_entry = ctk.CTkEntry(config_frame, width=80)
        self.trigger_threshold_entry.insert(0, str(self._config.trigger_threshold))
        self.trigger_threshold_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(config_frame, text="Trigger Minutes:").grid(row=row, column=2, padx=10, pady=5, sticky="e")
        self.trigger_minutes_entry = ctk.CTkEntry(config_frame, width=80)
        self.trigger_minutes_entry.insert(0, str(self._config.trigger_minutes_before_expiry))
        self.trigger_minutes_entry.grid(row=row, column=3, padx=5, pady=5, sticky="w")

        # Row 3: Check Interval and Status
        row = 2
        ctk.CTkLabel(config_frame, text="Check Interval (sec):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        self.check_interval_entry = ctk.CTkEntry(config_frame, width=80)
        self.check_interval_entry.insert(0, str(int(self._config.check_interval_seconds)))
        self.check_interval_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")

        # Status and Save
        status_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        status_frame.grid(row=row, column=2, columnspan=2, padx=5, pady=5, sticky="ew")

        notifier = get_notifier()
        tg_status = "TG" if notifier.telegram_enabled else "TG"
        tg_color = "green" if notifier.telegram_enabled else "gray"
        self.telegram_label = ctk.CTkLabel(status_frame, text=tg_status, text_color=tg_color, width=40)
        self.telegram_label.pack(side="left", padx=5)

        proxy_enabled = is_proxy_enabled()
        proxy_status = "Proxy" if proxy_enabled else "Proxy"
        proxy_color = "green" if proxy_enabled else "gray"
        self.proxy_label = ctk.CTkLabel(status_frame, text=proxy_status, text_color=proxy_color, width=50)
        self.proxy_label.pack(side="left", padx=5)

        self.save_btn = ctk.CTkButton(
            status_frame,
            text="Save",
            width=70,
            height=28,
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            command=self._save_config_with_feedback
        )
        self.save_btn.pack(side="left", padx=10)

        self.save_status_label = ctk.CTkLabel(status_frame, text="", text_color="green", width=100)
        self.save_status_label.pack(side="left", padx=5)

        # ==================== MARKETS FRAME ====================
        markets_frame = ctk.CTkFrame(self)
        markets_frame.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        markets_frame.grid_columnconfigure(0, weight=1)
        markets_frame.grid_rowconfigure(1, weight=1)

        # Markets header
        markets_header = ctk.CTkFrame(markets_frame)
        markets_header.grid(row=0, column=0, sticky="ew", padx=5, pady=5)

        ctk.CTkLabel(
            markets_header,
            text="Markets",
            font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            markets_header, text="Select All", width=80,
            command=self._select_all_markets
        ).pack(side="right", padx=5)

        ctk.CTkButton(
            markets_header, text="Select None", width=80,
            command=self._select_no_markets
        ).pack(side="right", padx=5)

        # Markets list
        markets_list_frame = ctk.CTkFrame(markets_frame)
        markets_list_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        for market in SUPPORTED_MARKETS:
            symbol = market["symbol"]
            name = market["name"]

            row_frame = ctk.CTkFrame(markets_list_frame)
            row_frame.pack(fill="x", padx=5, pady=2)

            var = ctk.BooleanVar(value=self._config.is_market_enabled(symbol))
            self._market_vars[symbol] = var

            cb = ctk.CTkCheckBox(
                row_frame,
                text=f"{symbol} ({name})",
                variable=var,
                width=150
            )
            cb.pack(side="left", padx=10)

            status_label = ctk.CTkLabel(row_frame, text="Idle", width=200)
            status_label.pack(side="left", padx=10)
            self._market_status_labels[symbol] = status_label

            up_bid_label = ctk.CTkLabel(row_frame, text="UP: -", width=80)
            up_bid_label.pack(side="left", padx=5)
            self._market_status_labels[f"{symbol}_up"] = up_bid_label

            down_bid_label = ctk.CTkLabel(row_frame, text="DOWN: -", width=80)
            down_bid_label.pack(side="left", padx=5)
            self._market_status_labels[f"{symbol}_down"] = down_bid_label

            tier_label = ctk.CTkLabel(row_frame, text="[far]", width=50)
            tier_label.pack(side="left", padx=5)
            self._market_status_labels[f"{symbol}_tier"] = tier_label

        # ==================== LOG FRAME ====================
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self.log_text = ctk.CTkTextbox(log_frame, height=200)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        ctk.CTkButton(
            log_frame, text="Clear Log", width=80,
            command=self._clear_log
        ).grid(row=1, column=0, pady=5)

        # ==================== CONTROL BUTTONS ====================
        control_frame = ctk.CTkFrame(self)
        control_frame.grid(row=4, column=0, sticky="ew", padx=10, pady=10)

        self.start_btn = ctk.CTkButton(
            control_frame,
            text="START",
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="green",
            hover_color="darkgreen",
            width=150,
            height=40,
            command=self.start
        )
        self.start_btn.pack(side="left", padx=20, pady=10)

        self.stop_btn = ctk.CTkButton(
            control_frame,
            text="STOP",
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="red",
            hover_color="darkred",
            width=150,
            height=40,
            state="disabled",
            command=self.stop
        )
        self.stop_btn.pack(side="left", padx=20, pady=10)

        self.stats_label = ctk.CTkLabel(
            control_frame,
            text="Orders: 0 | Fills: 0",
            font=ctk.CTkFont(size=12)
        )
        self.stats_label.pack(side="right", padx=20, pady=10)

        self.pnl_label = ctk.CTkLabel(
            control_frame,
            text="W/L: 0/0 | PnL: $0.00",
            font=ctk.CTkFont(size=12)
        )
        self.pnl_label.pack(side="right", padx=10, pady=10)

        self.status_label = ctk.CTkLabel(
            control_frame,
            text="STOPPED",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.status_label.pack(side="right", padx=20, pady=10)

    def _select_all_markets(self):
        for var in self._market_vars.values():
            var.set(True)

    def _select_no_markets(self):
        for var in self._market_vars.values():
            var.set(False)

    def _clear_log(self):
        self.log_text.delete("1.0", "end")

    def _save_config(self):
        """Save config from UI"""
        try:
            save_config = self._modules['save_config']

            self._config.sniper_price = safe_float(self.sniper_price_entry.get(), 0.01)
            self._config.order_size_shares = safe_float(self.order_size_entry.get(), 100)
            self._config.trigger_threshold = safe_float(self.trigger_threshold_entry.get(), 0.30)
            self._config.trigger_minutes_before_expiry = safe_float(self.trigger_minutes_entry.get(), 2.0)
            self._config.check_interval_seconds = safe_float(self.check_interval_entry.get(), 5.0)

            for symbol, var in self._market_vars.items():
                self._config.set_market_enabled(symbol, var.get())

            save_config(self._config)
            logging.info("Config saved")
            return True
        except Exception as e:
            logging.error(f"Config save error: {e}")
            return False

    def _save_config_with_feedback(self):
        success = self._save_config()
        if success:
            self.save_status_label.configure(text="Saved!", text_color="green")
        else:
            self.save_status_label.configure(text="Failed!", text_color="red")
        self.after(3000, lambda: self.save_status_label.configure(text=""))

    def start(self):
        """Start the sniper bot"""
        if self._running:
            return

        try:
            self._save_config()

            enabled = [s for s, v in self._market_vars.items() if v.get()]
            if not enabled:
                logging.error("No markets enabled!")
                return

            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.status_label.configure(text="STARTING...")

            self._state.clear_stop()

            # Initialize trade executor
            init_trade_executor = self._modules['init_trade_executor']
            logging.info("Initializing Trade Executor...")
            executor = init_trade_executor()

            if not executor.ready:
                logging.error("Trade Executor initialization failed!")
                self._reset_ui()
                return

            balance = executor.update_balance()
            logging.info(f"Balance: ${balance:,.2f}")

            self._state.set_running(True)
            self._running = True

            # Start engine
            start_engine = self._modules['start_engine']
            start_engine()

            # Start balance update thread
            self._balance_thread = threading.Thread(
                target=self._balance_update_worker,
                daemon=True,
                name="BalanceUpdater"
            )
            self._balance_thread.start()

            self.status_label.configure(text="RUNNING")

            # Notify
            get_notifier = self._modules['get_notifier']
            is_proxy_enabled = self._modules['is_proxy_enabled']
            notifier = get_notifier()
            notifier.notify_bot_start(
                version="V2.0 UI",
                proxy_enabled=is_proxy_enabled(),
                mode="ui"
            )

            logging.info("Sniper bot started!")

        except Exception as e:
            logging.error(f"Start error: {e}")
            import traceback
            traceback.print_exc()
            self._reset_ui()

    def stop(self):
        """Stop the sniper bot"""
        if not self._running:
            return

        try:
            self.status_label.configure(text="STOPPING...")

            self._state.request_stop()
            self._state.set_running(False)

            stop_engine = self._modules['stop_engine']
            stop_engine()

            self._running = False

            # Notify
            get_notifier = self._modules['get_notifier']
            notifier = get_notifier()
            stats = self._state.get_settlement_stats()
            notifier.notify_bot_stop(
                orders=self._state._state.get("total_orders_placed", 0),
                fills=self._state.get_fill_count(),
                wins=stats.get("total_wins", 0),
                losses=stats.get("total_losses", 0),
                pnl=stats.get("total_pnl", 0.0)
            )

            logging.info("Sniper bot stopped")

        except Exception as e:
            logging.error(f"Stop error: {e}")
        finally:
            self._reset_ui()

    def _reset_ui(self):
        self._running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="STOPPED")

    def _balance_update_worker(self):
        """Background worker to update balance"""
        interval_seconds = self._config.balance_update_hours * 3600

        while self._running and not self._state.should_stop():
            try:
                time.sleep(interval_seconds)
                if not self._running:
                    break

                get_trade_executor = self._modules['get_trade_executor']
                executor = get_trade_executor()
                if executor and executor.ready:
                    balance = executor.update_balance()
                    logging.info(f"Balance update: ${balance:,.2f}")

                    get_notifier = self._modules['get_notifier']
                    notifier = get_notifier()
                    notifier.notify_balance_update(
                        balance,
                        self._state._state.get("total_orders_placed", 0),
                        self._state.get_fill_count()
                    )

            except Exception as e:
                logging.error(f"Balance update error: {e}")

    def _update_loop(self):
        """UI update loop"""
        try:
            # Update log
            while self._log_queue and not self._log_queue.empty():
                try:
                    msg = self._log_queue.get_nowait()
                    self.log_text.insert("end", msg + "\n")
                    self.log_text.see("end")
                except:
                    break

            # Update balance
            balance = self._state.get_balance()
            self.balance_label.configure(text=f"Balance: ${balance:,.2f}")

            # Update stats
            orders = self._state._state.get("total_orders_placed", 0)
            fills = self._state.get_fill_count()
            self.stats_label.configure(text=f"Orders: {orders} | Fills: {fills}")

            # Update PnL
            stats = self._state.get_settlement_stats()
            wins = stats.get("total_wins", 0)
            losses = stats.get("total_losses", 0)
            total_pnl = stats.get("total_pnl", 0.0)
            pnl_color = "green" if total_pnl >= 0 else "red"
            self.pnl_label.configure(
                text=f"W/L: {wins}/{losses} | PnL: ${total_pnl:+.2f}",
                text_color=pnl_color
            )

            # Update market status
            for symbol in self._market_vars.keys():
                status = self._state.get_market_status(symbol)

                status_text = status.get("status", "Idle")
                self._market_status_labels[symbol].configure(text=status_text)

                up_bid = status.get("up_bid", 0.0)
                down_bid = status.get("down_bid", 0.0)
                tier = status.get("polling_tier", "far")

                self._market_status_labels[f"{symbol}_up"].configure(
                    text=f"UP: {up_bid:.2f}" if up_bid > 0 else "UP: -"
                )
                self._market_status_labels[f"{symbol}_down"].configure(
                    text=f"DOWN: {down_bid:.2f}" if down_bid > 0 else "DOWN: -"
                )
                self._market_status_labels[f"{symbol}_tier"].configure(
                    text=f"[{tier}]"
                )

        except Exception as e:
            logging.debug(f"Update loop error: {e}")

        # Schedule next update
        self._update_job = self.after(1000, self._update_loop)

    def on_closing(self):
        """Handle window close"""
        if self._running:
            self.stop()

        if self._update_job:
            self.after_cancel(self._update_job)

        self.destroy()


def main():
    """Main entry point for UI mode"""
    app = SniperApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
