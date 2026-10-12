#!/usr/bin/env python3
"""
TWS API Multi-Asset Velocity & Volatility Monitor Daemon
- Dynamically driven by monitor_config.csv (auto-reloads on file changes).
- Subscribes to live Stock Prices and TWS Model Option IV (tickType 13) for ATM contracts.
- Persists all historical ticks in a local SQLite database (monitor_history.db) so prior
  values for 5m, 30m, 1h, 5h, and 1d are never lost across Mac reboots or TWS restarts.
- Dispatches custom voice text, audio alarm/song, persistent modal dialogs, and desktop banners.
"""

import os
import sys
import csv
import time
import math
import sqlite3
import datetime
import subprocess
import threading
from collections import defaultdict
from ibapi.client import EClient, make_field
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from alert_router import AlertRouter

# ==============================================================================
# DAEMON CONFIGURATION
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_CSV_PATH = os.path.join(BASE_DIR, "monitor_config.csv")
STATUS_CSV_PATH = os.path.join(BASE_DIR, "monitor_status.csv")
DB_PATH = os.path.join(BASE_DIR, "monitor_history.db")

TWS_HOST = "127.0.0.1"
TWS_PORT = 7496                # 7496 = Live TWS, 7497 = Paper TWS
CLIENT_ID = 55                 # Dedicated Client ID for this multi-asset daemon

CONFIG_POLL_INTERVAL_SEC = 0.5 # Fast poll (every 500ms) for near-instant detection of config changes
ALARM_DURATION_SECONDS = 5     # How long to play sound/song (seconds)
CUSTOM_SONG_PATH = None        # e.g., "/Users/BeNiceToFamily/Music/alarm.mp3"
# ==============================================================================


# ==============================================================================
# DATABASE PERSISTENCE LAYER (SQLite)
# ==============================================================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                ticker TEXT,
                metric TEXT,
                strike REAL,
                expiry TEXT,
                value REAL,
                spot REAL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hist ON history(ticker, metric, timestamp)")
        conn.commit()

def record_tick(ticker: str, metric: str, value: float, strike: float = None, expiry: str = None, spot: float = None):
    """Saves tick to SQLite. Filters out micro-fluctuations to keep DB lean."""
    now = time.time()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            # Check last recorded value to avoid redundant writes
            cursor.execute("""
                SELECT value, timestamp FROM history
                WHERE ticker = ? AND metric = ?
                ORDER BY timestamp DESC LIMIT 1
            """, (ticker, metric))
            row = cursor.fetchone()
            if row:
                last_val, last_time = row
                # Write if >= 2 seconds elapsed OR if value moved notably
                if (now - last_time < 2.0) and abs(value - last_val) < 0.02:
                    return

            cursor.execute("""
                INSERT INTO history (timestamp, ticker, metric, strike, expiry, value, spot)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (now, ticker, metric, strike, expiry, value, spot))
            conn.commit()
    except Exception as e:
        print(f"[DB Error] record_tick: {e}", flush=True)

def get_prior_value(ticker: str, metric: str, target_time: float, max_time_diff_sec: float = 600.0, min_elapsed_sec: float = 10.0):
    """
    Finds the baseline reference point P0 = (value, timestamp) for the tracking window.
    Filters option metrics (IV, OPT_PRICE) by strike IS NOT NULL so stock 30-day index data is never mixed in.
    """
    now = time.time()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            strike_clause = " AND strike IS NOT NULL" if metric in ["IV", "OPT_PRICE"] else ""
            
            # 1. Primary: Record at or before target_time
            cursor.execute(f"""
                SELECT value, timestamp FROM history
                WHERE ticker = ? AND metric = ?{strike_clause} AND timestamp <= ?
                ORDER BY timestamp DESC LIMIT 1
            """, (ticker, metric, target_time))
            row = cursor.fetchone()
            if row:
                val, ts = row
                return val, ts

            # 2. Fallback: First available recorded point in the tracking window [target_time, now]
            cursor.execute(f"""
                SELECT value, timestamp FROM history
                WHERE ticker = ? AND metric = ?{strike_clause} AND timestamp >= ?
                ORDER BY timestamp ASC LIMIT 1
            """, (ticker, metric, target_time))
            row = cursor.fetchone()
            if row:
                val, ts = row
                if (now - ts) >= min_elapsed_sec:
                    return val, ts
    except Exception as e:
        print(f"[DB Error] get_prior_value: {e}", flush=True)
    return None, None

def prune_old_db_records(days: int = 3):
    """Prunes records older than N days to keep disk usage minimal."""
    cutoff = time.time() - (days * 86400)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM history WHERE timestamp < ?", (cutoff,))
            conn.commit()
    except Exception:
        pass


# ==============================================================================
# ALERT DISPATCHER (Audio, Voice, Dialog, Banner)
# ==============================================================================
def dispatch_alert(title: str, message: str, voice_text: str):
    print(f"\n{'='*75}\n🚨 [ALARM TRIGGERED] {title}\n{message}\n{'='*75}\n", flush=True)

    # 1. Desktop Banner Notification
    banner_script = f'display notification "{message.replace(chr(10), " | ")}" with title "{title}"'
    subprocess.Popen(["osascript", "-e", banner_script])

    # 2. Spoken Voice & Audio Playback
    def audio_thread():
        # Text-to-Speech
        if voice_text:
            subprocess.run(["say", "-v", "Samantha", voice_text])

        # Sound alarm / song
        end_time = time.time() + ALARM_DURATION_SECONDS
        if CUSTOM_SONG_PATH and os.path.exists(CUSTOM_SONG_PATH):
            subprocess.run(["afplay", "-t", str(ALARM_DURATION_SECONDS), CUSTOM_SONG_PATH])
        else:
            sound_file = "/System/Library/Sounds/Sosumi.aiff"
            while time.time() < end_time:
                subprocess.run(["afplay", sound_file])
                time.sleep(0.35)

    threading.Thread(target=audio_thread, daemon=True).start()

    # 3. Persistent Modal Dialog (stays on top until user acknowledges)
    def dialog_thread():
        dialog_script = (
            f'display dialog "{message}" with title "{title}" '
            f'with icon stop buttons {{"Acknowledge"}} default button "Acknowledge"'
        )
        subprocess.run(["osascript", "-e", dialog_script])

    threading.Thread(target=dialog_thread, daemon=True).start()


# ==============================================================================
# CONFIGURATION RULE MODEL
# ==============================================================================
def parse_datetime_str(s: str):
    if not s or not str(s).strip():
        return None
    s_clean = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(s_clean, fmt)
            return dt.timestamp()
        except ValueError:
            pass
    return None

class MonitorRule:
    def __init__(self, ticker: str, metric: str, mode: str, window_str: str, threshold_pct: float, 
                 target_price: float, condition: str, voice_template: str, enabled: bool,
                 window_start_time: str = "", rule_num: int = None):
        self.rule_num = rule_num
        self.ticker = ticker.upper().strip()
        self.metric = metric.upper().strip()        # "IV" or "PRICE" or "OPT_PRICE"
        
        # Mode: "TARGET" (compares against target_price) or "VELOCITY" (rolling window)
        mode_clean = (mode or "").upper().strip()
        if not mode_clean:
            mode_clean = "TARGET" if (target_price is not None and str(target_price).strip() != "") else "VELOCITY"
        self.mode = mode_clean

        self.window_str = (window_str or "").lower().strip()
        self.window_seconds = self._parse_window(self.window_str) if self.mode in ["VELOCITY", "WINDOW"] else 0.0
        
        self.window_start_time_raw = str(window_start_time or "").strip()
        self.window_start_ts = parse_datetime_str(self.window_start_time_raw)

        try:
            self.threshold_pct = float(threshold_pct) if threshold_pct not in [None, ""] else 0.0
        except ValueError:
            self.threshold_pct = 0.0

        try:
            self.target_price = float(target_price) if target_price not in [None, ""] else None
        except ValueError:
            self.target_price = None

        cond_clean = (condition or "ABOVE").upper().strip()
        self.condition = "BELOW" if cond_clean in ["BELOW", "<", "<=", "UNDER"] else "ABOVE"

        self.voice_template = voice_template or "Alert: {ticker} {metric} alert triggered"
        self.enabled = enabled

        if self.mode == "TARGET":
            self.rule_id = f"{self.ticker}_{self.metric}_TARGET_{self.condition}_{self.target_price}"
        else:
            start_suffix = f"_{self.window_start_time_raw}" if self.window_start_time_raw else ""
            self.rule_id = f"{self.ticker}_{self.metric}_{self.window_str}{start_suffix}"

    def get_rule_summary(self) -> str:
        rule_prefix = f"Rule #{self.rule_num}: " if self.rule_num else ""
        if self.mode == "TARGET":
            prefix = "" if self.metric == "IV" else "$"
            suffix = "%" if self.metric == "IV" else ""
            return f"{rule_prefix}{self.ticker} {self.metric} TARGET {self.condition} {prefix}{self.target_price:.2f}{suffix}"
        else:
            unit = "% (vol pts)" if self.metric == "IV" else "%"
            return f"{rule_prefix}{self.ticker} {self.metric} VELOCITY {self.window_str} (Threshold: ±{self.threshold_pct:.1f}{unit})"

    @staticmethod
    def _parse_window(s: str) -> float:
        if not s:
            return 0.0
        s = s.strip().lower()
        try:
            if s.endswith("m"):
                return float(s[:-1]) * 60
            elif s.endswith("h"):
                return float(s[:-1]) * 3600
            elif s.endswith("d"):
                return float(s[:-1]) * 86400
            return float(s)
        except ValueError:
            return 0.0


def determine_rule_window(rule: MonitorRule, now: float = None):
    """
    Evaluates a VELOCITY rule against the current clock time:
    - If user overwrote start_time (T0):
      - If now < T0: Not started yet (future schedule up to 1 day ahead)
      - If elapsed <= window: Anchored to T0
      - If elapsed > window: Resumes to dynamic rolling window (now - window)
    - If no start_time:
      - Dynamic rolling window (now - window)

    Returns: (is_active: bool, target_time: float, mode_str: str, window_desc: str, elapsed_sec: float)
    """
    if now is None:
        now = time.time()
    
    if rule.mode not in ["VELOCITY", "WINDOW"] or rule.window_seconds <= 0:
        return False, None, "TARGET", "", 0.0

    T0 = rule.window_start_ts
    W = rule.window_seconds

    if T0 is not None:
        if now < T0:
            future_dt = datetime.datetime.fromtimestamp(T0)
            wait_min = max(1, int((T0 - now) / 60))
            return False, T0, f"Scheduled (Starts in {wait_min}m at {future_dt.strftime('%H:%M')})", f"starts in {wait_min}m", 0.0
        
        elapsed = now - T0
        if elapsed <= W:
            T0_dt = datetime.datetime.fromtimestamp(T0)
            T0_brief = T0_dt.strftime("%H:%M") if T0_dt.date() == datetime.date.today() else T0_dt.strftime("%b %d %H:%M")
            elapsed_m = max(1, int(elapsed / 60))
            mode_str = f"Anchor ({elapsed_m}m / {rule.window_str} elapsed)"
            window_desc = f"{rule.window_str} window (since {T0_brief})"
            return True, T0, mode_str, window_desc, elapsed
        else:
            target_ts = now - W
            target_dt = datetime.datetime.fromtimestamp(target_ts)
            target_brief = target_dt.strftime("%H:%M") if target_dt.date() == datetime.date.today() else target_dt.strftime("%b %d %H:%M")
            mode_str = f"Rolling {rule.window_str} (Resumed post-anchor)"
            window_desc = f"{rule.window_str} rolling (since {target_brief})"
            return True, target_ts, mode_str, window_desc, W
    else:
        target_ts = now - W
        target_dt = datetime.datetime.fromtimestamp(target_ts)
        target_brief = target_dt.strftime("%H:%M") if target_dt.date() == datetime.date.today() else target_dt.strftime("%b %d %H:%M")
        mode_str = f"Rolling {rule.window_str}"
        window_desc = f"{rule.window_str} rolling (since {target_brief})"
        return True, target_ts, mode_str, window_desc, W


def validate_config_row(row: dict, rule_num: int = None):
    """
    Validates a single row from monitor_config.csv.
    Returns: (is_valid: bool, err_msg: str, rule: MonitorRule or None)
    """
    ticker = (row.get("ticker") or "").strip().upper()
    if not ticker:
        return False, "Validation Error: Missing ticker symbol", None
    if not ticker.replace(".", "").isalnum():
        return False, f"Validation Error: Invalid ticker symbol '{ticker}'", None

    metric = (row.get("metric") or "").strip().upper()
    if metric not in ["PRICE", "IV", "OPT_PRICE"]:
        return False, f"Validation Error: Invalid metric '{metric}' (allowed: PRICE, IV, OPT_PRICE)", None

    mode = (row.get("mode") or "").strip().upper()
    target_raw = (row.get("target_price") or "").strip()
    window_raw = (row.get("time_window") or "").strip()
    threshold_raw = (row.get("threshold_pct") or "").strip()

    if not mode:
        if target_raw:
            mode = "TARGET"
        elif window_raw or threshold_raw:
            mode = "VELOCITY"
        else:
            return False, "Validation Error: Mode not specified (must be TARGET or VELOCITY)", None

    if mode not in ["TARGET", "VELOCITY", "WINDOW"]:
        return False, f"Validation Error: Unknown mode '{mode}' (allowed: TARGET, VELOCITY)", None

    if mode == "TARGET":
        if not target_raw:
            return False, "Validation Error: TARGET mode requires target_price (e.g. 145.0)", None
        try:
            target_val = float(target_raw)
            if target_val <= 0:
                return False, "Validation Error: target_price must be > 0", None
        except ValueError:
            return False, f"Validation Error: target_price '{target_raw}' must be a number", None

        cond = (row.get("condition") or "ABOVE").strip().upper()
        if cond not in ["ABOVE", "BELOW", ">", "<", ">=", "<=", "OVER", "UNDER"]:
            return False, f"Validation Error: Invalid condition '{cond}' (allowed: ABOVE, BELOW)", None

    elif mode in ["VELOCITY", "WINDOW"]:
        if not window_raw:
            return False, "Validation Error: VELOCITY mode requires time_window (e.g. 5m, 30m, 1h, 1d)", None
        w_sec = MonitorRule._parse_window(window_raw)
        if w_sec <= 0:
            return False, f"Validation Error: Invalid time_window '{window_raw}' (use e.g. 5m, 30m, 1h, 1d)", None

        if not threshold_raw:
            return False, "Validation Error: VELOCITY mode requires threshold_pct (e.g. 5.0)", None
        try:
            thresh_val = float(threshold_raw)
            if thresh_val <= 0:
                return False, "Validation Error: threshold_pct must be > 0", None
        except ValueError:
            return False, f"Validation Error: threshold_pct '{threshold_raw}' must be a number", None

        start_time_raw = str(row.get("window_start_time") or "").strip()
        if start_time_raw:
            parsed_ts = parse_datetime_str(start_time_raw)
            if parsed_ts is None:
                return False, f"Validation Error: Invalid window_start_time '{start_time_raw}' (use YYYY-MM-DD HH:MM)", None
            now = time.time()
            if parsed_ts < (now - 10 * 86400):
                return False, "Validation Error: window_start_time cannot be more than 10 days in the past", None
            if parsed_ts > (now + 1 * 86400):
                return False, "Validation Error: window_start_time cannot be more than 1 day in the future", None

    # Check for extra columns from misplaced commas
    if row.get(None):
        return False, f"Validation Error: Too many columns ({len(row.get(None))} extra fields - check commas)", None

    enabled_raw = str(row.get("enabled", "true")).strip().lower()
    if enabled_raw not in ["true", "1", "yes", "false", "0", "no"]:
        return False, f"Validation Error: 'enabled' must be true or false (found: '{row.get('enabled')}')", None
    is_enabled = enabled_raw in ["true", "1", "yes"]

    rule = MonitorRule(
        ticker=ticker,
        metric=metric,
        mode=mode,
        window_str=window_raw,
        threshold_pct=threshold_raw,
        target_price=target_raw,
        condition=row.get("condition", "ABOVE"),
        voice_template=row.get("voice_alert_text", "Alert: {ticker} {metric} alert triggered"),
        enabled=is_enabled,
        window_start_time=row.get("window_start_time", ""),
        rule_num=rule_num
    )
    return True, "", rule


# ==============================================================================
# TWS SOCKET CLIENT & DATA ENGINE
# ==============================================================================
class MultiAssetMonitor(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected_event = threading.Event()
        
        # Re-entrant lock for thread safety
        self.lock = threading.RLock()

        # Rules & Config
        self.rules = []
        self.raw_config_rows = []
        self.config_fieldnames = []
        self.last_config_mtime = 0
        self.rule_cooldowns = {}      # rule_id -> last_fire_timestamp

        # Subscriptions & Stream Status Tracking
        # stream_status[(ticker, metric)] = {
        #     "sub_time": str,
        #     "last_update_time": str,
        #     "stream_state": str,
        #     "tws_error": str or None
        # }
        self.stream_status = {}
        self.last_status_write_time = 0.0
        self.status_write_pending = False

        self.req_id_counter = 1000
        self.stk_sub_map = {}         # ticker -> req_id
        self.stk_req_to_ticker = {}   # req_id -> ticker

        self.opt_sub_map = {}         # ticker -> (req_id, contract, strike, expiry)
        self.opt_req_to_ticker = {}   # req_id -> ticker
        self.opt_strike_switch_times = {} # ticker -> timestamp of last strike change

        self.spot_prices = {}         # ticker -> latest spot price

        # TWS Historical Baseline Fetching & Telemetry Cache
        self.pending_hist_reqs = {}   # req_id -> {"event": Event, "bars": []}
        self.pending_calc_iv = {}     # req_id -> {"event": Event, "iv": float}
        self.baseline_cache = {}      # rule_id -> {"value": float, "timestamp": float, "mode_str": str, "window_desc": str, "target_ts": float, "fetched_at": float}
        self.baseline_thread = None

    def record_ticker_subscribe(self, ticker: str, metric: str, note: str = ""):
        with self.lock:
            st = self.stream_status.setdefault((ticker, metric), {})
            st["sub_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st["tws_error"] = None
            if not st.get("stream_state") or not st["stream_state"].startswith("OK"):
                st["stream_state"] = note or "Subscribed (Awaiting market data)"

    def record_ticker_update(self, ticker: str, metric: str, stream_state: str):
        with self.lock:
            st = self.stream_status.setdefault((ticker, metric), {})
            st["last_update_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st["stream_state"] = stream_state
            st["tws_error"] = None

    def set_ticker_error(self, ticker: str, metric: str, err_msg: str):
        with self.lock:
            st = self.stream_status.setdefault((ticker, metric), {})
            st["tws_error"] = err_msg

    def request_status_update(self, immediate: bool = False):
        now = time.time()
        if immediate or (now - self.last_status_write_time >= 1.0):
            self.write_status_csv()
        else:
            self.status_write_pending = True

    def write_status_csv(self):
        with self.lock:
            if not self.raw_config_rows:
                return

            base_fields = [f for f in self.config_fieldnames if f not in [
                "last_successful_subscribe_time", "last_received_update_time", 
                "baseline_start_time", "baseline_price", "window_mode", "failed_reason"
            ]]
            fieldnames = base_fields + [
                "last_successful_subscribe_time",
                "last_received_update_time",
                "baseline_start_time",
                "baseline_price",
                "window_mode",
                "failed_reason"
            ]

            status_rows = []
            for idx, row in enumerate(self.raw_config_rows, start=1):
                try:
                    row_out = {k: row.get(k, "") for k in base_fields}
                    ticker = (row.get("ticker") or "").strip().upper()
                    metric = (row.get("metric") or "").strip().upper()
                    mode = (row.get("mode") or "").strip().upper()
                    enabled_raw = str(row.get("enabled", "true")).strip().lower()
                    is_enabled = enabled_raw in ["true", "1", "yes"]

                    is_valid, err_msg, rule = validate_config_row(row, rule_num=idx)

                    if not is_valid:
                        row_out["last_successful_subscribe_time"] = ""
                        row_out["last_received_update_time"] = ""
                        row_out["baseline_start_time"] = "-"
                        row_out["baseline_price"] = "-"
                        row_out["window_mode"] = "-"
                        row_out["failed_reason"] = err_msg
                    elif not is_enabled:
                        st = self.stream_status.get((ticker, metric), {})
                        row_out["last_successful_subscribe_time"] = st.get("sub_time", "")
                        row_out["last_received_update_time"] = st.get("last_update_time", "")
                        row_out["baseline_start_time"] = "-"
                        row_out["baseline_price"] = "-"
                        row_out["window_mode"] = "Disabled"
                        row_out["failed_reason"] = "Disabled"
                    else:
                        st = self.stream_status.get((ticker, metric), {})
                        sub_t = st.get("sub_time", "")
                        upd_t = st.get("last_update_time", "")
                        err = st.get("tws_error")
                        state = st.get("stream_state")

                        row_out["last_successful_subscribe_time"] = sub_t
                        row_out["last_received_update_time"] = upd_t

                        # Telemetry for baseline starting point and window mode
                        if rule and rule.mode in ["VELOCITY", "WINDOW"]:
                            base_info = self.baseline_cache.get(rule.rule_id)
                            if base_info and base_info.get("timestamp"):
                                b_ts = base_info["timestamp"]
                                row_out["baseline_start_time"] = datetime.datetime.fromtimestamp(b_ts).strftime("%Y-%m-%d %H:%M:%S")
                                b_val = base_info.get("value")
                                if b_val is not None:
                                    row_out["baseline_price"] = f"{b_val:.2f}%" if metric == "IV" else f"${b_val:.2f}"
                                else:
                                    row_out["baseline_price"] = "Pending TWS"
                                row_out["window_mode"] = base_info.get("mode_str", "-")
                            else:
                                _, target_ts, mode_str, _, _ = determine_rule_window(rule)
                                row_out["baseline_start_time"] = datetime.datetime.fromtimestamp(target_ts).strftime("%Y-%m-%d %H:%M:%S") if target_ts else "Pending"
                                row_out["baseline_price"] = "Fetching TWS..."
                                row_out["window_mode"] = mode_str
                        elif rule and rule.mode == "TARGET":
                            row_out["baseline_start_time"] = "-"
                            row_out["baseline_price"] = f"${rule.target_price:.2f}" if metric != "IV" else f"{rule.target_price:.1f}%"
                            row_out["window_mode"] = f"Target {rule.condition}"
                        else:
                            row_out["baseline_start_time"] = "-"
                            row_out["baseline_price"] = "-"
                            row_out["window_mode"] = "-"

                        if not self.isConnected():
                            row_out["failed_reason"] = "TWS Not Connected"
                        elif err:
                            row_out["failed_reason"] = err
                        elif state:
                            row_out["failed_reason"] = state
                        elif sub_t:
                            row_out["failed_reason"] = "Subscribed (Awaiting market data)"
                        else:
                            row_out["failed_reason"] = "Subscribing..."

                    status_rows.append(row_out)
                except Exception as row_err:
                    row_fallback = {k: row.get(k, "") for k in base_fields}
                    row_fallback["last_successful_subscribe_time"] = ""
                    row_fallback["last_received_update_time"] = ""
                    row_fallback["baseline_start_time"] = "-"
                    row_fallback["baseline_price"] = "-"
                    row_fallback["window_mode"] = "-"
                    row_fallback["failed_reason"] = f"Processing Error: {row_err}"
                    status_rows.append(row_fallback)

            try:
                temp_path = STATUS_CSV_PATH + ".tmp"
                with open(temp_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(status_rows)
                os.replace(temp_path, STATUS_CSV_PATH)
                self.last_status_write_time = time.time()
                self.status_write_pending = False
            except Exception as e:
                print(f"[!] Error writing {STATUS_CSV_PATH}: {e}", flush=True)

    def _next_req_id(self) -> int:
        self.req_id_counter += 1
        return self.req_id_counter

    def connectAck(self):
        super().connectAck()
        print("[+] TWS connection acknowledged.", flush=True)

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        self.connected_event.set()
        print(f"[+] Connected to TWS. Next valid order ID: {orderId}", flush=True)
        # 1 = Real-Time streaming market data (falls back automatically to 3 if exchange requires extra subscription)
        self.reqMarketDataType(1)
        self.request_status_update(immediate=True)

        # Start TWS historical baseline background worker if not active
        if not self.baseline_thread or not self.baseline_thread.is_alive():
            self.baseline_thread = threading.Thread(target=self._baseline_worker_loop, daemon=True)
            self.baseline_thread.start()

    def marketDataType(self, reqId: int, marketDataType: int):
        ticker_stk = self.stk_req_to_ticker.get(reqId)
        ticker_opt = self.opt_req_to_ticker.get(reqId)
        type_names = {1: "Real-Time Live", 2: "Frozen", 3: "Delayed", 4: "Delayed-Frozen"}
        desc = type_names.get(marketDataType, f"Type {marketDataType}")
        if ticker_stk:
            print(f"[*] Market data stream for stock {ticker_stk}: {desc}", flush=True)
        elif ticker_opt:
            print(f"[*] Market data stream for option {ticker_opt}: {desc}", flush=True)

    def historicalData(self, reqId: int, bar):
        with self.lock:
            if reqId in self.pending_hist_reqs:
                self.pending_hist_reqs[reqId]['bars'].append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        with self.lock:
            if reqId in self.pending_hist_reqs:
                self.pending_hist_reqs[reqId]['event'].set()

    def fetch_tws_historical_stock_price(self, ticker: str, target_time: float, timeout: float = 4.0):
        """
        Queries TWS via reqHistoricalData to find the exact stock price at target_time.
        Works across sleep/shutdown gaps and spans up to 10 days in the past.
        """
        if not self.isConnected():
            return None, None

        contract = Contract()
        contract.symbol = ticker
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        # Format endDateTime for TWS in UTC
        dt_utc = datetime.datetime.fromtimestamp(target_time, datetime.timezone.utc)
        end_dt_str = dt_utc.strftime("%Y%m%d %H:%M:%S UTC")

        req_id = self._next_req_id()
        evt = threading.Event()
        with self.lock:
            self.pending_hist_reqs[req_id] = {'event': evt, 'bars': []}

        try:
            self.reqHistoricalData(req_id, contract, end_dt_str, "1800 S", "1 min", "TRADES", 0, 2, False, [])
            evt.wait(timeout=timeout)
            with self.lock:
                req_data = self.pending_hist_reqs.pop(req_id, None)
            
            bars = req_data['bars'] if req_data else []
            if bars:
                last_bar = bars[-1]
                try:
                    bar_ts = float(last_bar.date)
                except ValueError:
                    bar_ts = target_time
                # Only accept bar if it is reasonably fresh near target_time (within 30 mins)
                if abs(bar_ts - target_time) <= 1800:
                    return last_bar.close, bar_ts
        except Exception as e:
            print(f"[!] Error fetching TWS historical stock price for {ticker}: {e}", flush=True)
            with self.lock:
                self.pending_hist_reqs.pop(req_id, None)

        return None, None

    def fetch_tws_historical_option_iv(self, ticker: str, target_time: float, timeout: float = 4.0):
        """
        Reconstructs historical ATM Option IV at target_time by:
        1. Fetching historical stock price at target_time from TWS.
        2. Snapping to the ATM strike.
        3. Fetching historical option trade price at target_time from TWS.
        4. Invoking TWS's native Model engine (calculateImpliedVolatility) directly on port 7496.
        Survives Mac sleep, hibernation, and multi-day shutdowns without local DB logs.
        """
        if not self.isConnected():
            return None, None

        # 1. Fetch historical stock price at target_time
        hist_spot, spot_ts = self.fetch_tws_historical_stock_price(ticker, target_time, timeout=timeout)
        if hist_spot is None or hist_spot <= 0 or spot_ts is None:
            # When market is closed or no fresh bars exist near target_time, fallback to local DB
            return None, None

        # 2. Determine ATM contract
        nearest_strike = round(hist_spot)
        expiry = self._get_default_friday_expiry()

        opt = Contract()
        opt.symbol = ticker
        opt.secType = "OPT"
        opt.exchange = "SMART"
        opt.currency = "USD"
        opt.lastTradeDateOrContractMonth = expiry
        opt.strike = float(nearest_strike)
        opt.right = "C"
        opt.multiplier = "100"

        # 3. Fetch historical option trade price at target_time
        dt_utc = datetime.datetime.fromtimestamp(target_time, datetime.timezone.utc)
        end_dt_str = dt_utc.strftime("%Y%m%d %H:%M:%S UTC")

        req_id_opt = self._next_req_id()
        evt_opt = threading.Event()
        with self.lock:
            self.pending_hist_reqs[req_id_opt] = {'event': evt_opt, 'bars': []}

        hist_opt_price = None
        try:
            self.reqHistoricalData(req_id_opt, opt, end_dt_str, "1800 S", "1 min", "TRADES", 0, 2, False, [])
            evt_opt.wait(timeout=timeout)
            with self.lock:
                opt_data = self.pending_hist_reqs.pop(req_id_opt, None)
            opt_bars = opt_data['bars'] if opt_data else []
            if opt_bars:
                last_opt = opt_bars[-1]
                try:
                    opt_ts = float(last_opt.date)
                except ValueError:
                    opt_ts = target_time
                # Option trade must be contemporaneous with the spot bar (within 5 mins)
                if abs(opt_ts - spot_ts) <= 300:
                    hist_opt_price = last_opt.close
        except Exception:
            with self.lock:
                self.pending_hist_reqs.pop(req_id_opt, None)

        if hist_opt_price is None or hist_opt_price <= 0:
            return None, None

        # 4. Invoke TWS's internal Model engine directly via socket (avoiding Error 320)
        calc_req_id = self._next_req_id()
        evt_calc = threading.Event()
        with self.lock:
            self.pending_calc_iv[calc_req_id] = {'event': evt_calc, 'iv': None}

        try:
            msg = ''.join([
                make_field(54), make_field(3), make_field(calc_req_id),
                make_field(0), make_field(opt.symbol), make_field(opt.secType),
                make_field(opt.lastTradeDateOrContractMonth), make_field(opt.strike),
                make_field(opt.right), make_field(opt.multiplier), make_field(opt.exchange),
                make_field(''), make_field(opt.currency), make_field(''),
                make_field(ticker), make_field(hist_opt_price), make_field(hist_spot), make_field('')
            ])
            self.sendMsg(msg)
            evt_calc.wait(timeout=timeout)
            with self.lock:
                calc_res = self.pending_calc_iv.pop(calc_req_id, None)

            if calc_res and calc_res.get('iv') is not None:
                return calc_res['iv'], target_time
        except Exception as e:
            print(f"[!] Error invoking TWS Model for historical option IV: {e}", flush=True)
            with self.lock:
                self.pending_calc_iv.pop(calc_req_id, None)

        return None, None

    def _baseline_worker_loop(self):
        """Background worker keeping TWS historical baselines refreshed for all active VELOCITY rules."""
        print("[*] TWS historical baseline worker loop started.", flush=True)
        while True:
            try:
                if self.isConnected():
                    now = time.time()
                    with self.lock:
                        vel_rules = [r for r in self.rules if r.enabled and r.mode in ["VELOCITY", "WINDOW"]]

                    for rule in vel_rules:
                        try:
                            is_active, target_ts, mode_str, window_desc, _ = determine_rule_window(rule, now)
                            if not is_active:
                                with self.lock:
                                    self.baseline_cache[rule.rule_id] = {
                                        "value": None,
                                        "timestamp": target_ts,
                                        "mode_str": mode_str,
                                        "window_desc": window_desc,
                                        "target_ts": target_ts,
                                        "fetched_at": now
                                    }
                                continue

                            cached = self.baseline_cache.get(rule.rule_id)
                            # Cache check: if within 30s of target_ts and fetched within 45s
                            if (cached and cached.get("value") is not None and 
                                abs(cached.get("target_ts", 0) - target_ts) < 30.0 and 
                                (now - cached.get("fetched_at", 0)) < 45.0):
                                cached["mode_str"] = mode_str
                                cached["window_desc"] = window_desc
                                continue

                            val = None
                            ts = None
                            if rule.metric == "PRICE":
                                tws_val, tws_ts = self.fetch_tws_historical_stock_price(rule.ticker, target_ts)
                                val = tws_val
                                ts = tws_ts
                                if val is None:
                                    val, ts = get_prior_value(rule.ticker, "PRICE", target_ts)
                            elif rule.metric in ["IV", "OPT_PRICE"]:
                                # 1. On-demand TWS historical model computation across sleep gap
                                tws_iv, tws_ts = self.fetch_tws_historical_option_iv(rule.ticker, target_ts)
                                val = tws_iv
                                ts = tws_ts
                                # 2. Fallback to local SQLite history if option bar has no trades (e.g. market closed)
                                if val is None:
                                    val, ts = get_prior_value(rule.ticker, rule.metric, target_ts)
                                    if val is None:
                                        with sqlite3.connect(DB_PATH) as conn:
                                            c = conn.cursor()
                                            c.execute("SELECT value, timestamp FROM history WHERE ticker = ? AND metric = ? AND strike IS NOT NULL ORDER BY timestamp DESC LIMIT 1", (rule.ticker, rule.metric))
                                            r = c.fetchone()
                                            if r:
                                                val, ts = r

                            if val is not None:
                                with self.lock:
                                    self.baseline_cache[rule.rule_id] = {
                                        "value": val,
                                        "timestamp": ts or target_ts,
                                        "mode_str": mode_str,
                                        "window_desc": window_desc,
                                        "target_ts": target_ts,
                                        "fetched_at": now
                                    }
                        except Exception as r_err:
                            print(f"[!] Error updating baseline for {rule.rule_id}: {r_err}", flush=True)
                        time.sleep(0.35)

                    self.request_status_update(immediate=False)
            except Exception as e:
                print(f"[!] Baseline worker error: {e}", flush=True)
            time.sleep(15.0)

    def connectionClosed(self):
        super().connectionClosed()
        self.connected_event.clear()
        print("[!] TWS connection closed.", flush=True)
        self.request_status_update(immediate=True)

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson=""):
        # Ignore routine connection status / farm resets
        if errorCode in [2103, 2104, 2105, 2106, 2157, 2158, 2176, 2186]:
            return

        ticker_stk = self.stk_req_to_ticker.get(reqId)
        ticker_opt = self.opt_req_to_ticker.get(reqId)

        if ticker_stk:
            err = f"TWS Error {errorCode}: {errorString}"
            if errorCode == 200:
                err = f"TWS Error 200: No security definition found for '{ticker_stk}'"
            elif errorCode == 354:
                err = f"TWS Warning 354: Market data not subscribed"
            elif errorCode == 10091:
                err = f"TWS Notice 10091: Delayed market data available"

            st = self.stream_status.get((ticker_stk, "PRICE"), {})
            if errorCode == 200 or not st.get("stream_state", "").startswith("OK"):
                self.set_ticker_error(ticker_stk, "PRICE", err)
                self.request_status_update(immediate=True)

        elif ticker_opt:
            err = f"TWS Error {errorCode}: {errorString}"
            if errorCode == 200:
                err = f"TWS Error 200: Option contract definition not found for '{ticker_opt}'"
            elif errorCode == 354:
                err = f"TWS Warning 354: Option market data not subscribed"
            elif errorCode == 10091:
                err = f"TWS Notice 10091: Delayed option market data available"

            st = self.stream_status.get((ticker_opt, "IV"), {})
            if errorCode == 200 or not st.get("stream_state", "").startswith("OK"):
                self.set_ticker_error(ticker_opt, "IV", err)
                self.request_status_update(immediate=True)

        if errorCode not in [300, 354, 10091]:
            print(f"[TWS Status/Error {errorCode}] ReqId {reqId}: {errorString}", flush=True)

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib):
        if price <= 0:
            return

        # Check if this reqId belongs to a Stock
        ticker = self.stk_req_to_ticker.get(reqId)
        if not ticker:
            return

        # Real-time ticks: 4=Last, 1=Bid, 2=Ask, 9=Close
        # Delayed ticks: 68=Delayed Last, 66=Delayed Bid, 67=Delayed Ask, 75=Delayed Close
        if tickType in [4, 68, 1, 2, 66, 67, 9, 75]:
            is_delayed = tickType in [66, 67, 68, 75]
            is_close_tick = tickType in [9, 75]

            prev_spot = self.spot_prices.get(ticker)
            # CRITICAL: Do NOT allow previous day's CLOSE (9 or 75) to overwrite actual LAST or BID/ASK spot price
            if is_close_tick and prev_spot is not None:
                return

            stream_state = f"OK (Streaming Delayed: ${price:.2f})" if is_delayed else f"OK (Streaming Live: ${price:.2f})"
            self.record_ticker_update(ticker, "PRICE", stream_state)
            self.request_status_update(immediate=False)

            if prev_spot is None or abs(price - prev_spot) > 0.02:
                self.spot_prices[ticker] = price
                # 1. Record stock price in SQLite
                record_tick(ticker, "PRICE", price, spot=price)
                
                # 2. Evaluate PRICE rules
                self.evaluate_rules(ticker, "PRICE", price)

                # 3. Check if ATM option needs subscription or strike adjustment
                self._update_atm_option_subscription(ticker, price)

    def tickOptionComputation(self, reqId: int, tickType: int, tickAttrib, impliedVol: float, delta: float, 
                              optPrice: float, pvDividend: float, gamma: float, vega: float, theta: float, undPrice: float):
        """
        Receives TWS options computations.
        tickType 13 = MODEL_OPTION (Real-time IBKR model IV & Greeks)
        tickType 83 = DELAYED_MODEL_OPTION (Delayed IBKR model IV & Greeks)
        """
        # 1. Check if this is an on-demand calculateImpliedVolatility response
        with self.lock:
            if reqId in self.pending_calc_iv:
                if impliedVol is not None and impliedVol > 0:
                    self.pending_calc_iv[reqId]['iv'] = impliedVol * 100.0
                self.pending_calc_iv[reqId]['event'].set()
                return

        # 2. Otherwise process live streaming tick
        if tickType not in [13, 83] or impliedVol is None or impliedVol <= 0:
            return

        ticker = self.opt_req_to_ticker.get(reqId)
        if not ticker:
            return

        iv_pct = impliedVol * 100.0
        spot = undPrice if (undPrice and undPrice > 0) else self.spot_prices.get(ticker, 0.0)

        with self.lock:
            sub_info = self.opt_sub_map.get(ticker)
            strike = sub_info[2] if sub_info else None
            expiry = sub_info[3] if sub_info else None

        # 1. Persist IV tick in SQLite
        record_tick(ticker, "IV", iv_pct, strike=strike, expiry=expiry, spot=spot)

        # 2. Evaluate IV rules
        self.evaluate_rules(ticker, "IV", iv_pct, current_spot=spot, strike=strike)

    def evaluate_rules(self, ticker: str, metric: str, current_value: float, current_spot: float = None, strike: float = None):
        """Checks all active rules matching (ticker, metric) in both TARGET and VELOCITY modes."""
        now = time.time()
        matching_rules = [r for r in self.rules if r.enabled and r.ticker == ticker and r.metric == metric]

        for rule in matching_rules:
            try:
                # ==========================================================
                # MODE 1: TARGET PRICE / TARGET VALUE MODE
                # ==========================================================
                if rule.mode == "TARGET" and rule.target_price is not None:
                    is_hit = False
                    if rule.condition == "ABOVE" and current_value >= rule.target_price:
                        is_hit = True
                    elif rule.condition == "BELOW" and current_value <= rule.target_price:
                        is_hit = True

                    if is_hit:
                        prefix = "" if metric == "IV" else "$"
                        suffix = "%" if metric == "IV" else ""
                        cond_str = "rose above" if rule.condition == "ABOVE" else "fell below"
                        rule_summary = rule.get_rule_summary()
                        rule_num_str = f"#{rule.rule_num}" if rule.rule_num else "Target"
                        
                        title = f"🎯 {ticker} {metric} Target Alert ({cond_str} {prefix}{rule.target_price:.2f}{suffix})"
                        msg = (
                            f"{ticker} {metric} {cond_str} Target {prefix}{rule.target_price:.2f}{suffix}!\n"
                            f"• Rule Number: {rule_num_str}\n"
                            f"• Triggered Rule: {rule_summary}\n"
                            f"• Current {metric}: {prefix}{current_value:.2f}{suffix}\n"
                            f"• Target {metric}: {prefix}{rule.target_price:.2f}{suffix}\n"
                            f"• Spot Price: ${current_spot or self.spot_prices.get(ticker, 0.0):.2f}"
                        )
                        if strike:
                            msg += f"\n• ATM Strike: ${strike:.1f}"

                        target_val = rule.target_price
                        target_clean = f"{target_val:.2f}".rstrip("0").rstrip(".") if target_val is not None else ""
                        target_fmt = f"{rule.target_price:.1f}%" if metric == "IV" else f"${rule.target_price:.2f}"
                        cond_spoken = "rose above" if rule.condition == "ABOVE" else "fell below"
                        spot_val = current_spot or self.spot_prices.get(ticker, 0.0)

                        voice_vars = {
                            "ticker": ticker,
                            "metric": metric,
                            "price": target_clean or rule.target_price,
                            "target": target_clean or rule.target_price,
                            "target_price": target_clean or rule.target_price,
                            "target_fmt": target_fmt,
                            "price_fmt": target_fmt,
                            "current": current_value,
                            "current_price": current_value,
                            "current_fmt": f"{current_value:.1f}%" if metric == "IV" else f"${current_value:.2f}",
                            "condition": cond_spoken,
                            "raw_condition": rule.condition,
                            "spot": spot_val,
                            "spot_price": spot_val,
                            "spot_fmt": f"${spot_val:.2f}"
                        }

                        raw_tpl = (rule.voice_template or "").strip()
                        if not raw_tpl:
                            if metric == "IV":
                                raw_tpl = "Warning: {ticker} ATM IV {condition} {target} percent"
                            else:
                                raw_tpl = "Alert: {ticker} {condition} target price {price}"

                        try:
                            voice_text = raw_tpl.format(**voice_vars)
                        except Exception:
                            voice_text = raw_tpl
                            for k, v in voice_vars.items():
                                voice_text = voice_text.replace(f"{{{k}}}", str(v))

                        # If user's template does not mention the target price at all, automatically append it
                        if target_clean and (target_clean not in voice_text and str(rule.target_price) not in voice_text):
                            voice_text += f" at {target_clean}"

                        AlertRouter.handle_rule_trigger(
                            rule=rule,
                            current_val=current_value,
                            spot=current_spot or self.spot_prices.get(ticker, 0.0),
                            strike=strike,
                            title=title,
                            msg=msg,
                            voice_text=voice_text,
                            desktop_dispatch_fn=dispatch_alert
                        )
                    else:
                        AlertRouter.handle_rule_cleared(rule.rule_id)
                    continue

                # ==========================================================
                # MODE 2: ROLLING WINDOW / VELOCITY MODE
                # (Compares current value against historical baseline fetched from TWS)
                # ==========================================================
                elif rule.mode in ["VELOCITY", "WINDOW"] and rule.window_seconds > 0:
                    base_info = self.baseline_cache.get(rule.rule_id)
                    prior_val = base_info.get("value") if base_info else None
                    prior_ts = base_info.get("timestamp") if base_info else None
                    window_desc = base_info.get("window_desc") if (base_info and base_info.get("window_desc")) else f"{rule.window_str} window"

                    if prior_val is None or prior_val <= 0:
                        # Fallback to local SQLite if TWS hasn't cached yet
                        target_time = now - rule.window_seconds
                        prior_val, prior_ts = get_prior_value(ticker, metric, target_time)
                        if prior_val is None or prior_val <= 0:
                            continue

                    delta = current_value - prior_val
                    
                    if metric == "IV":
                        # Volatility points displacement (e.g. 95.0% - 90.0% = 5.0 vol pts)
                        pct_change = abs(delta)
                        unit = "% (vol pts)"
                    else:
                        # Price percentage displacement (e.g. ($145 - $140) / $140 * 100 = 3.57%)
                        pct_change = (abs(delta) / prior_val) * 100.0
                        unit = "%"

                    if pct_change >= rule.threshold_pct:
                        direction = "SPIKED" if delta > 0 else "DROPPED"
                        elapsed_m = max(1, int((now - (prior_ts or (now - rule.window_seconds))) / 60))
                        
                        if metric == "IV":
                            title = f"⚠️ {ticker} {metric} {direction} ({delta:+.2f}{unit})"
                            move_detail = f"moved by {delta:+.2f} vol pts ({current_value:.1f}% vs {prior_val:.1f}%)"
                        else:
                            title = f"⚠️ {ticker} {metric} {direction} ({pct_change:.1f}%, ${delta:+.2f})"
                            move_detail = f"moved by {pct_change:.1f}% (${delta:+.2f})"

                        rule_summary = rule.get_rule_summary()
                        rule_num_str = f"#{rule.rule_num}" if rule.rule_num else "Velocity"
                        msg = (
                            f"{ticker} {metric} {move_detail} over {window_desc}!\n"
                            f"• Rule Number: {rule_num_str}\n"
                            f"• Triggered Rule: {rule_summary}\n"
                            f"• Current {metric}: {current_value:.2f}\n"
                            f"• Baseline {metric} ({window_desc}): {prior_val:.2f}\n"
                            f"• Spot Price: ${current_spot or self.spot_prices.get(ticker, 0.0):.2f}"
                        )
                        if strike:
                            msg += f"\n• ATM Strike: ${strike:.1f}"

                        delta_val = pct_change if metric != "IV" else abs(delta)
                        delta_clean = f"{delta_val:.1f}"
                        spot_val = current_spot or self.spot_prices.get(ticker, 0.0)
                        curr_clean = f"{current_value:.2f}".rstrip("0").rstrip(".")

                        voice_vars = {
                            "ticker": ticker,
                            "metric": metric,
                            "price": curr_clean,
                            "current": curr_clean,
                            "current_price": curr_clean,
                            "current_fmt": f"{current_value:.1f}%" if metric == "IV" else f"${current_value:.2f}",
                            "prior": f"{prior_val:.2f}".rstrip("0").rstrip("."),
                            "prior_price": f"{prior_val:.2f}".rstrip("0").rstrip("."),
                            "delta": delta_val,
                            "delta_fmt": f"{delta_clean}%",
                            "window": rule.window_str,
                            "window_brief": window_desc,
                            "spot": spot_val,
                            "spot_price": spot_val,
                            "direction": direction
                        }

                        raw_tpl = (rule.voice_template or "").strip()
                        if not raw_tpl:
                            if metric == "IV":
                                raw_tpl = "Attention: {ticker} ATM IV changed by {delta:.1f} percent over {window_brief}"
                            else:
                                raw_tpl = "Notice: {ticker} stock price changed by {delta:.1f} percent over {window_brief}"

                        try:
                            voice_text = raw_tpl.format(**voice_vars)
                        except Exception:
                            voice_text = raw_tpl
                            for k, v in voice_vars.items():
                                voice_text = voice_text.replace(f"{{{k}}}", str(v))

                        AlertRouter.handle_rule_trigger(
                            rule=rule,
                            current_val=current_value,
                            spot=current_spot or self.spot_prices.get(ticker, 0.0),
                            strike=strike,
                            title=title,
                            msg=msg,
                            voice_text=voice_text,
                            desktop_dispatch_fn=dispatch_alert
                        )
                    else:
                        AlertRouter.handle_rule_cleared(rule.rule_id)
            except Exception as rule_err:
                print(f"[!] Error evaluating rule {getattr(rule, 'rule_id', rule)}: {rule_err}", flush=True)

    def _update_atm_option_subscription(self, ticker: str, spot: float):
        """Snaps to nearest ATM strike and subscribes/adjusts option feed."""
        # Check if any active rule cares about IV or OPT_PRICE for this ticker
        wants_opt = any(r.enabled and r.ticker == ticker and r.metric in ["IV", "OPT_PRICE"] for r in self.rules)
        if not wants_opt or spot <= 0:
            return

        nearest_strike = round(spot)
        with self.lock:
            existing = self.opt_sub_map.get(ticker)
            if existing:
                req_id, contract, cur_strike, cur_expiry = existing
                if cur_strike == nearest_strike:
                    return # Already on optimal ATM strike

                # 1. HYSTERESIS / DEADBAND:
                # If current strike is still within 0.65 of spot, do NOT flip.
                # Prevents whipsawing when spot oscillates around .50 boundary (e.g. 140.48 vs 140.52).
                dist_cur = abs(spot - cur_strike)
                if dist_cur <= 0.65:
                    return

                # 2. DWELL TIME COOLDOWN:
                # Limit strike switches to at most once per 30 seconds unless a large move (> 2.0) occurs.
                now = time.time()
                last_switch = self.opt_strike_switch_times.get(ticker, 0)
                if (now - last_switch < 30.0) and dist_cur < 2.0:
                    return

                # Strike decisively drifted: cancel old feed
                self.opt_strike_switch_times[ticker] = now
                self.cancelMktData(req_id)
                del self.opt_req_to_ticker[req_id]

            # Determine upcoming Friday expiry
            expiry = self._get_default_friday_expiry()
            opt = Contract()
            opt.symbol = ticker
            opt.secType = "OPT"
            opt.exchange = "SMART"
            opt.currency = "USD"
            opt.lastTradeDateOrContractMonth = expiry
            opt.strike = float(nearest_strike)
            opt.right = "C"
            opt.multiplier = "100"

            req_id = self._next_req_id()
            self.opt_sub_map[ticker] = (req_id, opt, nearest_strike, expiry)
            self.opt_req_to_ticker[req_id] = ticker

            print(f"[*] Subscribing to {ticker} ATM Option: {expiry} ${nearest_strike}C (Spot: ${spot:.2f})...", flush=True)
            self.reqMktData(req_id, opt, "100,101,106", False, False, [])
            self.record_ticker_subscribe(ticker, "IV", f"Subscribed ATM ${nearest_strike}C (Awaiting data)")
            self.request_status_update(immediate=True)

    def _get_default_friday_expiry(self) -> str:
        today = datetime.date.today()
        days_ahead = 4 - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        target = today + datetime.timedelta(days_ahead)
        return target.strftime("%Y%m%d")

    def reload_config_file(self):
        """Reads monitor_config.csv, validates rows, syncs TWS subscriptions, and writes monitor_status.csv."""
        if not os.path.exists(CONFIG_CSV_PATH):
            return

        mtime = os.path.getmtime(CONFIG_CSV_PATH)
        if mtime == self.last_config_mtime:
            return

        print(f"\n[*] Detected update to monitor_config.csv. Validating and reloading rules...", flush=True)
        raw_rows = []
        new_rules = []
        fieldnames = []
        try:
            with open(CONFIG_CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                for idx, row in enumerate(reader, start=1):
                    # Ignore completely blank lines
                    if not any(v.strip() for v in row.values() if v):
                        continue
                    raw_rows.append(row)
                    try:
                        is_valid, err_msg, rule = validate_config_row(row, rule_num=idx)
                        if is_valid and rule:
                            new_rules.append(rule)
                        elif not is_valid:
                            print(f"[!] Validation warning for {row.get('ticker')}: {err_msg}", flush=True)
                    except Exception as e:
                        print(f"[!] Unexpected error validating row {row}: {e}", flush=True)
        except Exception as e:
            print(f"[!] Error parsing {CONFIG_CSV_PATH}: {e}", flush=True)
            return

        self.last_config_mtime = mtime
        self.raw_config_rows = raw_rows
        self.config_fieldnames = fieldnames
        self.rules = new_rules

        # Synchronize Stock & Option subscriptions
        active_tickers = set(r.ticker for r in self.rules if r.enabled)
        print(f"[+] Active monitored tickers: {sorted(list(active_tickers))}", flush=True)

        with self.lock:
            # 1. Unsubscribe removed stocks
            for ticker in list(self.stk_sub_map.keys()):
                if ticker not in active_tickers:
                    try:
                        req_id = self.stk_sub_map.pop(ticker)
                        self.stk_req_to_ticker.pop(req_id, None)
                        if self.isConnected():
                            self.cancelMktData(req_id)
                        print(f"[-] Unsubscribed stock: {ticker}", flush=True)
                        if (ticker, "PRICE") in self.stream_status:
                            self.stream_status[(ticker, "PRICE")]["stream_state"] = "Unsubscribed"
                    except Exception as e:
                        print(f"[!] Error unsubscribing stock {ticker}: {e}", flush=True)

            # 2. Unsubscribe removed options
            for ticker in list(self.opt_sub_map.keys()):
                if not any(r.enabled and r.ticker == ticker and r.metric == "IV" for r in self.rules):
                    try:
                        req_id, _, _, _ = self.opt_sub_map.pop(ticker)
                        self.opt_req_to_ticker.pop(req_id, None)
                        if self.isConnected():
                            self.cancelMktData(req_id)
                        print(f"[-] Unsubscribed ATM option: {ticker}", flush=True)
                        if (ticker, "IV") in self.stream_status:
                            self.stream_status[(ticker, "IV")]["stream_state"] = "Unsubscribed"
                    except Exception as e:
                        print(f"[!] Error unsubscribing option {ticker}: {e}", flush=True)

            # 3. Subscribe newly added stocks (if connected to TWS)
            for ticker in active_tickers:
                try:
                    if self.isConnected():
                        if ticker not in self.stk_sub_map:
                            req_id = self._next_req_id()
                            self.stk_sub_map[ticker] = req_id
                            self.stk_req_to_ticker[req_id] = ticker

                            stk = Contract()
                            stk.symbol = ticker
                            stk.secType = "STK"
                            stk.exchange = "SMART"
                            stk.currency = "USD"
                            print(f"[+] Subscribing to stock: {ticker} (ReqId {req_id})...", flush=True)
                            self.reqMktData(req_id, stk, "", False, False, [])
                            self.record_ticker_subscribe(ticker, "PRICE", "Subscribed (Awaiting market data)")

                            # Bootstrap ATM option from cached spot in SQLite if available
                            if any(r.enabled and r.ticker == ticker and r.metric == "IV" for r in self.rules):
                                if ticker not in self.spot_prices:
                                    try:
                                        with sqlite3.connect(DB_PATH) as conn:
                                            c = conn.cursor()
                                            c.execute("SELECT spot FROM history WHERE ticker = ? AND spot > 0 ORDER BY timestamp DESC LIMIT 1", (ticker,))
                                            row_db = c.fetchone()
                                            if row_db and row_db[0]:
                                                self.spot_prices[ticker] = row_db[0]
                                                self._update_atm_option_subscription(ticker, row_db[0])
                                            else:
                                                self.record_ticker_subscribe(ticker, "IV", "Awaiting stock price to select ATM strike")
                                    except Exception:
                                        pass
                                else:
                                    self._update_atm_option_subscription(ticker, self.spot_prices[ticker])
                        else:
                            # Already subscribed, ensure subscribe timestamp exists
                            if (ticker, "PRICE") not in self.stream_status:
                                self.record_ticker_subscribe(ticker, "PRICE", "Subscribed")
                except Exception as sub_err:
                    print(f"[!] Error subscribing ticker {ticker}: {sub_err}", flush=True)
                    self.set_ticker_error(ticker, "PRICE", f"Subscribe Error: {sub_err}")

        # Write status CSV immediately upon reload
        self.write_status_csv()


def config_watcher_loop(app: MultiAssetMonitor):
    """Background thread continuously polling monitor_config.csv for edits."""
    while True:
        try:
            # 1. Check for config changes and reload immediately
            app.reload_config_file()
            # 2. Flush throttled status write if pending (every >= 1.0s)
            if app.status_write_pending and (time.time() - app.last_status_write_time >= 1.0):
                app.write_status_csv()
        except Exception as e:
            print(f"[Watcher Error]: {e}", flush=True)
        time.sleep(CONFIG_POLL_INTERVAL_SEC)


def run_daemon():
    init_db()
    print("==================================================================", flush=True)
    print("Multi-Asset TWS Velocity Monitor Daemon Starting...", flush=True)
    print(f"Config: {CONFIG_CSV_PATH}", flush=True)
    print(f"Status: {STATUS_CSV_PATH}", flush=True)
    print(f"Database: {DB_PATH}", flush=True)
    print("==================================================================", flush=True)

    while True:
        app = MultiAssetMonitor()
        try:
            # Load initial config and generate monitor_status.csv immediately
            app.reload_config_file()

            # Start config watcher right away
            watcher = threading.Thread(target=config_watcher_loop, args=(app,), daemon=True)
            watcher.start()

            print(f"[*] Connecting to TWS at {TWS_HOST}:{TWS_PORT} (ClientID: {CLIENT_ID})...", flush=True)
            app.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)

            thread = threading.Thread(target=app.run, daemon=True)
            thread.start()

            if app.connected_event.wait(timeout=10):
                print("[+] Connection established. Synchronizing subscriptions...", flush=True)
                # Force resync with live TWS connection
                app.last_config_mtime = 0
                app.reload_config_file()

                while app.isConnected():
                    time.sleep(5)
            else:
                print("[-] Could not connect to TWS. Retrying...", flush=True)

        except Exception as e:
            print(f"[!] Error encountered in monitor loop: {e}", flush=True)

        finally:
            if app.isConnected():
                try:
                    app.disconnect()
                except Exception:
                    pass

        print("[*] Reconnecting in 15 seconds...", flush=True)
        time.sleep(15)


# ==============================================================================
# TEST UTILITIES
# ==============================================================================
def run_test_alert():
    print("[*] Testing Alert System with template...")
    sample_title = "⚠️ [TEST] SOXL IV SPIKED (+5.2%)"
    sample_msg = (
        "TEST ALERT: SOXL IV spiked by +5.20% over 5m!\n"
        "• Current IV: 86.20%\n"
        "• Prior IV (5m ago): 81.00%\n"
        "• SOXL Spot: $139.76\n"
        "• ATM Strike: $140.0"
    )
    sample_voice = "Attention: SOXL ATM IV changed by 5.2 percent over 5 minutes"
    dispatch_alert(sample_title, sample_msg, sample_voice)

def run_test_sim():
    init_db()
    print("[*] Testing Multi-Asset Simulation Engine...")
    now = time.time()
    ticker = "SOXL"
    
    app = MultiAssetMonitor()
    app.rules = [
        # Rule 1: VELOCITY mode (5m rolling window)
        MonitorRule("SOXL", "IV", "VELOCITY", "5m", 5.0, None, "ABOVE", 
                    "Attention: SOXL ATM IV changed by {delta:.1f} percent over 5 minutes", True),
        # Rule 2: TARGET mode (Target price 145.0, max 1 alert per 2 mins)
        MonitorRule("SOXL", "PRICE", "TARGET", "", 0.0, 145.0, "ABOVE", 
                    "Alert: SOXL rose above target price {target:.2f}, current is {current:.2f}", True)
    ]

    print("\n--- TEST 1: VELOCITY (Rolling Window) ---")
    t_0 = now - 300
    record_tick(ticker, "IV", 80.0, strike=140.0, spot=139.76)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE history SET timestamp = ? WHERE ticker = 'SOXL' AND metric = 'IV'", (t_0,))
        conn.commit()
    print(f" -> Recorded baseline tick at t-300s: 80.0% IV")
    print(" -> Injected current shock tick: 85.5% IV (+5.5% delta)")
    app.evaluate_rules("SOXL", "IV", 85.5, current_spot=139.76, strike=140.0)

    print("\n--- TEST 2: TARGET PRICE MODE (Max 1 alert per 2 mins) ---")
    print(" -> Injected stock price: $146.50 (Target: $145.00 ABOVE)")
    app.evaluate_rules("SOXL", "PRICE", 146.50, current_spot=146.50)
    print(" -> Injected second price immediately: $147.00 (Suppressed by 2-min rate limit)")
    app.evaluate_rules("SOXL", "PRICE", 147.00, current_spot=147.00)
    print("[+] Dual-mode simulation completed.")


def run_test_rate_limit():
    """Specifically tests multiple rapid price ticks crossing a target to demonstrate the 2-minute rate limit."""
    print("=" * 70)
    print("RATE-LIMIT VERIFICATION TEST (Target Mode: Max 1 alert per 2 mins)")
    print("=" * 70)
    app = MultiAssetMonitor()
    target_price = 145.0
    app.rules = [
        MonitorRule("SOXL", "PRICE", "TARGET", "", 0.0, target_price, "ABOVE", 
                    "Alert: SOXL rose above target price {target:.2f}, current is {current:.2f}", True)
    ]

    prices = [145.50, 146.00, 146.25, 147.00, 148.00]
    print(f"[*] Simulating 5 rapid ticks crossing target ${target_price:.2f} within 5 seconds...\n")
    
    for i, px in enumerate(prices, 1):
        print(f"[Tick #{i}] Testing price ${px:.2f} (Target: ${target_price:.2f} ABOVE):")
        app.evaluate_rules("SOXL", "PRICE", px, current_spot=px)
        time.sleep(0.5)

    print("\n" + "=" * 70)
    print("RESULT: Notice that Tick #1 fired the alert, while Ticks #2-#5")
    print("were all blocked by the 120-second (2-minute) cooldown limiter!")
    print("=" * 70)


def run_test_status():
    """Validates monitor_config.csv, generates monitor_status.csv, and prints the result table."""
    init_db()
    print("=" * 85)
    print("CSV INSTANT VALIDATION & STATUS REPORT TEST")
    print(f"Config: {CONFIG_CSV_PATH}")
    print(f"Status: {STATUS_CSV_PATH}")
    print("=" * 85)

    app = MultiAssetMonitor()
    app.reload_config_file()
    
    if os.path.exists(STATUS_CSV_PATH):
        with open(STATUS_CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        print(f"\n[+] Generated {STATUS_CSV_PATH} ({len(rows)} rules):\n")
        header = f"{'TICKER':<8} {'METRIC':<7} {'MODE':<9} {'TARGET/WIN':<12} {'ENABLED':<8} {'SUBSCRIBED AT':<20} {'FAILED REASON / STATUS'}"
        print(header)
        print("-" * len(header))
        for r in rows:
            tgt_win = r.get('target_price') or r.get('time_window') or '-'
            st_reason = r.get('failed_reason', '')
            sub_t = r.get('last_successful_subscribe_time', '') or '-'
            print(f"{r.get('ticker'):<8} {r.get('metric'):<7} {r.get('mode'):<9} {tgt_win:<12} {r.get('enabled'):<8} {sub_t:<20} {st_reason}")
        print("=" * 85)
    else:
        print("[!] Error: monitor_status.csv was not generated!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Asset TWS Velocity Monitor")
    parser.add_argument("--test-alert", action="store_true", help="Test Mac audio, speech, and dialog")
    parser.add_argument("--test-sim", action="store_true", help="Simulate a database tick and alert evaluation")
    parser.add_argument("--test-rate-limit", action="store_true", help="Test rapid multiple ticks against 2-minute rate limit")
    parser.add_argument("--test-status", action="store_true", help="Validate config and test monitor_status.csv generation")
    args = parser.parse_args()

    if args.test_alert:
        run_test_alert()
        time.sleep(30)
    elif args.test_sim:
        run_test_sim()
        time.sleep(30)
    elif args.test_rate_limit:
        run_test_rate_limit()
        time.sleep(15)
    elif args.test_status:
        run_test_status()
    else:
        run_daemon()
