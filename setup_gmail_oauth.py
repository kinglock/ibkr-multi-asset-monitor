#!/usr/bin/env python3
"""
Gmail OAuth 2.0 Setup & Verification Tool
- Generates gmail_token.json using credentials.json
- Verifies token by sending a test email to eric.suo.jin@gmail.com
"""

import os
import sys
import json
import base64
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(BASE_DIR, "credentials.json")
TOKEN_PATH = os.path.join(BASE_DIR, "gmail_token.json")
RECIPIENT_EMAIL = "eric.suo.jin@gmail.com"

# Minimal scope: send emails only
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send"
]

def authenticate_gmail():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"[!] Error: {CREDENTIALS_PATH} not found!")
        sys.exit(1)

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[*] Refreshing expired OAuth token...")
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"[!] Refresh failed: {e}. Starting fresh auth flow...")
                creds = None

        if not creds:
            print("[*] Launching Google OAuth authorization in your default browser...")
            print("[*] Please sign in as eric.suo.jin@gmail.com and approve access.")
            
            # Check credentials format
            with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
                raw_cfg = json.load(f)
            
            # If created as 'web', try with port 8080 or port 5055
            port = 8080
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            
            try:
                creds = flow.run_local_server(port=port, prompt="consent", access_type="offline")
            except Exception as e:
                print(f"[!] Authorization error: {e}")
                print("\n[TIP] If you see 'redirect_uri_mismatch', please either:")
                print("1. In Google Cloud Console, under your OAuth Client ID, add 'http://localhost:8080/' to 'Authorized redirect URIs'.")
                print("2. Or create an OAuth Client ID with Application type set to 'Desktop app'.")
                sys.exit(1)

        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        print(f"[+] Saved valid OAuth token to: {TOKEN_PATH}")

    return creds

def send_test_email(creds):
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "✅ [TEST] IBKR Alert Router: Gmail OAuth Plumbing Verified"
    msg["From"] = RECIPIENT_EMAIL
    msg["To"] = RECIPIENT_EMAIL

    text_body = (
        "IBKR Alert Notification Test\n"
        "Your Mac (ds-macbook-air) successfully authenticated with Gmail OAuth 2.0!\n"
        "Tailscale Node IP: 100.69.167.42\n"
        "Local LAN IP: 192.168.68.52\n"
    )

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0b0f17; color: #f1f5f9; padding: 20px; }}
        .card {{ background: #131b2a; border: 1px solid #223049; border-radius: 12px; max-width: 520px; margin: 0 auto; padding: 24px; box-shadow: 0 4px 16px rgba(0,0,0,0.4); }}
        .badge {{ display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 700; background: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid #10b981; }}
        .btn {{ display: inline-block; padding: 10px 18px; border-radius: 8px; font-weight: 700; font-size: 13px; text-decoration: none; margin-right: 8px; margin-top: 12px; }}
        .btn-confirm {{ background: #2563eb; color: #ffffff !important; }}
        .btn-disable {{ background: rgba(239, 68, 68, 0.2); color: #ef4444 !important; border: 1px solid #ef4444; }}
      </style>
    </head>
    <body>
      <div class="card">
        <span class="badge">OAUTH VERIFIED</span>
        <h2 style="margin-top: 12px; margin-bottom: 8px; color: #f1f5f9;">IBKR Alert Escalation System</h2>
        <p style="color: #94a3b8; font-size: 13px; line-height: 1.5;">
          This is a test notification confirming that your Mac's monitor daemon can deliver rich alert emails via Gmail OAuth 2.0.
        </p>
        <div style="background: #0f1624; border: 1px solid #223049; border-radius: 8px; padding: 12px; margin: 16px 0; font-family: monospace; font-size: 12px; color: #cbd5e1;">
          <div>• <b>Mac Tailscale IP:</b> 100.69.167.42</div>
          <div>• <b>Port:</b> 5055</div>
          <div>• <b>Status:</b> Connected & Authenticated</div>
        </div>
        <p style="color: #94a3b8; font-size: 12px;">Test Action Buttons:</p>
        <div>
          <a href="http://100.69.167.42:5055/api/alert/confirm?token=test_token" class="btn btn-confirm">✅ Confirm Delivery (Test)</a>
          <a href="http://100.69.167.42:5055/api/alert/disable?token=test_token" class="btn btn-disable">🛑 Disable Rule (Test)</a>
        </div>
      </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    send_res = service.users().messages().send(userId="me", body={"raw": raw_msg}).execute()
    print(f"[+] Success! Test alert email sent to {RECIPIENT_EMAIL} (Message ID: {send_res.get('id')})")

if __name__ == "__main__":
    print("==================================================")
    print("Gmail OAuth 2.0 Setup & Verification Tool")
    print(f"Credentials: {CREDENTIALS_PATH}")
    print(f"Target Email: {RECIPIENT_EMAIL}")
    print("==================================================")
    creds = authenticate_gmail()
    print("\n[*] Sending test email...")
    send_test_email(creds)
    print("\n[+] Gmail OAuth plumbing complete and ready!")
