#!/usr/bin/env python3
"""
Alert Router & Escalation Controller for IBKR Multi-Asset Monitor
- Manages intelligent alert cadence:
  * Wave 1: First desktop alert immediately upon condition match.
  * Wave 2: Second desktop alert 2 minutes later + concurrent Gmail escalation with 1-click action buttons.
  * Fallback Snooze: If unconfirmed, silences desktop alerts except at 6:00 AM, 9:00 AM, and 5:00 PM.
  * Action Links: 
    - Confirm Delivery -> Silences snoozes and confirms delivery.
    - Disable Rule -> Directly sets enabled=false in monitor_config.csv and halts monitoring.
"""

import os
import sys
import csv
import time
import datetime
import sqlite3
import secrets
import threading
from typing import Optional, Tuple, Dict, Any

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitor_history.db")
CONFIG_CSV_PATH = os.path.join(BASE_DIR, "monitor_config.csv")
TAILSCALE_BASE_URL = "http://100.69.167.42:5055"

# Cooldown between desktop alert 1 and desktop alert 2
ALERT_COOLDOWN_SECONDS = 120.0

# Scheduled fallback snooze hours (local time)
SNOOZE_HOURS = [6, 9, 17]

# Lock for thread safety during database operations
_db_lock = threading.Lock()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_alert_db():
    """Initializes the alert_incidents table in SQLite database."""
    with _db_lock:
        with get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id TEXT UNIQUE,
                    rule_id TEXT,
                    rule_num INTEGER,
                    ticker TEXT,
                    metric TEXT,
                    mode TEXT,
                    condition TEXT,
                    target_val REAL,
                    threshold_pct REAL,
                    current_val REAL,
                    spot REAL,
                    strike REAL,
                    trigger_count INTEGER DEFAULT 1,
                    first_triggered_at REAL,
                    last_alert_at REAL,
                    email_sent_at REAL DEFAULT 0,
                    confirmed INTEGER DEFAULT 0,
                    confirmed_at REAL DEFAULT 0,
                    disabled INTEGER DEFAULT 0,
                    disabled_at REAL DEFAULT 0,
                    confirmation_token TEXT UNIQUE,
                    last_snooze_slot TEXT DEFAULT '',
                    status TEXT DEFAULT 'ACTIVE',
                    last_seen_at REAL,
                    rule_summary TEXT DEFAULT ''
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_incident_rule ON alert_incidents(rule_id, status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_incident_token ON alert_incidents(confirmation_token);")
            conn.commit()


# Ensure DB schema is created on module load
init_alert_db()


class AlertRouter:
    """Intelligent alert escalation controller."""

    # In-memory tracking of rule hit states: rule_id -> {"is_hit": bool, "last_updated": float, "val": float, "spot": float}
    _rule_state: Dict[str, Dict[str, Any]] = {}

    # Active 2-minute evaluation timers: incident_id -> threading.Timer
    _active_timers: Dict[str, threading.Timer] = {}

    # Optional external metric value provider callback: (ticker, metric) -> float or None
    _value_provider: Optional[Any] = None

    @classmethod
    def set_value_provider(cls, provider_fn: Any):
        """Registers a callback for real-time metric lookup (e.g. from MultiAssetMonitor)."""
        cls._value_provider = provider_fn

    @classmethod
    def get_latest_metric_value(cls, ticker: str, metric: str) -> Optional[float]:
        """Fetches latest metric value from provider callback or SQLite history table."""
        if cls._value_provider:
            try:
                val = cls._value_provider(ticker, metric)
                if val is not None:
                    return float(val)
            except Exception:
                pass
        try:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT value FROM history WHERE ticker = ? AND metric = ? ORDER BY id DESC LIMIT 1",
                    (ticker, metric)
                ).fetchone()
                if row and row["value"] is not None:
                    return float(row["value"])
        except Exception:
            pass
        return None

    @classmethod
    def is_criteria_met(cls, rule: Any, current_val: Optional[float] = None) -> bool:
        """Evaluates whether rule condition is currently met."""
        rule_id = getattr(rule, "rule_id", f"{getattr(rule, 'ticker', '')}_{getattr(rule, 'metric', '')}")
        state = cls._rule_state.get(rule_id)
        now = time.time()

        # 1. Check in-memory rule state if updated recently (within 45s)
        if state and (now - state.get("last_updated", 0)) < 45.0:
            return bool(state.get("is_hit", False))

        # 2. Check current metric value if available
        ticker = getattr(rule, "ticker", "")
        metric = getattr(rule, "metric", "")
        if current_val is None:
            current_val = cls.get_latest_metric_value(ticker, metric)

        if current_val is None:
            return bool(state.get("is_hit", False)) if state else False

        mode = getattr(rule, "mode", "TARGET")
        condition = getattr(rule, "condition", "ABOVE")
        target_price = getattr(rule, "target_price", None)

        if mode == "TARGET" and target_price is not None:
            if condition == "ABOVE":
                return current_val >= target_price
            elif condition == "BELOW":
                return current_val <= target_price

        return bool(state.get("is_hit", False)) if state else False

    @classmethod
    def _evaluate_two_minute_mark(
        cls,
        incident_id: str,
        rule: Any,
        title: str,
        msg: str,
        voice_text: str,
        desktop_dispatch_fn: Any
    ):
        """
        Executed exactly 2 minutes (ALERT_COOLDOWN_SECONDS) after Alert #1.
        Checks if criteria is still met:
          - If YES: fires Desktop Alert #2.
          - If NO: lets it timeout and marks incident RESOLVED.
        """
        cls._active_timers.pop(incident_id, None)

        with _db_lock:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM alert_incidents WHERE incident_id = ?",
                    (incident_id,)
                ).fetchone()
                if not row:
                    return
                incident = dict(row)

        if incident.get("confirmed") == 1 or incident.get("disabled") == 1 or incident.get("status") != "ACTIVE":
            return

        if incident.get("trigger_count", 1) >= 2:
            return

        rule_id = getattr(rule, "rule_id", incident.get("rule_id"))
        ticker = incident.get("ticker", "")
        metric = incident.get("metric", "")

        is_met = cls.is_criteria_met(rule)
        now = time.time()

        if is_met:
            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute("""
                        UPDATE alert_incidents
                        SET trigger_count = 2, last_alert_at = ?, last_seen_at = ?
                        WHERE incident_id = ?
                    """, (now, now, incident_id))
                    conn.commit()

            print(f"\n[ALERT ROUTER] 🚨 Wave 2/2 Desktop Alert (Final at 2m mark) for {ticker} {metric}! Criteria still met. Incident: {incident_id}. Desktop alerts now silenced until 6am/9am/5pm.", flush=True)
            desktop_dispatch_fn(f"[2nd Alert] {title}", msg, f"Second alert: {voice_text}")
        else:
            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE alert_incidents SET status = 'RESOLVED' WHERE incident_id = ?",
                        (incident_id,)
                    )
                    conn.commit()

            print(f"\n[ALERT ROUTER] ⏱️ 2 minutes elapsed since Alert #1. Criteria is NO LONGER MET for {ticker} {metric} ({rule_id}). Incident timed out. Suppressing 2nd alert.", flush=True)

    @staticmethod
    def get_active_incident(rule_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves currently ACTIVE incident for this rule if one exists."""
        with _db_lock:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM alert_incidents WHERE rule_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
                    (rule_id,)
                ).fetchone()
                return dict(row) if row else None

    @staticmethod
    def get_incident_by_token(token: str) -> Optional[Dict[str, Any]]:
        """Retrieves incident by confirmation token."""
        with _db_lock:
            with get_db_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM alert_incidents WHERE confirmation_token = ? LIMIT 1",
                    (token,)
                ).fetchone()
                return dict(row) if row else None

    @classmethod
    def handle_rule_trigger(
        cls,
        rule: Any,
        current_val: float,
        spot: float,
        strike: Optional[float],
        title: str,
        msg: str,
        voice_text: str,
        desktop_dispatch_fn: Any
    ):
        """
        Main decision engine called whenever a rule's trigger condition is evaluated as TRUE.
        Enforces 2-wave desktop cadence + Gmail escalation on Wave 1 + 6am/9am/5pm fallback snooze.
        """
        now = time.time()
        dt_now = datetime.datetime.now()
        rule_id = getattr(rule, "rule_id", f"{rule.ticker}_{rule.metric}_{rule.mode}")
        rule_num = getattr(rule, "rule_num", None)
        rule_summary = getattr(rule, "get_rule_summary", lambda: str(rule_id))()

        # Update in-memory rule state
        cls._rule_state[rule_id] = {
            "is_hit": True,
            "last_updated": now,
            "val": current_val,
            "spot": spot
        }

        incident = cls.get_active_incident(rule_id)

        # -------------------------------------------------------------
        # CASE 1: No active incident -> Wave 1: First Desktop Alert + Immediate Gmail Alert
        # -------------------------------------------------------------
        if not incident:
            incident_id = f"inc_{int(now)}_{secrets.token_hex(4)}"
            token = secrets.token_urlsafe(16)
            
            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute("""
                        INSERT INTO alert_incidents (
                            incident_id, rule_id, rule_num, ticker, metric, mode, condition,
                            target_val, threshold_pct, current_val, spot, strike,
                            trigger_count, first_triggered_at, last_alert_at, email_sent_at,
                            confirmed, disabled, confirmation_token, last_snooze_slot,
                            status, last_seen_at, rule_summary
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 0, 0, ?, '', 'ACTIVE', ?, ?)
                    """, (
                        incident_id, rule_id, rule_num, rule.ticker, rule.metric, rule.mode,
                        getattr(rule, "condition", "ABOVE"), getattr(rule, "target_price", None),
                        getattr(rule, "threshold_pct", None), current_val, spot, strike,
                        now, now, now, token, now, rule_summary
                    ))
                    conn.commit()

            print(f"\n[ALERT ROUTER] 🚨 Wave 1/2 Desktop Alert + Gmail Alert dispatched for {rule.ticker} {rule.metric} (Val: {current_val:.2f}). Incident: {incident_id}", flush=True)
            desktop_dispatch_fn(title, msg, voice_text)

            # Schedule exact 2-minute mark evaluation timer
            def timer_task():
                cls._evaluate_two_minute_mark(
                    incident_id=incident_id,
                    rule=rule,
                    title=title,
                    msg=msg,
                    voice_text=voice_text,
                    desktop_dispatch_fn=desktop_dispatch_fn
                )
            t = threading.Timer(ALERT_COOLDOWN_SECONDS, timer_task)
            t.daemon = True
            cls._active_timers[incident_id] = t
            t.start()

            # Fire Gmail Alert immediately on first trigger asynchronously
            confirm_url = f"{TAILSCALE_BASE_URL}/api/alert/confirm?token={token}"
            disable_url = f"{TAILSCALE_BASE_URL}/api/alert/disable?token={token}"
            email_payload = {
                "ticker": rule.ticker,
                "metric": rule.metric,
                "mode": rule.mode,
                "condition": getattr(rule, "condition", "ABOVE"),
                "target_val": getattr(rule, "target_price", None),
                "threshold_pct": getattr(rule, "threshold_pct", None),
                "current_val": current_val,
                "spot": spot,
                "strike": strike,
                "rule_num": rule_num,
                "rule_summary": rule_summary,
                "time_str": dt_now.strftime("%Y-%m-%d %H:%M:%S"),
                "confirm_url": confirm_url,
                "disable_url": disable_url,
                "attempt_num": 1
            }
            def send_async():
                try:
                    import gmail_service
                    gmail_service.send_alert_email(email_payload)
                except Exception as mail_err:
                    print(f"[!] [ALERT ROUTER] Gmail alert dispatch failed: {mail_err}", flush=True)

            threading.Thread(target=send_async, daemon=True).start()
            return

        # -------------------------------------------------------------
        # Check if already confirmed or disabled
        # -------------------------------------------------------------
        if incident.get("confirmed") == 1 or incident.get("disabled") == 1:
            # User confirmed receipt or disabled rule; silence alerts
            return

        trigger_count = incident.get("trigger_count", 1)
        last_alert_at = incident.get("last_alert_at", 0)
        time_since_last = now - last_alert_at

        # -------------------------------------------------------------
        # CASE 2: Wave 2 (Second Desktop Alert 2 minutes later)
        # -------------------------------------------------------------
        if trigger_count == 1:
            if time_since_last < ALERT_COOLDOWN_SECONDS:
                # Still within 2-minute gap between Alert 1 and Alert 2
                return

            if time_since_last > (ALERT_COOLDOWN_SECONDS + 30.0):
                # More than 2.5 minutes elapsed without Wave 2 firing.
                # The 2-minute evaluation window has expired (timed out).
                with _db_lock:
                    with get_db_connection() as conn:
                        conn.execute(
                            "UPDATE alert_incidents SET status = 'RESOLVED' WHERE incident_id = ?",
                            (incident["incident_id"],)
                        )
                        conn.commit()
                t = cls._active_timers.pop(incident["incident_id"], None)
                if t:
                    try:
                        t.cancel()
                    except Exception:
                        pass
                print(f"[ALERT ROUTER] ⏱️ Tick arrived after 2-minute window ({time_since_last:.1f}s > 150s). Incident {incident['incident_id']} timed out. Suppressing 2nd alert.", flush=True)
                return

            # Within 2-minute evaluation window [120s, 150s] and criteria is met!
            # Cancel timer if still pending so it doesn't double-fire
            t = cls._active_timers.pop(incident["incident_id"], None)
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass

            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute("""
                        UPDATE alert_incidents
                        SET trigger_count = 2, last_alert_at = ?,
                            current_val = ?, spot = ?, strike = ?, last_seen_at = ?
                        WHERE incident_id = ?
                    """, (now, current_val, spot, strike, now, incident["incident_id"]))
                    conn.commit()

            print(f"\n[ALERT ROUTER] 🚨 Wave 2/2 Desktop Alert (Final) for {rule.ticker} {rule.metric}! Incident: {incident['incident_id']}. Desktop alerts now silenced until 6am/9am/5pm.", flush=True)
            
            # Fire Desktop Alert #2
            desktop_dispatch_fn(f"[2nd Alert] {title}", msg, f"Second alert: {voice_text}")
            return

        # -------------------------------------------------------------
        # CASE 3: Trigger count >= 2 (Desktop alerts silenced until 6am/9am/5pm)
        # -------------------------------------------------------------
        current_hour = dt_now.hour
        if current_hour in SNOOZE_HOURS:
            slot_key = f"{dt_now.strftime('%Y-%m-%d')} {current_hour:02d}"
            last_slot = incident.get("last_snooze_slot", "")

            if last_slot != slot_key:
                # Fire scheduled snooze reminder
                with _db_lock:
                    with get_db_connection() as conn:
                        conn.execute("""
                            UPDATE alert_incidents
                            SET trigger_count = trigger_count + 1, last_alert_at = ?,
                                last_snooze_slot = ?, current_val = ?, spot = ?, last_seen_at = ?
                            WHERE incident_id = ?
                        """, (now, slot_key, current_val, spot, now, incident["incident_id"]))
                        conn.commit()

                snooze_title = f"⏰ [Scheduled Reminder] {title}"
                snooze_voice = f"Reminder: {voice_text}"
                print(f"[ALERT ROUTER] ⏰ Dispatched scheduled snooze reminder ({slot_key}) for {rule.ticker} {rule.metric}", flush=True)
                desktop_dispatch_fn(snooze_title, msg, snooze_voice)
                return

        # Otherwise, suppressed according to policy
        with _db_lock:
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE alert_incidents SET last_seen_at = ? WHERE incident_id = ?",
                    (now, incident["incident_id"])
                )
                conn.commit()

    @classmethod
    def handle_rule_cleared(cls, rule_id: str):
        """Called when a rule's condition is NOT met."""
        now = time.time()
        cls._rule_state[rule_id] = {"is_hit": False, "last_updated": now}

        incident = cls.get_active_incident(rule_id)
        if not incident:
            return

        trigger_count = incident.get("trigger_count", 1)
        first_triggered_at = incident.get("first_triggered_at", now)

        # CASE A: If trigger_count == 1 and 2 minutes have elapsed since first alert:
        # Criteria is NOT met at or after the 2-minute mark -> timeout immediately!
        if trigger_count == 1 and (now - first_triggered_at) >= ALERT_COOLDOWN_SECONDS:
            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE alert_incidents SET status = 'RESOLVED' WHERE incident_id = ?",
                        (incident["incident_id"],)
                    )
                    conn.commit()
            t = cls._active_timers.pop(incident["incident_id"], None)
            if t:
                try:
                    t.cancel()
                except Exception:
                    pass
            print(f"[ALERT ROUTER] ⏱️ Rule {rule_id} condition cleared at/after 2m mark. Incident {incident['incident_id']} timed out.", flush=True)
            return

        # CASE B: For trigger_count >= 2, if cleared for > 300s, resolve the incident
        last_seen = incident.get("last_seen_at", first_triggered_at)
        if (now - last_seen) > 300.0:
            with _db_lock:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE alert_incidents SET status = 'RESOLVED' WHERE incident_id = ?",
                        (incident["incident_id"],)
                    )
                    conn.commit()
            print(f"[ALERT ROUTER] Incident {incident['incident_id']} for {rule_id} marked RESOLVED (condition cleared for 5m).", flush=True)

    @classmethod
    def confirm_delivery(cls, token: str) -> Dict[str, Any]:
        """User clicked 'Confirm Delivery' link from email."""
        incident = cls.get_incident_by_token(token)
        if not incident:
            return {"success": False, "message": "Invalid or expired confirmation token."}

        # Cancel active 2-minute timer if running
        t = cls._active_timers.pop(incident["incident_id"], None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

        now = time.time()
        with _db_lock:
            with get_db_connection() as conn:
                conn.execute("""
                    UPDATE alert_incidents
                    SET confirmed = 1, confirmed_at = ?, status = 'CONFIRMED'
                    WHERE confirmation_token = ?
                """, (now, token))
                conn.commit()

        print(f"[ALERT ROUTER] ✅ Alert delivery confirmed by user for incident {incident['incident_id']} ({incident['ticker']} {incident['metric']})!", flush=True)
        return {
            "success": True,
            "message": "Delivery successfully confirmed. Desktop alerts and scheduled snoozes have been stopped.",
            "incident": incident
        }

    @classmethod
    def disable_rule(cls, token: str) -> Dict[str, Any]:
        """User clicked 'Disable Rule' link from email. Directly sets enabled=false in monitor_config.csv."""
        incident = cls.get_incident_by_token(token)
        if not incident:
            return {"success": False, "message": "Invalid or expired token."}

        # Cancel active 2-minute timer if running
        t = cls._active_timers.pop(incident["incident_id"], None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass

        now = time.time()
        rule_num = incident.get("rule_num")
        ticker = incident.get("ticker", "").upper()
        metric = incident.get("metric", "").upper()

        # Update monitor_config.csv atomically
        if not os.path.exists(CONFIG_CSV_PATH):
            return {"success": False, "message": f"Config file not found at {CONFIG_CSV_PATH}"}

        disabled_row_count = 0
        try:
            with open(CONFIG_CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                rows = list(reader)

            for idx, r in enumerate(rows):
                cur_num = idx + 1
                row_ticker = (r.get("ticker") or "").strip().upper()
                row_metric = (r.get("metric") or "").strip().upper()

                match = False
                if rule_num and cur_num == rule_num:
                    match = True
                elif row_ticker == ticker and row_metric == metric:
                    match = True

                if match:
                    r["enabled"] = "false"
                    disabled_row_count += 1

            if disabled_row_count > 0:
                tmp_path = CONFIG_CSV_PATH + ".tmp"
                with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)
                os.replace(tmp_path, CONFIG_CSV_PATH)
                print(f"[ALERT ROUTER] 🛑 Disabled Rule #{rule_num or '?'} ({ticker} {metric}) in monitor_config.csv.", flush=True)
            else:
                print(f"[ALERT ROUTER] [!] Warning: Could not find matching rule row to disable for {ticker} {metric}.", flush=True)

        except Exception as e:
            return {"success": False, "message": f"Failed to update config CSV: {e}"}

        # Mark incident disabled & confirmed in database
        with _db_lock:
            with get_db_connection() as conn:
                conn.execute("""
                    UPDATE alert_incidents
                    SET disabled = 1, disabled_at = ?, confirmed = 1, confirmed_at = ?, status = 'DISABLED'
                    WHERE confirmation_token = ?
                """, (now, now, token))
                conn.commit()

        return {
            "success": True,
            "message": f"Rule for {ticker} {metric} was disabled in monitor_config.csv. Daemon will automatically unsubscribe.",
            "incident": incident
        }


if __name__ == "__main__":
    print("[*] AlertRouter self-test...")
    init_alert_db()
    print("[+] AlertRouter DB initialized successfully.")
