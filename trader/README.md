# Manual option trader (`trader/`)

A local web UI and execution service for manual NIFTY / BANKNIFTY / FINNIFTY / SENSEX option trades on
Zerodha. You set the entry limit, stop-loss, target, trailing SL, partial booking and auto-exit time.
Built on the existing `zerodha/` login and instrument code. **PAPER is the default.**

## Run

```bash
source venv/bin/activate
python3 scripts/get_session_token.py       # Breeze token (prices); PAPER can skip it with TRADER_PAPER_QUOTES=manual
python3 -m trader serve                    # PAPER: http://127.0.0.1:8765
python3 -m trader status                   # open trades + system status from the DB (no broker calls)
python3 -m trader reconcile                # one sync + reconciliation pass, then exit
python3 -m trader resume                   # allow new trades after a halt
```

**PAPER rehearsal without Breeze:** set `TRADER_PAPER_QUOTES=manual`, then set prices in the "Paper price"
box. The simulated exchange fills orders at those prices.

**LIVE** (real orders) needs all four switches: `TRADER_MODE=LIVE`, `ENABLE_LIVE_TRADING=true`,
`KITE_DRY_RUN=0`, and `python3 -m trader serve --live`, plus `python3 -m zerodha login` for the day.
The page then shows a red LIVE banner, and every order, exit, cancel and edit opens a confirmation dialog
with the order summary (one click; a server-side single-use token backs it).

Settings are in `.env`: see the table at the top of [`config.py`](config.py) and the block in `.env.example`.

## Architecture

| Layer | File |
|---|---|
| Web/API (stdlib `http.server`, 127.0.0.1 only) + plain HTML/JS | `web/server.py`, `web/static/` |
| Trade service: lifecycle, SL/target/trailing/partial/auto-exit | `service.py`, `lifecycle.py` |
| Validation / risk | `validation.py`, `risk.py` |
| Idempotent order placement | `orders.py` |
| Broker adapter (Kite, or the paper exchange with the same API) | `broker.py`, `paper.py` |
| Repository (SQLite) | `repository.py` |
| Reconciliation / manual-exit detection | `service.py` (`_reconcile`, `_manual_exit`, `_recheck`) |
| Monitor thread | `monitor.py` |
| Instruments (lot/tick size, tradingsymbol) | `instruments.py` over `zerodha/instruments.py` |
| Prices (Breeze) | `market.py` over `trading_data/breeze/client.py` |

PAPER wraps the simulated exchange in the same `KiteTraderBroker` LIVE uses. Every line of order,
reconciliation and recovery code therefore runs identically in both modes. Only the exchange is fake.

## Persistence: why SQLite

`data/trader/trades.sqlite` (WAL mode, `synchronous=FULL`) has these tables:
- `trades`: every field in the spec
- `orders`: every broker order, with its Kite tag, kind, status, fills and average price
- `trade_events`: the audit trail, append-only (enforced by triggers), also mirrored to `logs/trader/audit_*.jsonl`
- `action_tokens`: single-use confirmation tokens
- `system_status`

The project's DuckDB (`data/market_data.duckdb`) wasn't used because DuckDB allows one writer
process, and the Breeze pipeline and live engine already hold it. SQLite is transactional and
row-oriented, which suits order state. DuckDB can still read the file for analysis:
`ATTACH 'data/trader/trades.sqlite' (TYPE sqlite)`. A lock file allows only one trader process per
database.

## Order safety

- **Intent before send.** An `orders` row with a unique Kite tag (`mt<salt><trade><kind><n>`) is
  committed before `place_order` is called.
- **Lost responses.** A lost response or timeout marks the row `UNCERTAIN`. The row is resolved only
  by finding its tag in Zerodha's order book. If the tag is still missing after
  `TRADER_ORDER_LOOKUP_GRACE_SECONDS`, the row becomes `NOT_PLACED`, and only then may a new order be
  created. An entry is never re-sent automatically; the trade becomes REJECTED instead.
- **One order per kind.** While an order of a kind (entry, SL, partial or exit) is unresolved or
  working, a second one for the same trade is refused (`DUPLICATE_PREVENTED`).
- **Confirm tokens.** A double click or resubmitted form can't place twice: confirm uses a
  single-use token plus a compare-and-set on `READY`.
- **Every tick,** Zerodha's order book and net positions are read. Fills are recalculated from the
  order rows, never from memory. A failed read means no action that tick.
- **Before any exit, SL change or partial**, the Zerodha net position must equal what our fills
  say. A mismatch must repeat on `TRADER_RECONCILE_CONFIRMATIONS` syncs, to allow for Kite's
  position lag, and nothing is sent while it is unconfirmed. Once confirmed:
  - **Position flat:** `MANUALLY_EXITED`. Only our resting orders are cancelled (a leftover SL would
    open a new position). No exit is sent and there is no re-entry.
  - **Position smaller:** the rest was closed outside. The smaller quantity is adopted (its P&L
    estimated at the last LTP), and the SL order is resized.
  - **Position larger or flipped:** `UNKNOWN_REQUIRES_RECONCILIATION`. No more orders are sent for
    the trade. It resumes by itself once Kite matches again.
- **Fill after close.** If any order of a closed trade executes, you get a CRITICAL alert and new
  trades are blocked.
- **Opening rules.** One trade per tradingsymbol is allowed, and a symbol you already hold in Kite
  can't be opened here, so each position has one owner.

## Stop-loss, target, trailing and partial booking

- **The stop rests at Zerodha** as an SL (stop-limit) order, with the limit set
  `TRADER_SL_LIMIT_BUFFER_PCT` beyond the trigger. The position therefore stays protected if this
  process or machine dies. SL-M isn't used because Zerodha blocks it for F&O options.
- **Gaps.** A stop-limit can miss in a gap. If the LTP stays past the stop for
  `TRADER_STOP_GRACE_SECONDS`, the SL order is cancelled (the cancel is confirmed first) and a
  marketable exit is sent.
- **If the SL order is cancelled in Kite or rejected**, it is not re-placed against your wishes. The
  trade switches to a software stop on Breeze prices and shows a CRITICAL warning.
- **Targets, partial booking and trailing** are checked on Breeze prices. A resting target order
  next to a resting SL order could let both fill and flip the position.
- **Partial booking** shrinks the SL order first, then sends the partial order. The window is
  briefly under-protected rather than risking an over-exit.
- **Exits** cancel the SL, partial and entry-remainder orders, wait until those cancels are
  confirmed, then send one marketable LIMIT. The exit is re-priced every
  `TRADER_EXIT_REPRICE_SECONDS`, and repeated failures raise a CRITICAL alert.
- **Trailing** moves the SL order's trigger by modifying the order. After
  `TRADER_SL_MAX_MODIFICATIONS` changes it is cancelled and re-placed, because Kite allows about 25
  modifications per order.
- **Auto-exit time** (per trade) and **`TRADER_SQUARE_OFF_TIME`** (all trades) exit open positions
  and cancel unfilled entries.

## Editing a trade

**Edit** on an active trade opens a form. You then review the old → new diff and confirm it.

- **Unfilled or partly filled entry:** the limit price and lots can change. The Kite order is
  modified in place, never re-placed, and quantity can't go below the filled quantity.
- **Any trade, pending or active:** SL, target, trailing, partial booking (until it has happened) and
  auto-exit time.
- **Changing the SL** modifies the resting SL order. With trailing on, trailing restarts from the
  current price.
- **Checks:** edits use the creation rules. Once a position exists, SL, target and partial price are
  checked against the current price, so a stop moved into profit is allowed. Each edit is re-checked
  against risk limits and re-validated at apply time. The confirmation token covers exactly the
  reviewed changes.
- **Audit:** each applied edit is logged as `TRADE_EDITED` with old → new values.
- **Changes made in Kite:** a price or quantity changed directly in Kite, or an edit whose response
  was lost, is adopted from the order book (`ORDER_TERMS_FROM_BROKER`, `ENTRY_TERMS_SYNCED`).

## Strategy Builder: multi-leg (Phase 1)

`http://127.0.0.1:PORT/strategy` builds a multi-leg strategy (straddle, strangle, iron condor, iron fly,
or a custom set of legs) and places every leg through the exact same `preview`/`confirm` API and SL/
target/trailing engine a single manual trade uses (`trader/strategy.py`) - each leg is an ordinary
`trades` row (`group_id` = the strategy), so per-leg crash recovery, reconciliation and idempotency are
unchanged. On top of that, `StrategyService.tick()` (run every monitor tick, see `TradeService.extra_tick`)
adds ONE more layer: combined-P&L rules across the group -

- **Exit when overall profit/loss** reaches an amount, and **profit trailing** (lock a fixed floor once
  profit reaches X, trail the floor as profit grows, or both) on the group's *combined* P&L.
- **Move SL to cost**: once a leg has moved N points in its favour, its own stop is pulled to its entry
  price (breakeven).

Both act by setting `pending_exit_reason` on the affected leg(s) - the SAME field the single-trade
engine's own SL/target/daily-limit exits already set - so the next tick's ordinary per-trade processing
places the actual exit order. `StrategyService` itself never places, modifies or cancels a broker order.

**Order type** (MIS | CNC | BTST) is a workflow label, not a Zerodha product: MIS -> product MIS; CNC and
BTST both place as NRML (Zerodha has neither CNC nor BTST for F&O). BTST additionally makes
`TradeService._time_exit_reason` skip the *global* `TRADER_SQUARE_OFF_TIME` for that trade, so it carries
overnight; it is picked back up by this same engine on the next day's run exactly like any other open
trade (trades already resume across restarts regardless of which day they were opened), until its own
auto-exit time, SL, target or a manual exit closes it.

**Not implemented yet** (see "Future: multi-leg" below, which this still is for the harder parts):
broker-side atomic multi-leg entry (hedges first) or unwinding a partially-placed strategy, combined
margin, backtesting, and saved/recurring/scheduled strategies - "Start time" and "days of week" in the
builder are stored but not yet enforced (there is no scheduler; a strategy runs when you press Trade All).

## Future: multi-leg (iron condor / iron fly), the parts Phase 1 above does not cover

Plan, with the schema already prepared (`trades.group_id` and `trades.leg_role` exist, and additive
column changes are applied automatically at start):

1. Add a `trade_groups` table: strategy type, combined SL and target on net P&L or net premium,
   trailing on combined P&L, auto-exit time, status. Each leg stays a `trades` row with its own
   orders, fills and reconciliation, so crash recovery, manual-exit detection and idempotency work
   unchanged.
2. Group lifecycle in the service:
   - Enter hedges (long wings) first, then shorts.
   - If a later leg fails, unwind the filled legs, as `zerodha/executor.py` already does for spreads.
   - Evaluate combined P&L each tick.
   - Exit all legs together: shorts first, wings last.
3. Per-leg SL orders are optional for groups. A combined stop has to be monitored, since no single
   resting order can express it. The resting protection could be each short leg's own SL order.
4. Reconciliation per leg as today. A leg closed outside turns the group `UNKNOWN_REQUIRES_RECONCILIATION`
   (or partially managed, if configured).
5. Relax "one trade per symbol" to "one owner per symbol". Legs of different groups on the same
   strike would otherwise be ambiguous at the broker.
6. Margin: use Kite basket margin (`zerodha/margin.py`) before entry.

## Market data

`MARKET_DATA_PROVIDER` (see `marketdata/config.py`) picks the source.

**KITE** (paid Kite Connect plan):
- One KiteTicker WebSocket for the whole process (`marketdata/kite_stream.py`). Subscriptions are
  refcounted, and a reconnect resubscribes everything held.
- A watchdog rebuilds the connection when it gives up, goes stale (no heartbeat for
  `KITE_STALE_SECONDS`) or the daily token changes. After 06:00 IST, or a 403, it waits for the next
  `python3 -m zerodha login`.
- Prices come from ticks. When the stream can't vouch for a price, `kite.ltp()` over REST is used
  (batched, about 1 req/s). With `MARKET_DATA_FALLBACK=BREEZE`, Breeze is tried after that.
- The pages get prices pushed over `GET /api/stream` (Server-Sent Events, `trader/stream.py`):
  - LTP and running P&L of every trade of the day (open and closed) update on each tick.
  - The dashboard is refetched only when trade or order state changes, instead of every 2s.
  - The "Get LTP" and ↻ buttons are hidden while streaming.
- SL, target, trailing and partial rules still run on the monitor tick (`TRADER_POLL_SECONDS`),
  reading the latest tick.

**BREEZE** (the default, as before): Zerodha's free Personal API has no quotes, so prices come from ICICI Breeze:
- `get_quotes`, falling back to the last 1-minute bar
- the same session file and client as the rest of the repository
- calls are counted in the trader's SQLite file, not DuckDB, and capped by `TRADER_BREEZE_DAILY_BUDGET`

Keep this cap plus the live engine's and downloaders' usage under Breeze's 5,000 calls per day.

**Breeze usage:**
- **No calls** when no position is open, including while an entry is waiting in LIVE. Dashboard
  refreshes never call Breeze, and the form only calls it when you click "Get LTP".
- **One cached quote per symbol** is shared by the monitor and every request.
- **Refresh rate:** every `TRADER_QUOTE_INTERVAL_SECONDS` (15) while a target, trailing, partial
  booking or software stop needs the price. Every `TRADER_QUOTE_SLOW_SECONDS` (60) when only the
  resting SL order protects the trade.
- **Pacing** stretches the interval so the remaining budget, minus `TRADER_BREEZE_RESERVE` kept for
  exits, lasts until 15:30.
- **Roughly:** 1,300 calls/day per open symbol with a target, 375 with only an SL. The old code used
  about 3,750.

## Tests

```bash
python3 -m pytest -q trader/tests
```

The fake Kite is `paper.PaperExchange`, which can simulate lost responses, crashes before send,
order book lag, rejections, partial fills, manual exits and read failures. The tests cover:
- restart after a submitted order with no response (no duplicate)
- crash before send
- partial fills
- rejection
- manual full and partial exits (no exit order placed)
- trailing SL moves, in points and percent
- partial booking, then the remainder managed to its target or stop
- auto-exit and square-off
- risk limits blocking entries
- a lost exit response followed by a restart
- a gap through the stop-limit
- an SL cancelled outside the app
- HTTP safety: GET never places orders, token, header and host checks
- an end-to-end PAPER trade through the HTTP API

## Verified vs not verified

**Verified:**
- all tests
- a PAPER run of the real server against real Kite instrument dumps: NFO and BFO lot sizes
  NIFTY 65, BANKNIFTY 30, FINNIFTY 60, SENSEX 20; real tradingsymbols such as `NIFTY26SEP24050CE`
- one entry, SL order, trailing SL, partial booking, then an exit through the browser UI

**Not verified against real Kite or real money. Check these before LIVE:**
1. Real `kite.orders()` and `kite.positions()` shapes, including `tag`/`tags` and product-wise net
   positions (the fake copies the documented format).
2. That Kite accepts an SL order with a limit price for NFO/BFO options, a quantity change on a
   partly filled SL, and trigger and price modifications. Zerodha's limit-price-protection bands may
   reject orders far from LTP.
3. That Breeze `get_quotes` returns timely LTPs for weekly options, and **whether it serves BFO
   (SENSEX) option quotes at all.** Breeze BFO historical bars work; BFO quotes were never tested.
   If quotes fail, the last 1-minute bar is used, and targets and trailing then lag by about a minute.
4. Freeze limits (`TRADER_FREEZE_QTY_*`). Orders above them are refused, with no slicing.
5. First LIVE run: 1 lot, tight `TRADER_MAX_*` limits, and watch the audit trail.

**Known limitations:**
- No order slicing.
- Costs and charges aren't included in P&L.
- If a carried NRML order filled on an earlier day while the app was down, today's order book no
  longer has it. The trade is then reconciled from positions, as MANUALLY_EXITED with estimated P&L.
- Daily P&L counts a carried trade's full P&L, not just today's move.
- MIDCPNIFTY needs a Breeze `[options.MIDCPNIFTY]` profile before it can be enabled.
- The live engine (`live/`) halts on option positions it didn't open in its own underlying. Don't
  run both on the same underlying in LIVE until that's reconciled.
