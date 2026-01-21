"""
Endgame Sniper Bot - Main UI
Standalone bot for catching price crashes in final minutes before market expiry

UPDATED: Display proxy status in UI
"""
import sys
import time
import threading
import logging
import customtkinter as ctk
from datetime import datetime

from config import (
    BOT_VERSION, SUPPORTED_MARKETS, sniper_config, UI_UPDATE_MS,
    BALANCE_UPDATE_HOURS, TG_TOKEN, TG_CHAT_ID, is_proxy_enabled
)
from shared_state import (
    setup_logging, log_queue, shared_data, data_lock, stop_event,
    get_market_status, notify_bot_start, notify_bot_stop, notify_balance_update,
    get_settlement_stats
)
from trade_manager import init_trade_manager, get_trade_manager
from sniper_core import get_sniper, start_sniper, stop_sniper


def safe_float(val, default: float) -> float:
    """Safely convert to float"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class SniperApp(ctk.CTk):
    """Main UI for Endgame Sniper Bot"""

    def __init__(self):
        super().__init__()

        # Setup logging first
        setup_logging()

        self.title(f"Endgame Sniper Bot - {BOT_VERSION}")
        self.geometry("900x700")
        self.minsize(800, 600)

        # Set theme
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

        # Start UI update loop
        self._update_loop()

        logging.info(f"\U0001F680 {BOT_VERSION} UI initialized")

    def _setup_ui(self):
        """Setup the UI components"""

        # Main container with padding
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ==================== HEADER ====================
        header_frame = ctk.CTkFrame(self)
        header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))

        title_label = ctk.CTkLabel(
            header_frame,
            text=f"\U0001F3AF Endgame Sniper Bot",
            font=ctk.CTkFont(size=20, weight="bold")
        )
        title_label.pack(side="left", padx=10, pady=10)

        self.version_label = ctk.CTkLabel(
            header_frame,
            text=BOT_VERSION,
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

        # Sniper Price
        ctk.CTkLabel(config_frame, text="Sniper Price ($):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        self.sniper_price_entry = ctk.CTkEntry(config_frame, width=80)
        self.sniper_price_entry.insert(0, str(sniper_config.sniper_price))
        self.sniper_price_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")

        # Order Size (shares)
        ctk.CTkLabel(config_frame, text="Order Size (shares):").grid(row=row, column=2, padx=10, pady=5, sticky="e")
        self.order_size_entry = ctk.CTkEntry(config_frame, width=80)
        self.order_size_entry.insert(0, str(int(sniper_config.order_size_shares)))
        self.order_size_entry.grid(row=row, column=3, padx=5, pady=5, sticky="w")

        # Row 2: Trigger Settings
        row = 1

        # Trigger Threshold
        ctk.CTkLabel(config_frame, text="Trigger Threshold:").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        self.trigger_threshold_entry = ctk.CTkEntry(config_frame, width=80)
        self.trigger_threshold_entry.insert(0, str(sniper_config.trigger_threshold))
        self.trigger_threshold_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")

        # Trigger Minutes
        ctk.CTkLabel(config_frame, text="Trigger Minutes:").grid(row=row, column=2, padx=10, pady=5, sticky="e")
        self.trigger_minutes_entry = ctk.CTkEntry(config_frame, width=80)
        self.trigger_minutes_entry.insert(0, str(sniper_config.trigger_minutes_before_expiry))
        self.trigger_minutes_entry.grid(row=row, column=3, padx=5, pady=5, sticky="w")

        # Row 3: Check Interval, Status & Save Button (compact layout)
        row = 2

        ctk.CTkLabel(config_frame, text="Check Interval (sec):").grid(row=row, column=0, padx=10, pady=5, sticky="e")
        self.check_interval_entry = ctk.CTkEntry(config_frame, width=80)
        self.check_interval_entry.insert(0, str(int(sniper_config.check_interval_seconds)))
        self.check_interval_entry.grid(row=row, column=1, padx=5, pady=5, sticky="w")

        # Status indicators and Save button in a sub-frame
        status_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        status_frame.grid(row=row, column=2, columnspan=2, padx=5, pady=5, sticky="ew")

        # Telegram status
        tg_status = "\u2705 TG" if (TG_TOKEN and TG_CHAT_ID) else "\u274c TG"
        self.telegram_label = ctk.CTkLabel(status_frame, text=tg_status, width=50)
        self.telegram_label.pack(side="left", padx=5)

        # Proxy status
        proxy_status = "\u2705 Proxy" if is_proxy_enabled() else "\u274c Proxy"
        self.proxy_label = ctk.CTkLabel(status_frame, text=proxy_status, width=60)
        self.proxy_label.pack(side="left", padx=5)

        # Save Settings button (compact)
        self.save_btn = ctk.CTkButton(
            status_frame,
            text="\U0001F4BE Save",
            width=70,
            height=28,
            fg_color="#2E7D32",
            hover_color="#1B5E20",
            command=self._save_config_with_feedback
        )
        self.save_btn.pack(side="left", padx=10)

        # Save status label
        self.save_status_label = ctk.CTkLabel(
            status_frame,
            text="",
            text_color="green",
            width=100
        )
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

        # Select All / None buttons
        ctk.CTkButton(
            markets_header,
            text="Select All",
            width=80,
            command=self._select_all_markets
        ).pack(side="right", padx=5)

        ctk.CTkButton(
            markets_header,
            text="Select None",
            width=80,
            command=self._select_no_markets
        ).pack(side="right", padx=5)

        # Markets list
        markets_list_frame = ctk.CTkFrame(markets_frame)
        markets_list_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        for i, market in enumerate(SUPPORTED_MARKETS):
            symbol = market["symbol"]
            name = market["name"]

            row_frame = ctk.CTkFrame(markets_list_frame)
            row_frame.pack(fill="x", padx=5, pady=2)

            # Checkbox
            var = ctk.BooleanVar(value=sniper_config.is_market_enabled(symbol))
            self._market_vars[symbol] = var

            cb = ctk.CTkCheckBox(
                row_frame,
                text=f"{symbol} ({name})",
                variable=var,
                width=150
            )
            cb.pack(side="left", padx=10)

            # Status label
            status_label = ctk.CTkLabel(
                row_frame,
                text="Idle",
                width=200
            )
            status_label.pack(side="left", padx=10)
            self._market_status_labels[symbol] = status_label

            # Bid labels
            up_bid_label = ctk.CTkLabel(row_frame, text="UP: -", width=80)
            up_bid_label.pack(side="left", padx=5)
            self._market_status_labels[f"{symbol}_up"] = up_bid_label

            down_bid_label = ctk.CTkLabel(row_frame, text="DOWN: -", width=80)
            down_bid_label.pack(side="left", padx=5)
            self._market_status_labels[f"{symbol}_down"] = down_bid_label

        # ==================== LOG FRAME ====================
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)

        self.grid_rowconfigure(3, weight=1)

        # Log textbox
        self.log_text = ctk.CTkTextbox(log_frame, height=200)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        # Clear log button
        ctk.CTkButton(
            log_frame,
            text="Clear Log",
            width=80,
            command=self._clear_log
        ).grid(row=1, column=0, pady=5)

        # ==================== CONTROL BUTTONS ====================
        control_frame = ctk.CTkFrame(self)
        control_frame.grid(row=4, column=0, sticky="ew", padx=10, pady=10)

        self.start_btn = ctk.CTkButton(
            control_frame,
            text="\u25B6 START",
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
            text="\u23F9 STOP",
            font=ctk.CTkFont(size=16, weight="bold"),
            fg_color="red",
            hover_color="darkred",
            width=150,
            height=40,
            state="disabled",
            command=self.stop
        )
        self.stop_btn.pack(side="left", padx=20, pady=10)

        # Stats display
        self.stats_label = ctk.CTkLabel(
            control_frame,
            text="Orders: 0 | Fills: 0",
            font=ctk.CTkFont(size=12)
        )
        self.stats_label.pack(side="right", padx=20, pady=10)

        # PnL display (NEW)
        self.pnl_label = ctk.CTkLabel(
            control_frame,
            text="W/L: 0/0 | PnL: $0.00",
            font=ctk.CTkFont(size=12)
        )
        self.pnl_label.pack(side="right", padx=10, pady=10)

        # Status label
        self.status_label = ctk.CTkLabel(
            control_frame,
            text="\u23F8 STOPPED",
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.status_label.pack(side="right", padx=20, pady=10)

    def _select_all_markets(self):
        """Select all markets"""
        for var in self._market_vars.values():
            var.set(True)

    def _select_no_markets(self):
        """Deselect all markets"""
        for var in self._market_vars.values():
            var.set(False)

    def _clear_log(self):
        """Clear log textbox"""
        self.log_text.delete("1.0", "end")

    def _save_config(self):
        """Save config from UI"""
        try:
            sniper_config.sniper_price = safe_float(self.sniper_price_entry.get(), 0.01)
            sniper_config.order_size_shares = safe_float(self.order_size_entry.get(), 100)
            sniper_config.trigger_threshold = safe_float(self.trigger_threshold_entry.get(), 0.30)
            sniper_config.trigger_minutes_before_expiry = safe_float(self.trigger_minutes_entry.get(), 2.0)
            sniper_config.check_interval_seconds = safe_float(self.check_interval_entry.get(), 5.0)

            # Save market enabled states
            for symbol, var in self._market_vars.items():
                sniper_config.set_market_enabled(symbol, var.get())

            sniper_config.save()
            logging.info("\u2705 Config saved")
            return True
        except Exception as e:
            logging.error(f"Config save error: {e}")
            return False

    def _save_config_with_feedback(self):
        """Save config and show feedback in UI"""
        success = self._save_config()
        if success:
            self.save_status_label.configure(
                text="\u2705 Settings saved!",
                text_color="green"
            )
        else:
            self.save_status_label.configure(
                text="\u274c Save failed!",
                text_color="red"
            )
        # Clear status after 3 seconds
        self.after(3000, lambda: self.save_status_label.configure(text=""))

    def start(self):
        """Start the sniper bot"""
        if self._running:
            return

        try:
            # Save config
            self._save_config()

            # Check if any market is enabled
            enabled = [s for s, v in self._market_vars.items() if v.get()]
            if not enabled:
                logging.error("\u274c No markets enabled!")
                return

            # Update UI state
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.status_label.configure(text="\U0001F504 STARTING...")

            # Clear stop event
            stop_event.clear()

            # Initialize trade manager
            logging.info("\U0001F50C Initializing Trade Manager...")
            executor = init_trade_manager()

            if not executor.ready:
                logging.error("\u274c Trade Manager initialization failed!")
                self._reset_ui()
                return

            # Update balance
            balance = executor.update_balance()
            logging.info(f"\U0001F4B0 Balance: ${balance:,.2f}")

            # Set running state
            with data_lock:
                shared_data["is_running"] = True

            self._running = True

            # Start sniper
            start_sniper()

            # Start balance update thread
            self._balance_thread = threading.Thread(
                target=self._balance_update_worker,
                daemon=True,
                name="BalanceUpdater"
            )
            self._balance_thread.start()

            # Update UI
            self.status_label.configure(text="\u25B6 RUNNING")

            # Notify Telegram
            notify_bot_start()

            logging.info("\u2705 Sniper bot started!")

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
            self.status_label.configure(text="\U0001F504 STOPPING...")

            # Set stop event
            stop_event.set()

            with data_lock:
                shared_data["is_running"] = False

            # Stop sniper
            stop_sniper()

            self._running = False

            # Notify Telegram
            notify_bot_stop()

            logging.info("\u2705 Sniper bot stopped")

        except Exception as e:
            logging.error(f"Stop error: {e}")
        finally:
            self._reset_ui()

    def _reset_ui(self):
        """Reset UI to stopped state"""
        self._running = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_label.configure(text="\u23F8 STOPPED")

    def _balance_update_worker(self):
        """Background worker to update balance every 2 hours"""
        interval_seconds = BALANCE_UPDATE_HOURS * 3600

        while self._running and not stop_event.is_set():
            try:
                time.sleep(interval_seconds)

                if not self._running:
                    break

                executor = get_trade_manager()
                if executor and executor.ready:
                    balance = executor.update_balance()
                    logging.info(f"\U0001F4B0 Balance update: ${balance:,.2f}")
                    notify_balance_update(balance)

            except Exception as e:
                logging.error(f"Balance update error: {e}")

    def _update_loop(self):
        """UI update loop"""
        try:
            # Update log
            while not log_queue.empty():
                try:
                    msg = log_queue.get_nowait()
                    self.log_text.insert("end", msg + "\n")
                    self.log_text.see("end")
                except:
                    break

            # Update balance display
            with data_lock:
                balance = shared_data.get("usdc_balance", 0.0)
                orders = shared_data.get("total_orders_placed", 0)
                fills = shared_data.get("total_fills", 0)

            self.balance_label.configure(text=f"Balance: ${balance:,.2f}")
            self.stats_label.configure(text=f"Orders: {orders} | Fills: {fills}")

            # Update PnL display (NEW)
            settlement_stats = get_settlement_stats()
            wins = settlement_stats.get("total_wins", 0)
            losses = settlement_stats.get("total_losses", 0)
            total_pnl = settlement_stats.get("total_pnl", 0.0)
            pnl_color = "green" if total_pnl >= 0 else "red"
            self.pnl_label.configure(
                text=f"W/L: {wins}/{losses} | PnL: ${total_pnl:+.2f}",
                text_color=pnl_color
            )

            # Update market status
            for symbol in self._market_vars.keys():
                status = get_market_status(symbol)

                status_text = status.get("status", "Idle")
                self._market_status_labels[symbol].configure(text=status_text)

                up_bid = status.get("up_bid", 0.0)
                down_bid = status.get("down_bid", 0.0)

                self._market_status_labels[f"{symbol}_up"].configure(
                    text=f"UP: {up_bid:.2f}" if up_bid > 0 else "UP: -"
                )
                self._market_status_labels[f"{symbol}_down"].configure(
                    text=f"DOWN: {down_bid:.2f}" if down_bid > 0 else "DOWN: -"
                )

        except Exception as e:
            logging.debug(f"Update loop error: {e}")

        # Schedule next update
        self._update_job = self.after(UI_UPDATE_MS, self._update_loop)

    def on_closing(self):
        """Handle window close"""
        if self._running:
            self.stop()

        if self._update_job:
            self.after_cancel(self._update_job)

        self.destroy()


def main():
    """Main entry point"""
    app = SniperApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
