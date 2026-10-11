#!/usr/bin/env python3
"""
Directly queries the IBKR TWS Internal Model to calculate Implied Volatility
and Greeks for SOXL Jan 21, 2028 $150 Call.
"""

import time
import threading
from ibapi.client import EClient, make_field
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

class TwsModelRunner(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.results = {}

    def tickOptionComputation(self, reqId, tickType, tickAttrib, impliedVol, delta, optPrice, pvDividend, gamma, vega, theta, undPrice):
        if impliedVol is not None:
            self.results[reqId] = {
                'iv': impliedVol,
                'delta': delta,
                'optPrice': optPrice,
                'gamma': gamma,
                'vega': vega,
                'theta': theta,
                'undPrice': undPrice
            }

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=''):
        pass

def main():
    history = [
        {'date': '2026-09-09', 'opt': 57.38, 'soxl': 125.87},
        {'date': '2026-09-10', 'opt': 49.00, 'soxl': 115.76},
        {'date': '2026-09-11', 'opt': 52.90, 'soxl': 121.82},
        {'date': '2026-09-14', 'opt': 37.70, 'soxl': 101.13},
        {'date': '2026-09-15', 'opt': 38.34, 'soxl': 102.32},
        {'date': '2026-09-16', 'opt': 38.70, 'soxl': 103.97},
        {'date': '2026-09-17', 'opt': 46.25, 'soxl': 114.82},
        {'date': '2026-09-18', 'opt': 50.00, 'soxl': 123.67},
        {'date': '2026-09-21', 'opt': 66.00, 'soxl': 141.93},
        {'date': '2026-09-22', 'opt': 73.64, 'soxl': 151.95},
        {'date': '2026-09-23', 'opt': 66.88, 'soxl': 146.25},
        {'date': '2026-09-24', 'opt': 69.80, 'soxl': 146.33},
        {'date': '2026-09-25', 'opt': 75.15, 'soxl': 151.45},
        {'date': '2026-09-28', 'opt': 67.71, 'soxl': 142.29},
        {'date': '2026-09-29', 'opt': 71.25, 'soxl': 147.00},
        {'date': '2026-09-30', 'opt': 71.99, 'soxl': 147.86},
        {'date': '2026-10-01', 'opt': 75.57, 'soxl': 153.69},
        {'date': '2026-10-02', 'opt': 82.49, 'soxl': 163.71},
        {'date': '2026-10-05', 'opt': 82.07, 'soxl': 164.27},
        {'date': '2026-10-06', 'opt': 83.50, 'soxl': 164.26},
        {'date': '2026-10-07', 'opt': 74.75, 'soxl': 158.91},
        {'date': '2026-10-08', 'opt': 64.50, 'soxl': 142.52},
        {'date': '2026-10-09', 'opt': 63.10, 'soxl': 139.76}
    ]

    app = TwsModelRunner()
    app.connect('127.0.0.1', 7496, clientId=75)
    t = threading.Thread(target=app.run)
    t.start()
    time.sleep(1)

    contract = Contract()
    contract.conId = 877203859
    contract.symbol = 'SOXL'
    contract.secType = 'OPT'
    contract.lastTradeDateOrContractMonth = '20280121'
    contract.strike = 150.0
    contract.right = 'C'
    contract.multiplier = '100'
    contract.exchange = 'SMART'
    contract.currency = 'USD'
    contract.tradingClass = 'SOXL'

    def send_calc(client, reqId, optPrice, undPrice):
        msg = ''.join([
            make_field(54), make_field(3), make_field(reqId),
            make_field(contract.conId), make_field(contract.symbol), make_field(contract.secType),
            make_field(contract.lastTradeDateOrContractMonth), make_field(contract.strike),
            make_field(contract.right), make_field(contract.multiplier), make_field(contract.exchange),
            make_field(contract.primaryExchange), make_field(contract.currency), make_field(contract.localSymbol),
            make_field(contract.tradingClass), make_field(optPrice), make_field(undPrice), make_field('')
        ])
        client.sendMsg(msg)

    print("[*] Dispatching historical prices to IBKR TWS Model engine...")
    for i, h in enumerate(history):
        send_calc(app, 3000 + i, h['opt'], h['soxl'])
        time.sleep(0.04)

    time.sleep(2)

    print("\n" + "=" * 78)
    print(" DIRECT IBKR TWS INTERNAL MODEL HISTORICAL OUTPUT")
    print(" Contract: SOXL Jan 21, 2028 $150 Call (conId: 877203859)")
    print("=" * 78)
    print(f"{'Date':<12} | {'SOXL Spot':<12} | {'Option Close':<14} | {'TWS Direct Model IV':<22}")
    print("-" * 78)

    for i, h in enumerate(history):
        req_id = 3000 + i
        res = app.results.get(req_id)
        iv_val = res['iv'] if res else None
        iv_str = f"{iv_val * 100:.2f}%" if iv_val is not None else "N/A"
        print(f"{h['date']:<12} | ${h['soxl']:<11.2f} | ${h['opt']:<13.2f} | {iv_str:<22}")

    print("=" * 78)

    # Latest Greeks computation
    def send_calc_px(client, reqId, vol, undPrice):
        msg = ''.join([
            make_field(55), make_field(3), make_field(reqId),
            make_field(contract.conId), make_field(contract.symbol), make_field(contract.secType),
            make_field(contract.lastTradeDateOrContractMonth), make_field(contract.strike),
            make_field(contract.right), make_field(contract.multiplier), make_field(contract.exchange),
            make_field(contract.primaryExchange), make_field(contract.currency), make_field(contract.localSymbol),
            make_field(contract.tradingClass), make_field(vol), make_field(undPrice), make_field('')
        ])
        client.sendMsg(msg)

    latest_iv = app.results.get(3000 + len(history) - 1, {}).get('iv', 1.070068)
    send_calc_px(app, 9999, latest_iv, 139.76)
    time.sleep(1)
    greeks = app.results.get(9999)
    if greeks:
        print("\nDIRECT IBKR TWS MODEL GREEKS (Current):")
        print(f"  Model Price : ${greeks['optPrice']:.4f}")
        print(f"  Delta       : {greeks['delta']:.4f}")
        print(f"  Gamma       : {greeks['gamma']:.5f}")
        print(f"  Vega        : {greeks['vega']:.4f}")
        print(f"  Theta       : {greeks['theta']:.4f}")
        print("=" * 78 + "\n")

    app.disconnect()

if __name__ == '__main__':
    main()
