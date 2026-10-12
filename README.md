# IBKR Multi-Asset Velocity & Volatility Monitor

An autonomous, production-grade monitoring and alert escalation platform for Interactive Brokers (TWS / IB Gateway).

Subscribes to live stock prices and TWS Model ATM Implied Volatility (`tickType 13`), evaluates customizable multi-asset velocity and target rules, serves an interactive web dashboard, and features a decoupled 2-wave alert escalation system with mobile 1-click confirmation over Tailscale.

---

## Architecture Overview

```
                               ┌──────────────────────────────────────────────┐
                               │       Interactive Brokers TWS / Gateway      │
                               │               (Port 7496 / 7497)             │
                               └──────────────────────┬───────────────────────┘
                                                      │ Socket Ticks
                                                      ▼
┌──────────────────────────────┐       ┌──────────────────────────────────────────────┐
│     monitor_config.csv       │◄─────►│    Multi-Asset Monitor Daemon Engine         │
│  (Dynamic Config / Hot-Reload│       │         (multi_asset_monitor.py)             │
└──────────────────────────────┘       └──────────────────────┬───────────────────────┘
                                                              │ Rule Triggers / Reset
                                                              ▼
┌──────────────────────────────┐       ┌──────────────────────────────────────────────┐
│      Web UI Dashboard        │       │         Alert Router & State Engine          │
│   (monitor_ui_server.py)     │◄─────►│               (alert_router.py)              │
│  - http://localhost:5055     │       │   - SQLite table: alert_incidents            │
│  - Tailscale: 100.x.x.x:5055 │       │   - 2-Wave Cadence (2-min gap)               │
│  - Mobile 1-click Webhooks   │       │   - Fallback Snooze: 6am / 9am / 5pm         │
└──────────────────────────────┘       └──────────────────────┬───────────────────────┘
                                                              │ Wave 2 Escalation
                                                              ▼
                                       ┌──────────────────────────────────────────────┐
                                       │         Gmail Escalation Service             │
                                       │               (gmail_service.py)             │
                                       │   - Mobile HTML Card with Action Buttons     │
                                       │   - Gmail OAuth 2.0 with Auto-Token Refresh  │
                                       └──────────────────────────────────────────────┘
```

---

## Core Features

1. **Authoritative IBKR Implied Volatility (IV):**
   - Direct invocation of Interactive Brokers' internal pricing model (`calculateImpliedVolatility` via Socket API) for live ATM options and Greeks.
   - Eliminates generic Black-Scholes approximations.

2. **Dual-Mode Alert Evaluation:**
   - **Target Mode:** Alerts when Price or ATM IV crosses absolute target thresholds (e.g. `SOXL Price > $140.00` or `SOXL ATM IV > 100%`).
   - **Velocity Mode:** Compares real-time values against rolling historical baselines (5m, 30m, 1h, 5h, 1d) persisted in SQLite.

3. **Intelligent Alert Cadence & Escalation:**
   - **Wave 1:** Immediate Desktop Alert (Voice TTS, Audio sound, Modal dialog, macOS banner).
   - **Wave 2:** 2 minutes later if condition remains active: final desktop alert + concurrent Gmail escalation.
   - **Fallback Snooze:** Desktop alerts are silenced after Wave 2 to prevent fatigue. Unacknowledged alerts only fire as reminders at **6:00 AM, 9:00 AM, and 5:00 PM** local time.

4. **1-Click Mobile Action Tracking via Tailscale (NAT Traversal):**
   - Alert emails contain two 1-click action buttons:
     - `[ ✅ Confirm Delivery ]`: Confirms alert receipt, silencing all desktop reminders and snoozes.
     - `[ 🛑 Disable Rule ]`: Atomically sets `enabled = false` in `monitor_config.csv`. The daemon hot-reloads within 1 second and halts monitoring.
   - Works seamlessly from mobile devices outside the local network via Tailscale mesh VPN.

5. **Live Interactive Dashboard:**
   - Single-page dark mode UI on port `5055`.
   - Real-time rule configuration editor with right-aligned spot price and ATM IV telemetry updated every minute.
   - Live daemon process telemetry, TWS connectivity status, and SQLite database health.

---

## Repository Structure

```
├── multi_asset_monitor.py     # Core TWS Socket API daemon and tick persistence
├── monitor_ui_server.py       # Web UI server and Tailscale action webhook receiver
├── alert_router.py            # Decoupled alert state machine and escalation engine
├── gmail_service.py           # Gmail OAuth 2.0 dispatch service
├── setup_gmail_oauth.py       # One-time OAuth setup utility
├── call_tws_model_direct.py   # Direct socket TWS model pricing tool
├── index.html                 # Single-page web dashboard UI
├── monitor_config.csv         # Active monitoring rules configuration
├── start_ui.command           # 1-click macOS launcher
├── requirements.txt           # Python library dependencies
└── README.md                  # Project documentation
```

---

## Setup & Installation

### 1. Prerequisites
- Python 3.10+
- Interactive Brokers TWS or IB Gateway (API enabled on port 7496 or 7497)
- Tailscale (optional, for remote mobile action link traversal)

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Gmail OAuth (One-Time)
1. In Google Cloud Console, create an OAuth 2.0 Web Client ID.
2. Add `http://localhost:8080/` to **Authorized redirect URIs**.
3. Under **OAuth consent screen $\rightarrow$ Test users**, add your recipient email address.
4. Download client secret JSON as `credentials.json` into this folder.
5. Run the authentication script:
```bash
python3 setup_gmail_oauth.py
```
This will open your browser, generate `gmail_token.json`, and dispatch a verification test email.

---

## Running the System

### Start the Web Dashboard
```bash
python3 monitor_ui_server.py
```
Open [http://localhost:5055](http://localhost:5055) (or your Tailscale node URL `http://100.x.x.x:5055`).

### Start the Monitor Daemon
```bash
python3 multi_asset_monitor.py
```

### Run as Background macOS Services (launchd)
Place plist definitions in `~/Library/LaunchAgents/`:
- `com.ibkr.monitor_ui_server.plist`
- `com.ibkr.multi_asset_monitor.plist`

```bash
launchctl load ~/Library/LaunchAgents/com.ibkr.monitor_ui_server.plist
launchctl load ~/Library/LaunchAgents/com.ibkr.multi_asset_monitor.plist
```

---

## Moomoo Open API: SOXL Full-Depth Market Data (Level 2 / Level 3 Depth)

Consumes live, 30-to-40 level order book depth ladders, order book imbalances, microprice, and liquidity walls for `US.SOXL` via Moomoo OpenD.

### Prerequisites
1. Ensure **moomoo OpenD** is running on `127.0.0.1:11111`:
   ```bash
   open /Applications/moomoo_OpenD.app
   ```
2. Python SDK (`moomoo-api` / `futu-api`):
   ```bash
   pip3 install moomoo-api futu-api
   ```

### Quick Start Commands
- **Instant Depth Snapshot**:
  ```bash
  python3 moomoo_soxl_depth_consumer.py --mode snapshot --levels 30
  ```
- **Real-Time Live Streaming Ladder**:
  ```bash
  python3 moomoo_soxl_depth_consumer.py --mode stream --levels 15
  ```
- **Stream Depth Snapshots into SQLite Database**:
  ```bash
  python3 moomoo_soxl_depth_consumer.py --mode record --db soxl_moomoo_depth.db
  ```
- **Output JSON for Web APIs / Alerting**:
  ```bash
  python3 moomoo_soxl_depth_consumer.py --mode snapshot --json
  ```
