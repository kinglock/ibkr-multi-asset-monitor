# Options & Implied Volatility (IV) Protocol

For any options pricing or historical Implied Volatility (IV) requests:
- When IBKR TWS / Gateway Socket API (or IBKR MCP) is available, ALWAYS use Interactive Brokers' internal IV pricing model (`calculateImpliedVolatility` via socket / TWS API or IBKR MCP) as the authoritative ground truth for option IV and Greeks.
- Do NOT substitute with generic theoretical Black-Scholes approximations when the live IBKR engine can be called directly.
- The TWS Socket API script `call_tws_model_direct.py` is configured and ready in this workspace to directly invoke TWS's model on port 7496 without Error 320 serializer issues.
