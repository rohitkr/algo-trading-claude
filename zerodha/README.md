# zerodha — Kite Connect execution adapter

Executes broker-agnostic `strategy_signals.OrderIntent`s on Zerodha (NFO options).
It depends only on the standard library and the tiny `strategy_signals` package;
`kiteconnect` is imported lazily, so paper trading and the tests run without it.
To move this package to its own repo, copy `zerodha/` and `strategy_signals/`
(or publish `strategy_signals` as a package).

**Default is dry run.** Nothing is sent to Zerodha unless `KITE_DRY_RUN=0`.

## Layout

| Module | What it does |
|---|---|
| `config.py` | `ZerodhaConfig.from_env()`: API key/secret, token file, product, order type, freeze qty, fill timeouts, margin buffer. Secrets come only from env / `.env` and are hidden from `repr`. |
| `auth.py` | Login URL, `request_token` → access token (`generate_session`), token file (chmod 600), expiry at 06:00 IST next day. |
| `instruments.py` | `InstrumentBook`: (underlying, expiry, strike, CALL/PUT) → tradingsymbol, lot size, tick size, from `kite.instruments("NFO")`, cached per day as CSV. |
| `broker.py` | `Broker` protocol; `KiteBroker` (place/modify/cancel/order history/LTP/basket margin/funds) and `PaperBroker` (in-memory fills). |
| `orders.py` | `OrderManager`: marketable LIMIT around LTP (tick-rounded), freeze-quantity slicing, fill polling, re-pricing via `modify_order`, cancel on timeout. |
| `margin.py` | `check_margin`: Kite **basket** margin for all legs together (so the hedge benefit is applied) vs. available funds + buffer. `estimate_basket_margin` for paper mode. |
| `executor.py` | `Executor.handle(intent)`: margin check, then legs in a safe order (below), position bookkeeping, idempotent by `intent_id`. |
| `__main__.py` | `python3 -m zerodha login` / `status`. |

## Leg ordering (hedged positions)

- **Entry:** margin check for the whole basket → **BUY hedge wings first** → SELL main legs.
  If a hedge does not fill, no short is sold (any partial hedge fill is reversed).
  If a main leg fails, whatever was filled is unwound (main first, hedge last).
- **Exit:** **BUY back main legs first** → SELL hedge wings last.
  If a main leg cannot be closed, the hedges stay open and the report says so.

## Setup

```bash
pip install kiteconnect              # only for live trading / login
```

Add to `.env` (never commit it):

```
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_DRY_RUN=1          # 0 sends real orders
KITE_PRODUCT=NRML       # NRML for positional, MIS for intraday-only
```

Other settings (all optional) are documented at the top of `config.py`:
`KITE_ORDER_TYPE`, `KITE_LIMIT_BUFFER_PCT`, `KITE_FREEZE_QTY` (check NSE's current
NIFTY freeze limit), `KITE_FILL_TIMEOUT_S`, `KITE_MAX_REPRICES`,
`KITE_MARGIN_BUFFER_PCT`, `KITE_TAG`, `KITE_TOKEN_FILE`, `KITE_ACCESS_TOKEN`.

Daily login (Kite has no password API; you log in in the browser):

```bash
python3 -m zerodha login     # open the URL, log in, paste the redirect URL back
python3 -m zerodha status    # token valid? available margin
```

## Use

```python
from zerodha import Executor, InstrumentBook, KiteBroker, ZerodhaConfig
from zerodha.auth import connected_kite

cfg = ZerodhaConfig.from_env()
kite = connected_kite(cfg)
ex = Executor(KiteBroker(kite), InstrumentBook.from_kite(kite), cfg)
report = ex.handle(intent)          # strategy_signals.OrderIntent
print(report.ok, report.message, report.plan, report.margin)
```

Paper trading: pass `PaperBroker(prices=..., margin_fn=...)` instead of `KiteBroker`.
`scripts/paper_replay.py` (repo root) replays backtest signals this way and checks the
paper P&L equals the backtest's.

## Tests

```bash
python3 -m pytest zerodha/tests -q      # offline: fake KiteConnect + PaperBroker, no network
```

## Not included / to do before going live

- A live signal source: the strategies in `backtest/` run over historical data; a live
  loop would have to emit the same `OrderIntent`s from streaming candles.
- Kite postback/websocket order updates (the order manager polls `order_history`).
- Stop-loss orders resting at the exchange (stops are strategy-side signals).
- Reconciliation with `kite.positions()` after a restart (executor state is in memory).
- Rate limits: Kite allows ~10 orders/second; the executor sends legs sequentially.
