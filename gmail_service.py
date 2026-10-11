#!/usr/bin/env python3
"""
Gmail Service Module for IBKR Multi-Asset Monitor
- Uses Gmail OAuth 2.0 (gmail_token.json) to dispatch rich alert emails.
- Auto-refreshes OAuth credentials when expired.
- Generates mobile-responsive HTML alerts with 1-click Tailscale action links.
"""

import os
import sys
import json
import base64
import time
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "gmail_token.json")
DEFAULT_RECIPIENT = "eric.suo.jin@gmail.com"
TAILSCALE_BASE_URL = "http://100.69.167.42:5055"

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

_service_cache = None
_last_service_time = 0

def get_gmail_service():
    """Returns an authenticated Gmail API service client, refreshing tokens as needed."""
    global _service_cache, _last_service_time
    
    # Cache for up to 30 minutes unless expired
    if _service_cache and (time.time() - _last_service_time < 1800):
        return _service_cache

    if not os.path.exists(TOKEN_PATH):
        raise FileNotFoundError(f"OAuth token not found at {TOKEN_PATH}. Run setup_gmail_oauth.py first.")

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("OAuth credentials invalid and cannot be refreshed automatically.")

    _service_cache = build("gmail", "v1", credentials=creds, cache_discovery=False)
    _last_service_time = time.time()
    return _service_cache


def build_alert_email_html(data: dict) -> str:
    """Renders a modern, mobile-friendly HTML email with 1-click action buttons."""
    ticker = data.get("ticker", "UNKNOWN")
    metric = data.get("metric", "VALUE")
    mode = data.get("mode", "TARGET")
    condition = data.get("condition", "ABOVE")
    target_val = data.get("target_val")
    threshold_pct = data.get("threshold_pct")
    current_val = data.get("current_val", 0.0)
    spot = data.get("spot", 0.0)
    strike = data.get("strike")
    rule_num = data.get("rule_num", "?")
    rule_summary = data.get("rule_summary", "")
    time_str = data.get("time_str", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    confirm_url = data.get("confirm_url", "#")
    disable_url = data.get("disable_url", "#")
    attempt_num = data.get("attempt_num", 2)

    val_prefix = "" if metric == "IV" else "$"
    val_suffix = "%" if metric == "IV" else ""
    target_display = f"{val_prefix}{target_val:.2f}{val_suffix}" if target_val is not None else f"{threshold_pct:.1f}% shift"
    current_display = f"{val_prefix}{current_val:.2f}{val_suffix}"

    cond_color = "#ef4444" if condition == "BELOW" else "#10b981"
    cond_text = "Fell Below" if condition == "BELOW" else "Rose Above"

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>IBKR Alert: {ticker} {metric}</title>
</head>
<body style="margin: 0; padding: 16px; background-color: #0b0f17; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #f1f5f9;">
  <div style="max-width: 540px; margin: 0 auto; background: #131b2a; border: 1px solid #223049; border-radius: 14px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
    
    <!-- Top Header Banner -->
    <div style="background: linear-gradient(135deg, #1e293b, #0f172a); padding: 18px 24px; border-bottom: 1px solid #223049;">
      <div style="display: flex; align-items: center; justify-content: space-between;">
        <span style="display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); text-transform: uppercase; letter-spacing: 0.5px;">
          🚨 ALERT #{attempt_num} · ESCALATION
        </span>
        <span style="font-size: 12px; color: #94a3b8; font-family: monospace;">{time_str}</span>
      </div>
      <h1 style="margin: 12px 0 4px 0; font-size: 22px; font-weight: 800; color: #ffffff;">
        {ticker} <span style="color: #38bdf8;">{metric}</span> Triggered
      </h1>
      <div style="font-size: 13px; color: #94a3b8;">
        Rule #{rule_num}: <span style="color: #cbd5e1;">{rule_summary}</span>
      </div>
    </div>

    <!-- Main Value Display Card -->
    <div style="padding: 24px;">
      <div style="background: #0d131f; border: 1px solid #1e293b; border-radius: 10px; padding: 18px; margin-bottom: 20px;">
        <table style="width: 100%; border-collapse: collapse;">
          <tr>
            <td style="padding: 6px 0; font-size: 13px; color: #94a3b8;">Condition:</td>
            <td style="padding: 6px 0; font-size: 13px; font-weight: 700; color: {cond_color}; text-align: right;">{cond_text} Target</td>
          </tr>
          <tr>
            <td style="padding: 6px 0; font-size: 13px; color: #94a3b8;">Target Level:</td>
            <td style="padding: 6px 0; font-size: 14px; font-weight: 700; color: #f1f5f9; text-align: right; font-family: monospace;">{target_display}</td>
          </tr>
          <tr>
            <td style="padding: 8px 0; font-size: 14px; color: #cbd5e1; font-weight: 600;">Current {metric}:</td>
            <td style="padding: 8px 0; font-size: 20px; font-weight: 800; color: #38bdf8; text-align: right; font-family: monospace;">{current_display}</td>
          </tr>
          <tr style="border-top: 1px solid #1e293b;">
            <td style="padding: 8px 0 4px 0; font-size: 12px; color: #64748b;">Spot Price:</td>
            <td style="padding: 8px 0 4px 0; font-size: 12px; color: #cbd5e1; text-align: right; font-family: monospace;">${spot:.2f}</td>
          </tr>
          {"<tr><td style='padding: 2px 0; font-size: 12px; color: #64748b;'>ATM Strike:</td><td style='padding: 2px 0; font-size: 12px; color: #cbd5e1; text-align: right; font-family: monospace;'>$" + f"{strike:.1f}" + "</td></tr>" if strike else ""}
        </table>
      </div>

      <!-- Instruction / Notice -->
      <div style="font-size: 13px; color: #94a3b8; line-height: 1.5; margin-bottom: 20px;">
        This alert has fired on your Mac desktop twice. To prevent alert fatigue, desktop sound/voice alerts are now <b>silenced</b> until scheduled snooze reminders at <b>6:00 AM, 9:00 AM, and 5:00 PM</b> unless confirmed below.
      </div>

      <!-- Action Buttons -->
      <div style="text-align: center; margin: 24px 0 10px 0;">
        <table style="width: 100%; border-collapse: separate; border-spacing: 10px 0;">
          <tr>
            <td style="width: 50%; vertical-align: middle;">
              <a href="{confirm_url}" style="display: block; padding: 14px 12px; background: #2563eb; color: #ffffff; text-decoration: none; font-weight: 700; font-size: 14px; border-radius: 8px; text-align: center; box-shadow: 0 4px 12px rgba(37,99,235,0.3);">
                ✅ Confirm Delivery
              </a>
            </td>
            <td style="width: 50%; vertical-align: middle;">
              <a href="{disable_url}" style="display: block; padding: 13px 12px; background: rgba(239, 68, 68, 0.15); color: #f87171; text-decoration: none; font-weight: 700; font-size: 14px; border-radius: 8px; text-align: center; border: 1px solid #ef4444;">
                🛑 Disable Rule
              </a>
            </td>
          </tr>
        </table>
      </div>

      <!-- Action Descriptions -->
      <div style="margin-top: 14px; padding: 10px 12px; background: #0f1624; border-radius: 6px; font-size: 11px; color: #64748b; line-height: 1.4;">
        <div>• <b>Confirm Delivery:</b> Acknowledges alert receipt. Silences all further desktop snoozes for this event.</div>
        <div style="margin-top: 4px;">• <b>Disable Rule:</b> Immediately sets <code>enabled = false</code> in <code>monitor_config.csv</code> and stops monitoring this rule.</div>
      </div>

    </div>

    <!-- Footer -->
    <div style="padding: 14px 24px; background: #0d131f; border-top: 1px solid #1e293b; text-align: center; font-size: 11px; color: #475569;">
      Sent via Tailscale Node (100.69.167.42) · IBKR Autonomous Monitor
    </div>

  </div>
</body>
</html>
"""


def send_alert_email(data: dict, recipient: str = DEFAULT_RECIPIENT) -> bool:
    """Dispatches the alert email to the recipient via Gmail OAuth."""
    try:
        service = get_gmail_service()
        ticker = data.get("ticker", "TICKER")
        metric = data.get("metric", "ALERT")
        condition = data.get("condition", "ABOVE")
        target_val = data.get("target_val")
        val_prefix = "" if metric == "IV" else "$"
        val_suffix = "%" if metric == "IV" else ""
        target_display = f"{val_prefix}{target_val:.2f}{val_suffix}" if target_val is not None else "Threshold"
        
        subject = f"🚨 [IBKR ALERT] {ticker} {metric} {condition.title()} {target_display}"

        html_body = build_alert_email_html(data)
        text_body = (
            f"IBKR Alert: {ticker} {metric} {condition} {target_display}\n"
            f"Current {metric}: {data.get('current_val', 0.0):.2f}\n"
            f"Spot: ${data.get('spot', 0.0):.2f}\n\n"
            f"Confirm Delivery: {data.get('confirm_url')}\n"
            f"Disable Rule: {data.get('disable_url')}\n"
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = recipient
        msg["To"] = recipient

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        result = service.users().messages().send(userId="me", body={"raw": raw_msg}).execute()
        print(f"[+] [GMAIL DISPATCHED] Alert email sent to {recipient} (ID: {result.get('id')})", flush=True)
        return True
    except Exception as e:
        print(f"[!] [GMAIL ERROR] Failed to send alert email: {e}", flush=True)
        return False


if __name__ == "__main__":
    print("[*] Testing gmail_service directly...")
    sample_data = {
        "ticker": "SOXL",
        "metric": "IV",
        "mode": "TARGET",
        "condition": "ABOVE",
        "target_val": 100.0,
        "current_val": 104.2,
        "spot": 139.76,
        "strike": 140.0,
        "rule_num": 1,
        "rule_summary": "SOXL ATM IV Target Above 100.0%",
        "confirm_url": f"{TAILSCALE_BASE_URL}/api/alert/confirm?token=sample_test_token",
        "disable_url": f"{TAILSCALE_BASE_URL}/api/alert/disable?token=sample_test_token",
        "attempt_num": 2
    }
    success = send_alert_email(sample_data)
    print(f"Result: {'SUCCESS' if success else 'FAILED'}")
