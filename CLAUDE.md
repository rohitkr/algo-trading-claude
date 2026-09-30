# Algo Trading – Local

Local algo execution app for my own trading and testing (Python).

- Execution: Zerodha Kite (paid Kite Connect). Market data: `MARKET_DATA_PROVIDER=KITE` (Breeze fallback;
  Breeze is still needed for expired-option history / backtests).
- Web UI: `python -m trader serve` (port 8765), often running LIVE with real money. Never place, modify or
  cancel orders, and never restart the live trader; test only on an isolated PAPER copy (its own `TRADER_DB`,
  another port).
- I commit myself: stay on my current branch and don't commit unless I ask. Run the full test suite
  (`venv/bin/python -m pytest`) for every change.
