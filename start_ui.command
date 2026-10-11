#!/bin/bash
# 1-Click Launcher for IBKR Multi-Asset Monitor UI
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "========================================================"
echo " Starting IBKR Multi-Asset Monitor Control Dashboard"
echo "========================================================"

# Check if server is already running on port 5055
if nc -z 127.0.0.1 5055 2>/dev/null; then
  echo "[+] Server is already active on port 5055."
else
  echo "[*] Launching background UI server on http://127.0.0.1:5055..."
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 "$DIR/monitor_ui_server.py" &
  sleep 1
fi

echo "[*] Opening browser..."
open "http://127.0.0.1:5055"

echo "[+] Dashboard is running at http://127.0.0.1:5055"
