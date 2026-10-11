#!/usr/bin/env python3
"""
Lightweight Web Server for IBKR Monitor Dashboard
- Serves interactive single-page UI on http://127.0.0.1:5055
- Provides REST APIs to read/write monitor_config.csv and monitor_status.csv
- Checks and reports live daemon health, TWS port reachability, and SQLite DB telemetry
"""

import os
import sys
import csv
import json
import time
import socket
import sqlite3
import datetime
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from alert_router import AlertRouter, get_db_connection

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_CSV_PATH = os.path.join(BASE_DIR, "monitor_config.csv")
STATUS_CSV_PATH = os.path.join(BASE_DIR, "monitor_status.csv")
DB_PATH = os.path.join(BASE_DIR, "monitor_history.db")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")

PORT = 5055
TWS_HOST = "127.0.0.1"
TWS_PORT = 7496

CONFIG_FIELDNAMES = [
    "ticker", "metric", "mode", "time_window", "threshold_pct",
    "target_price", "condition", "voice_alert_text", "enabled"
]

def check_tws_connectivity(timeout=0.3):
    try:
        with socket.create_connection((TWS_HOST, TWS_PORT), timeout=timeout):
            return True
    except Exception:
        return False

def get_daemon_info():
    try:
        res = subprocess.run(
            ["pgrep", "-fl", "multi_asset_monitor.py"],
            capture_output=True, text=True, timeout=1.0
        )
        lines = res.stdout.strip().splitlines()
        for line in lines:
            parts = line.split(None, 1)
            if parts and parts[0].isdigit():
                pid = int(parts[0])
                if pid != os.getpid():
                    # Get CPU and memory info
                    ps_res = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "%cpu,%mem,etime"],
                        capture_output=True, text=True, timeout=1.0
                    )
                    ps_lines = ps_res.stdout.strip().splitlines()
                    stats = ps_lines[1].split() if len(ps_lines) > 1 else ["0.0", "0.0", "00:00"]
                    return {
                        "running": True,
                        "pid": pid,
                        "cpu": stats[0] if len(stats) > 0 else "0.0",
                        "mem": stats[1] if len(stats) > 1 else "0.0",
                        "uptime": stats[2] if len(stats) > 2 else "-"
                    }
        return {"running": False, "pid": None, "cpu": "0.0", "mem": "0.0", "uptime": "-"}
    except Exception as e:
        return {"running": False, "pid": None, "error": str(e)}

def get_db_stats():
    if not os.path.exists(DB_PATH):
        return {"exists": False, "ticks": 0, "latest_tick": None, "size_kb": 0}
    try:
        size_kb = round(os.path.getsize(DB_PATH) / 1024, 1)
        with sqlite3.connect(DB_PATH, timeout=1.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*), MAX(timestamp) FROM history")
            row = cursor.fetchone()
            count = row[0] if row else 0
            max_ts = row[1] if row and row[1] else None
            max_ts_str = datetime.datetime.fromtimestamp(max_ts).strftime("%Y-%m-%d %H:%M:%S") if max_ts else "-"
            return {
                "exists": True,
                "ticks": count,
                "latest_tick": max_ts_str,
                "size_kb": size_kb
            }
    except Exception as e:
        return {"exists": True, "ticks": 0, "latest_tick": None, "size_kb": 0, "error": str(e)}

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

def get_latest_market_quotes():
    """Returns latest spot price and ATM IV for monitored tickers from monitor_history.db."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        with sqlite3.connect(DB_PATH, timeout=1.0) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT ticker FROM history")
            tickers = [r[0] for r in cursor.fetchall() if r[0]]
            quotes = {}
            for t in tickers:
                cursor.execute("""
                    SELECT value, timestamp FROM history 
                    WHERE ticker = ? AND metric = 'PRICE' 
                    ORDER BY timestamp DESC LIMIT 1
                """, (t,))
                p_row = cursor.fetchone()

                cursor.execute("""
                    SELECT value, strike, timestamp FROM history 
                    WHERE ticker = ? AND metric = 'IV' AND strike IS NOT NULL 
                    ORDER BY timestamp DESC LIMIT 1
                """, (t,))
                iv_row = cursor.fetchone()

                quotes[t] = {
                    "spot": round(float(p_row[0]), 2) if p_row and p_row[0] is not None else None,
                    "spot_ts": p_row[1] if p_row else None,
                    "iv": round(float(iv_row[0]), 2) if iv_row and iv_row[0] is not None else None,
                    "strike": iv_row[1] if iv_row else None,
                    "iv_ts": iv_row[2] if iv_row else None,
                }
            return quotes
    except Exception as e:
        return {}

def validate_row_dict(row):
    """Row validator mirroring multi_asset_monitor.py"""
    ticker = (row.get("ticker") or "").strip().upper()
    if not ticker:
        return False, "Missing ticker symbol"
    if not ticker.replace(".", "").isalnum():
        return False, f"Invalid ticker symbol '{ticker}'"

    metric = (row.get("metric") or "").strip().upper()
    if metric not in ["PRICE", "IV", "OPT_PRICE"]:
        return False, f"Invalid metric '{metric}' (allowed: PRICE, IV, OPT_PRICE)"

    mode = (row.get("mode") or "").strip().upper()
    target_raw = str(row.get("target_price") or "").strip()
    window_raw = str(row.get("time_window") or "").strip().lower()
    threshold_raw = str(row.get("threshold_pct") or "").strip()

    if not mode:
        if target_raw:
            mode = "TARGET"
        elif window_raw or threshold_raw:
            mode = "VELOCITY"
        else:
            return False, "Mode not specified (choose TARGET or VELOCITY)"

    if mode not in ["TARGET", "VELOCITY", "WINDOW"]:
        return False, f"Invalid mode '{mode}' (allowed: TARGET, VELOCITY)"

    if mode == "TARGET":
        metric = (row.get("metric") or "PRICE").strip().upper()
        label = "Target IV Level (%)" if metric == "IV" else "Target Price"
        if not target_raw:
            return False, f"TARGET mode requires a numeric {label} > 0"
        try:
            val = float(target_raw)
            if val <= 0:
                return False, f"{label} must be greater than 0"
        except ValueError:
            return False, f"{label} '{target_raw}' is not a valid number"

        cond = (row.get("condition") or "ABOVE").strip().upper()
        if cond not in ["ABOVE", "BELOW"]:
            return False, f"Condition '{cond}' must be ABOVE or BELOW"

    elif mode in ["VELOCITY", "WINDOW"]:
        if not window_raw:
            return False, "VELOCITY mode requires a Time Window (e.g. 5m, 30m, 1h, 1d)"
        # Check window validity
        valid_units = ("m", "h", "d")
        if not any(window_raw.endswith(u) for u in valid_units):
            return False, f"Time Window '{window_raw}' must end in m, h, or d (e.g. 5m, 1h)"
        try:
            num = float(window_raw[:-1])
            if num <= 0:
                return False, "Time Window duration must be > 0"
        except ValueError:
            return False, f"Time Window '{window_raw}' is not a valid number"

        if not threshold_raw:
            return False, "VELOCITY mode requires a Threshold % > 0"
        try:
            thresh = float(threshold_raw)
            if thresh <= 0:
                return False, "Threshold % must be greater than 0"
        except ValueError:
            return False, f"Threshold % '{threshold_raw}' is not a valid number"

        start_time_raw = str(row.get("window_start_time") or "").strip()
        if start_time_raw:
            parsed_ts = parse_datetime_str(start_time_raw)
            if parsed_ts is None:
                return False, f"Invalid Start Time '{start_time_raw}' (use YYYY-MM-DD HH:MM)"
            now = time.time()
            if parsed_ts < (now - 10 * 86400):
                return False, "Start Time cannot be more than 10 days in the past"
            if parsed_ts > (now + 1 * 86400):
                return False, "Start Time cannot be more than 1 day in the future"

    enabled_raw = str(row.get("enabled", "true")).strip().lower()
    if enabled_raw not in ["true", "false", "1", "0", "yes", "no"]:
        return False, f"'enabled' must be true or false (found: '{row.get('enabled')}')"

    return True, "Valid"


def render_action_result_page(title: str, badge: str, badge_color: str, is_success: bool, message: str, incident: dict = None) -> str:
    inc = incident or {}
    ticker = inc.get("ticker", "")
    metric = inc.get("metric", "")
    time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    details_rows = ""
    if ticker:
        details_rows += f"<tr><td style='color:#94a3b8;padding:6px 0;'>Ticker & Metric:</td><td style='color:#f1f5f9;font-weight:700;text-align:right;'>{ticker} {metric}</td></tr>"
    if inc.get("rule_num"):
        details_rows += f"<tr><td style='color:#94a3b8;padding:6px 0;'>Rule Number:</td><td style='color:#f1f5f9;text-align:right;'>#{inc.get('rule_num')}</td></tr>"
    if inc.get("rule_summary"):
        details_rows += f"<tr><td style='color:#94a3b8;padding:6px 0;'>Rule Summary:</td><td style='color:#cbd5e1;text-align:right;'>{inc.get('rule_summary')}</td></tr>"
    if inc.get("current_val"):
        details_rows += f"<tr><td style='color:#94a3b8;padding:6px 0;'>Trigger Value:</td><td style='color:#38bdf8;font-family:monospace;font-weight:700;text-align:right;'>{inc.get('current_val'):.2f}</td></tr>"
    details_rows += f"<tr><td style='color:#94a3b8;padding:6px 0;'>Acknowledged At:</td><td style='color:#94a3b8;font-family:monospace;text-align:right;'>{time_str}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} · IBKR Monitor</title>
  <style>
    body {{
      margin: 0; padding: 20px; background-color: #0b0f17; color: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex; justify-content: center; align-items: center; min-height: 90vh;
    }}
    .card {{
      background: #131b2a; border: 1px solid #223049; border-radius: 16px;
      max-width: 480px; width: 100%; padding: 28px; box-shadow: 0 12px 40px rgba(0,0,0,0.6);
      box-sizing: border-box; text-align: center;
    }}
    .badge {{
      display: inline-block; padding: 5px 12px; border-radius: 9999px; font-size: 11px;
      font-weight: 800; letter-spacing: 0.5px; text-transform: uppercase;
      background: {badge_color}22; color: {badge_color}; border: 1px solid {badge_color};
      margin-bottom: 16px;
    }}
    h1 {{ margin: 0 0 10px 0; font-size: 22px; font-weight: 800; color: #ffffff; }}
    p.desc {{ color: #94a3b8; font-size: 14px; line-height: 1.5; margin: 0 0 20px 0; }}
    .details-box {{
      background: #0d131f; border: 1px solid #1e293b; border-radius: 10px;
      padding: 16px; margin-bottom: 24px; text-align: left; font-size: 13px;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    .btn {{
      display: inline-block; width: 100%; box-sizing: border-box; padding: 12px;
      border-radius: 8px; font-weight: 700; font-size: 14px; text-decoration: none;
      background: #2563eb; color: #ffffff; transition: background 0.2s;
    }}
    .btn:hover {{ background: #1d4ed8; }}
    .footer-note {{ margin-top: 16px; font-size: 11px; color: #64748b; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="badge">{badge}</div>
    <h1>{title}</h1>
    <p class="desc">{message}</p>
    {f'<div class="details-box"><table>{details_rows}</table></div>' if details_rows else ''}
    <a href="/" class="btn">Open Monitor Dashboard</a>
    <div class="footer-note">Tailscale Node (100.69.167.42) · IBKR Autonomous Monitor</div>
  </div>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence routine 200 GET access logging to keep terminal tidy
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content, status=200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html"]:
            if os.path.exists(INDEX_HTML_PATH):
                with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
                    self._send_html(f.read())
            else:
                self._send_html("<h1>index.html not found</h1>", status=404)

        elif path == "/api/config":
            rows = []
            mtime = 0
            mtime_str = "-"
            if os.path.exists(CONFIG_CSV_PATH):
                mtime = os.path.getmtime(CONFIG_CSV_PATH)
                mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    with open(CONFIG_CSV_PATH, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for r in reader:
                            if any(v.strip() for v in r.values() if v):
                                # Clean and validate row
                                is_valid, err_msg = validate_row_dict(r)
                                r_clean = {k: r.get(k, "") for k in CONFIG_FIELDNAMES}
                                r_clean["_is_valid"] = is_valid
                                r_clean["_validation_msg"] = err_msg
                                rows.append(r_clean)
                except Exception as e:
                    self._send_json({"success": False, "error": str(e)}, status=500)
                    return
            quotes = get_latest_market_quotes()
            self._send_json({
                "success": True,
                "rows": rows,
                "market_quotes": quotes,
                "mtime": mtime,
                "mtime_str": mtime_str
            })

        elif path == "/api/status":
            rows = []
            mtime = 0
            mtime_str = "-"
            if os.path.exists(STATUS_CSV_PATH):
                mtime = os.path.getmtime(STATUS_CSV_PATH)
                mtime_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    with open(STATUS_CSV_PATH, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for r in reader:
                            if any(v.strip() for v in r.values() if v):
                                rows.append(r)
                except Exception as e:
                    self._send_json({"success": False, "error": str(e)}, status=500)
                    return
            quotes = get_latest_market_quotes()
            self._send_json({
                "success": True,
                "rows": rows,
                "market_quotes": quotes,
                "mtime": mtime,
                "mtime_str": mtime_str
            })

        elif path == "/api/health":
            daemon = get_daemon_info()
            tws_online = check_tws_connectivity()
            db = get_db_stats()
            cfg_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(CONFIG_CSV_PATH)).strftime("%Y-%m-%d %H:%M:%S") if os.path.exists(CONFIG_CSV_PATH) else "-"
            st_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(STATUS_CSV_PATH)).strftime("%Y-%m-%d %H:%M:%S") if os.path.exists(STATUS_CSV_PATH) else "-"

            self._send_json({
                "success": True,
                "daemon": daemon,
                "tws": {
                    "connected": tws_online,
                    "host": TWS_HOST,
                    "port": TWS_PORT
                },
                "db": db,
                "config_mtime": cfg_mtime,
                "status_mtime": st_mtime,
                "server_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        elif path == "/api/alert/confirm":
            query = parse_qs(parsed.query)
            token = query.get("token", [""])[0]
            result = AlertRouter.confirm_delivery(token)
            html = render_action_result_page(
                title="Delivery Confirmed",
                badge="CONFIRMED",
                badge_color="#10b981",
                is_success=result.get("success", False),
                message=result.get("message", ""),
                incident=result.get("incident")
            )
            self._send_html(html, status=200 if result.get("success") else 400)

        elif path == "/api/alert/disable":
            query = parse_qs(parsed.query)
            token = query.get("token", [""])[0]
            result = AlertRouter.disable_rule(token)
            html = render_action_result_page(
                title="Rule Disabled",
                badge="RULE DISABLED",
                badge_color="#ef4444",
                is_success=result.get("success", False),
                message=result.get("message", ""),
                incident=result.get("incident")
            )
            self._send_html(html, status=200 if result.get("success") else 400)

        elif path == "/api/alerts":
            try:
                with get_db_connection() as conn:
                    rows = conn.execute(
                        "SELECT * FROM alert_incidents ORDER BY id DESC LIMIT 50"
                    ).fetchall()
                    self._send_json({
                        "success": True,
                        "alerts": [dict(r) for r in rows]
                    })
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, status=500)

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                rows = payload.get("rows", [])

                # Validate each row
                validated_rows = []
                errors = []
                for idx, r in enumerate(rows):
                    is_valid, msg = validate_row_dict(r)
                    if not is_valid:
                        errors.append(f"Row {idx+1} ({r.get('ticker','?')}): {msg}")
                    # Normalize fields
                    clean_row = {
                        "ticker": (r.get("ticker") or "").strip().upper(),
                        "metric": (r.get("metric") or "PRICE").strip().upper(),
                        "mode": (r.get("mode") or "TARGET").strip().upper(),
                        "time_window": (r.get("time_window") or "").strip().lower() if r.get("mode") in ["VELOCITY", "WINDOW"] else "",
                        "threshold_pct": str(r.get("threshold_pct") or "").strip() if r.get("mode") in ["VELOCITY", "WINDOW"] else "",
                        "target_price": str(r.get("target_price") or "").strip() if r.get("mode") == "TARGET" else "",
                        "condition": (r.get("condition") or "ABOVE").strip().upper() if r.get("mode") == "TARGET" else "",
                        "voice_alert_text": (r.get("voice_alert_text") or "").strip(),
                        "enabled": "true" if str(r.get("enabled", "true")).strip().lower() in ["true", "1", "yes"] else "false"
                    }
                    validated_rows.append(clean_row)

                if errors:
                    self._send_json({
                        "success": False,
                        "message": "Validation failed on some rows",
                        "errors": errors
                    }, status=400)
                    return

                # Write to monitor_config.csv atomically
                tmp_path = CONFIG_CSV_PATH + ".tmp"
                with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=CONFIG_FIELDNAMES)
                    writer.writeheader()
                    writer.writerows(validated_rows)
                os.replace(tmp_path, CONFIG_CSV_PATH)

                self._send_json({
                    "success": True,
                    "message": f"Successfully saved {len(validated_rows)} monitoring rules to config file. Daemon will auto-reload within 500ms.",
                    "count": len(validated_rows)
                })
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, status=500)

        elif path == "/api/daemon/restart":
            try:
                plist = os.path.expanduser("~/Library/LaunchAgents/com.ibkr.multi_asset_monitor.plist")
                cmd = f"launchctl unload '{plist}' && launchctl load '{plist}'"
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5.0)
                time.sleep(1.0)
                daemon_info = get_daemon_info()
                self._send_json({
                    "success": True,
                    "message": "Daemon restarted successfully via launchctl",
                    "daemon": daemon_info
                })
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, status=500)

        elif path == "/api/test-alert":
            try:
                def run_alert():
                    subprocess.run([
                        sys.executable,
                        os.path.join(BASE_DIR, "multi_asset_monitor.py"),
                        "--test-alert"
                    ])
                threading.Thread(target=run_alert, daemon=True).start()
                self._send_json({"success": True, "message": "Test audio & speech alert dispatched!"})
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, status=500)

        else:
            self.send_error(404, "Not Found")

def run_server():
    server = HTTPServer(("0.0.0.0", PORT), MonitorHandler)
    print("=" * 70, flush=True)
    print(f"IBKR Multi-Asset Monitor Web UI Server running at:", flush=True)
    print(f"  ➜ Local:     http://localhost:{PORT}", flush=True)
    print(f"  ➜ Tailscale: http://100.69.167.42:{PORT}", flush=True)
    print("=" * 70, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping server...", flush=True)
        server.server_close()

if __name__ == "__main__":
    run_server()
