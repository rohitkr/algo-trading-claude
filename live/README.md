# Live trading engine (`live/`)

Runs the two backtested NIFTY option-selling strategies on live data:

```text
ICICI Breeze ──► MarketDataProvider ──► Strategy ──► Signal ──► RiskManager ──► ExecutionBroker ──► Zerodha Kite
 (1-min bars,     trading_data/breeze/    live/        live/       live/risk.py     zerodha/execution.py
  quotes)         live.py                 strategies   interfaces                   (PaperBroker today)
```

> **Status:** BACKTEST and PAPER modes are complete. **LIVE order execution is not wired
> into the engine yet**: `TRADING_MODE=LIVE` stops with an error. See [LIVE mode setup](#6-live-mode-setup).

Contents: [1 Architecture](#1-architecture) · [2 Environment](#2-environment-configuration) ·
[3 Breeze](#3-icici-breeze-requirements) · [4 Zerodha](#4-zerodha-requirements) ·
[5 PAPER](#5-paper-mode-setup) · [6 LIVE](#6-live-mode-setup) · [7 Risk](#7-risk-management-configuration) ·
[8 Start](#8-starting-the-engine) · [9 Monitor/reconcile](#9-monitoring-and-reconciliation) ·
[10 Emergency square-off](#10-emergency-square-off) · [Backtest spec](#what-the-backtest-does-the-specification-live-reproduces) ·
[Live vs backtest](#live-vs-backtest-differences) · [Implemented / simulated / verify](#implemented-simulated-and-what-to-verify-before-live)

## 1. Architecture

| Seam | Interface | Implementations |
|---|---|---|
| Market data | `MarketDataProvider` (`live/interfaces.py`) | `BreezeMarketData` (`trading_data/breeze/live.py`, PAPER/LIVE), `ReplayMarketData` (`live/replay.py`, BACKTEST) |
| Strategy | `Strategy` (`live/interfaces.py`) | `PositionalBreakout`, `ZeroDteStraddle` (`live/strategies.py`) |
| Risk | `RiskManager` (`live/risk.py`) | one implementation, every limit from config |
| Execution | `ExecutionBroker` (`strategy_signals/execution.py`) | `ZerodhaExecutionBroker` (`zerodha/execution.py`) over `PaperBroker` (PAPER/BACKTEST) or `KiteBroker` (LIVE, not wired) |

- **Strategies never import a broker or a data source.** They see completed 1-minute bars and
  prices through the provider, and emit `Signal`s. The engine turns an approved signal into a
  broker-neutral `strategy_signals.OrderIntent`.
- **The rules are shared with the backtest.** `backtest/rules.py` holds every rule as a pure function
  (range, breakout, strike selection, spot stop, premium stop and fill, re-entry conditions,
  walk-forward entry-time choice). `backtest/strategies.py` was refactored to call it. Its output is
  byte-identical to before the refactor (checked on three backtest runs), and `live/strategies.py`
  calls the same functions with the same parameter classes.
- **Zerodha-specific code stays in `zerodha/`**, which depends only on `strategy_signals`, so it can
  be moved to its own repository. PAPER uses the *same* Zerodha order path as LIVE (basket-margin
  check, hedge-first leg ordering, freeze slicing, marketable limits, fill waiting, re-pricing,
  unwinds). Only the exchange is simulated.
- **Breeze access reuses `BreezeClient`**: the same session token file, persisted daily API budget,
  throttle, retries and candle validation as the downloaders. One addition is `get_quote`.

### Several algo instances in one process

```text
Account (one process)  ── Breeze feed, broker, kill switches, reconciliation, _account.json
  ├─ instance posH300  ── positional, HEDGED 300, holds to expiry;  own risk caps, state, audit, order tag
  └─ instance zd1      ── 0DTE, NAKED, intraday;                    own risk caps, state, audit, order tag
```

One process runs every configured instance: `python3 -m live run`, or `--instance ID` for a subset.
Reasons for one process rather than one per instance:
- DuckDB allows one writer, and the Breeze API budget is counted there.
- Zerodha reports net positions per contract for the whole account, so only a process that sees every
  instance's book can tell whether the broker is right, which instance owns a difference, or whether
  it's your manual trade.
- One Breeze poll feeds every instance.

Each instance is otherwise independent:
- its own strategy, config, risk limits and daily caps
- its own state file `data/live/<mode>/<ID>.json`
- its own audit files `logs/live/audit_<mode>_<ID>_<date>.jsonl` and `trades_<mode>_<ID>.csv`
- its own Kite order tag (the instance id)

An exception in one instance is logged, and the others keep running.
```text
live/
  interfaces.py   MarketDataProvider, Strategy, Signal, RiskCheck/RiskDecision (+ re-exports execution types)
  config.py       EngineConfig from .env (every parameter; table at the top of the file)
  selection.py    expiry / ATM / ITM / hedge-wing selection (backtest calendar + rules)
  strategies.py   PositionalBreakout, ZeroDteStraddle (minute state machines on backtest/rules.py)
  risk.py         pre-trade checks + sizing
  engine.py       Account (loop, kill switches, reconciliation across instances, account caps, data health)
                  + TradingEngine = one instance (orders, exits/retries, carry rules, MTM, its own caps)
  state.py        <mode>/<ID>.json per instance + _account.json (atomic writes)
  audit.py        audit_<mode>_<ID>_<date>.jsonl + trades_<mode>_<ID>.csv
  replay.py       BACKTEST: DuckDB replay provider + comparison with backtest/strategies.py
  app.py          wiring for BACKTEST / PAPER
  __main__.py     CLI: run, replay, status, squareoff, resume (all take --instance ID)
  tests/          offline tests (python3 -m pytest live/tests)
backtest/rules.py                 shared rule functions
strategy_signals/execution.py     ExecutionBroker protocol, ExecutionResult, LegFill
trading_data/breeze/live.py       BreezeMarketData (+ BreezeClient.get_quote)
zerodha/execution.py              ZerodhaExecutionBroker; build_live_broker (guarded, not used by the engine yet)
zerodha/broker.py                 + KiteBroker.positions/open_orders, price hook (no kite.ltp on the Personal plan);
                                    PaperBroker slippage + partial fills
zerodha/instruments.py            + SyntheticInstrumentBook (paper symbols)
```

## 2. Environment configuration

All strategy, risk, timing and engine settings are read from the root `.env`, and shell variables
override it. The full table with defaults is at the top of [`live/config.py`](config.py), and
`.env.example` has a ready-made block. The only settings kept elsewhere are the Kite order handling
(`KITE_*`, `zerodha/config.py`) and the expiry calendar and holidays (`config/settings.toml`,
`config/holidays.toml`). Nothing in the code is a fixed trading value. The most important settings:

**Instances.** `INSTANCES` lists them. `<ID>__KEY` sets any key for one instance. A key that isn't
overridden falls back to the unprefixed `KEY`, and then to the default.
- Ids are 1–12 letters or digits, because the id becomes the Kite order tag.
- Account-wide keys can't be overridden per instance; a prefixed one is ignored with a warning. They
  are `TRADING_MODE`, `ENABLE_LIVE_TRADING`, `UNDERLYING`, `SHARED_CONTRACTS`, `ACCOUNT_*`, `ENGINE_*`
  and `PAPER_*`.
- Without `INSTANCES`, there is one instance per strategy in `STRATEGIES`, named after the strategy.

```ini
TRADING_MODE=PAPER            # BACKTEST | PAPER | LIVE   (default PAPER)
ENABLE_LIVE_TRADING=false     # must be true for LIVE (and LIVE is not wired yet anyway)
INSTANCES=posH300,zd1
posH300__STRATEGY=positional
posH300__POSITION_MODE=HEDGED # NAKED | HEDGED (buy a wing per short)
posH300__HEDGE_WIDTH=300      # wing distance in points
posH300__INTRADAY_ONLY=false  # hold to expiry (positional default)
posH300__EXPIRY_OFFSET=0      # 0 = nearest weekly expiry after the entry day, 1 = next week, ...
posH300__RISK_MAX_DAILY_LOSS=40000
zd1__STRATEGY=zerodte         # defaults: NAKED, intraday
zd1__RISK_MAX_DAILY_LOSS=25000
# shared defaults (any of these can be set per instance too)
ENTRY_START_TIME=09:15        # no new entries before
ENTRY_END_TIME=15:00          # no new entries after (signals after it are ignored, never opened)
FORCE_EXIT_TIME=15:15         # intraday instances: everything still open is squared off
RISK_CAPITAL=1000000          # per instance
RISK_MAX_RISK_PER_TRADE=60000
RISK_MAX_DAILY_LOSS_ENABLED=true
RISK_MAX_DAILY_LOSS=50000
RISK_MAX_DAILY_PROFIT_ENABLED=false
RISK_MAX_DAILY_PROFIT=0
RISK_MAX_TRADES_PER_DAY=6
RISK_MAX_OPEN_POSITIONS=3
RISK_MAX_LOTS_PER_TRADE=5
RISK_MAX_QTY_PER_TRADE=325
# SHARED_CONTRACTS=false      # account-wide: may two instances hold the same contract?
```

**Strategy defaults.**
- Positional: `POSITION_MODE=HEDGED`, `HEDGE_WIDTH=300`, `INTRADAY_ONLY=false` (held to expiry, like the
  backtest).
- 0DTE: `POSITION_MODE=NAKED`, `INTRADAY_ONLY=true`.

Nothing is mandatory. Holding overnight NAKED, or holding overnight with `KITE_PRODUCT=MIS` (which
Zerodha auto-closes around 15:20), only logs a WARNING at start.

An unprefixed `POSITION_MODE=` or `HEDGE_WIDTH=` applies to **every** instance. Remove it if you want
the per-strategy defaults.

### Intraday only and what it does to the positional strategy

Positional instances hold to expiry by default (`INTRADAY_ONLY=false`). Each position records:
- `carry_allowed` and `hold_until` (the expiry-day exit)
- `position_mode`, `entry_date`, both legs with entry and exit order ids, the stop, and the hedge
  details

That position is managed across days: the stop is checked each day, re-entry is allowed until 15:00 on
expiry day, and it exits at 15:15 on expiry day. A safety exit fires only if it is somehow still open
after that.

Intraday instances (0DTE always, positional if you set `INTRADAY_ONLY=true`) are squared off at
`FORCE_EXIT_TIME`, once that minute's bar has completed. A position of theirs found on a new day is an
error, and it is exited at once.

With `INTRADAY_ONLY=true` on a positional instance:

- **Positional:** the position and its re-entry window end at `FORCE_EXIT_TIME` on the entry day.
  In the backtest it is held until the next weekly expiry.
- **Everything else:** the engine squares off anything still open at `FORCE_EXIT_TIME`, and never
  carries a position into a new day.
- **Unchanged:** `backtest/strategies.py`, contract selection (still the next weekly expiry), the
  stop, and re-entry.

**This removes the positional strategy's edge.** Replaying June 2025 – September 2026 through the
live engine (`python3 -m live replay --parity` vs `--parity --intraday`):

| Variant | Positional positions | Positional gross P&L | 0DTE gross P&L |
|---|---:|---:|---:|
| Backtest (holds to expiry) | 14 | ₹2,91,850 | ₹1,77,635 |
| Live engine, `INTRADAY_ONLY=false` | 13 | ₹2,23,681 | same as below |
| Live engine, `INTRADAY_ONLY=true` | 18 | **−₹2,454** (before costs) | ₹1,48,509 (unchanged by intraday) |

The positional strategy's profit came from holding the sold ITM option to expiry. An intraday version
needs its own backtest before it is traded. `INTRADAY_ONLY=false` restores the backtested holding
period.

Strategy parameters default to the backtest's values (`RangeBreakoutParams`, `ZeroDteParams`):
itm points, stop %, re-entry, lookback. Changing them changes the strategy, so change them only
together with a new backtest.

## 3. ICICI Breeze requirements

- A Breeze API app (`BREEZE_API_KEY`, `BREEZE_API_SECRET`) and **today's session token**:
  `python3 scripts/get_session_token.py` each trading morning, before starting the engine.
- **Plan:** Breeze's API is free for ICICI Direct customers and includes historical 1-minute data
  (`historical_data_v2`, which this repository already uses for backtesting) and live quotes
  (`get_quotes`). No extra subscription is used.
- **Polling, not streaming:** every ~15 s at most per instrument, the engine asks
  `historical_data_v2` only for bars after the last completed bar it holds. 0DTE legs also get a
  `get_quotes` price at most every 20 s, for faster stop detection. Breeze's WebSocket feed is not
  used.
- **API budget:** the engine shares the persisted 4,900/day budget with the other scripts. It uses
  roughly 1–3 calls/minute with no 0DTE position and ~6–10/minute with 0DTE legs open (about
  1,500–3,500 on an expiry day). `RISK_MIN_API_BUDGET` (default 300) blocks new entries when too
  few calls remain, so exits can still be priced.
- **Historical data for 0DTE:** the walk-forward entry time needs the previous 8 expiry days of NIFTY
  1-minute data in DuckDB. Run `python3 scripts/daily_update.py` after each session. Missing option
  contracts are fetched on demand. Without that history the 0DTE strategy does not trade that day,
  and logs why. It does not silently fall back to 09:20.
- **DuckDB lock:** the engine keeps `data/market_data.duckdb` open while it runs (for the API
  budget), so run the daily update after the engine stops.

## 4. Zerodha requirements

- Kite Connect **Personal (free) plan** is enough: orders, positions, margins and instruments. **No
  market data from Kite is used.** `kite.ltp` is replaced by Breeze prices through
  `KiteBroker(price_fn=...)`.
- Daily login: `python3 -m zerodha login` (browser redirect caught on `KITE_REDIRECT_URL`).
- `KITE_PRODUCT`: `MIS` is enough with `INTRADAY_ONLY=true`. `NRML` is required if you turn intraday
  off, because the positional strategy then holds overnight until expiry.
- Order settings (`KITE_ORDER_TYPE`, `KITE_LIMIT_BUFFER_PCT`, `KITE_FREEZE_QTY`,
  `KITE_FILL_TIMEOUT_S`, `KITE_MAX_REPRICES`, `KITE_MARGIN_BUFFER_PCT`) are documented in
  `zerodha/config.py` and apply to PAPER too.

## 5. PAPER mode setup

PAPER uses live Breeze data and simulates fills at the Breeze price ± `PAPER_SLIPPAGE_POINTS`
(default 0.5, as in the backtest cost model). Margin uses the SPAN+exposure estimate from
`zerodha/margin.py`. No Zerodha login is needed and no order can reach Zerodha: the paper adapter
refuses a `KiteBroker`.

```bash
source venv/bin/activate
python3 scripts/get_session_token.py        # Breeze token for today
python3 -m live run                         # TRADING_MODE defaults to PAPER; runs until 15:35
```

Before relying on live results, replay history through the same engine (BACKTEST mode, no network):

```bash
python3 -m live replay --start 2026-07-27 --end 2026-09-25 --parity   # strategy logic only (limits lifted)
python3 -m live replay --start 2026-07-27 --end 2026-09-25            # with your configured risk limits
```

`--parity` prints how many backtest trades the live engine reproduced (same entry bar, strike, exit
minute and exit reason) and the P&L of each side.

## 6. LIVE mode setup

**Not wired yet, deliberately.** The engine refuses `TRADING_MODE=LIVE`. What exists:
`zerodha/execution.py: build_live_broker()` builds the Zerodha adapter over a real `KiteBroker`.
It raises unless **all four** switches are on: `TRADING_MODE=LIVE`, `ENABLE_LIVE_TRADING=true`,
`KITE_DRY_RUN=0` and the `--live` command-line flag. Connecting it to `live/app.py` is the remaining
step, to be done only after the checklist in
[what to verify before LIVE](#implemented-simulated-and-what-to-verify-before-live).

## 7. Risk-management configuration

Every entry is checked, and each check is written to the audit log whether it passes or fails.
Exits (stops, time exits, square-off) are never blocked.

| Check | Setting | Behaviour |
|---|---|---|
| Max risk per trade | `RISK_MAX_RISK_PER_TRADE` | naked: stop distance × `RISK_POSITIONAL_DELTA` (positional) or entry × stop % (0DTE); hedged: min(that, width − credit). Sizes lots down; blocks if < 1 lot |
| Position sizing from capital/margin | `RISK_CAPITAL`, `RISK_MARGIN_UTILISATION_PCT` | lots ≤ min(capital, available margin) × util% ÷ margin per lot (Kite basket margin in LIVE, estimate in PAPER) |
| Max quantity / lots | `RISK_MAX_QTY_PER_TRADE`, `RISK_MAX_LOTS_PER_TRADE`, `*_LOTS` | the smallest limit wins; the binding limit is logged |
| Max trades per day | `RISK_MAX_TRADES_PER_DAY` | entries incl. re-entries |
| Max open positions | `RISK_MAX_OPEN_POSITIONS` | positional + each 0DTE leg count separately |
| Max daily loss (per instance) | `RISK_MAX_DAILY_LOSS_ENABLED`, `RISK_MAX_DAILY_LOSS` | that instance's daily P&L ≤ −limit: **halts its new entries** until the next day (open positions keep their stops; nothing is force-closed) |
| Max daily profit (per instance) | `RISK_MAX_DAILY_PROFIT_ENABLED`, `RISK_MAX_DAILY_PROFIT` | that instance's daily P&L ≥ cap: **squares off everything that instance holds, including overnight positions**; `MAX_PROFIT_REACHED`; no more trades for it that day. Other instances are unaffected |
| Daily P&L (both caps) | — | `TradingEngine.daily_pnl()`: realised today + unrealised mark-to-market of the instance's open positions. A position held over several days is re-based to the previous day's last mark each morning, so each day counts only its own move (including an overnight gap), with no double counting |
| Account caps (optional) | `ACCOUNT_MAX_DAILY_LOSS`, `ACCOUNT_MAX_DAILY_PROFIT` (0 = off) | sum over all instances: loss blocks every instance's entries; profit squares off everything |
| One contract, one owner | `SHARED_CONTRACTS` (default false) | an instance may not open a contract another instance (or your manual position) holds, so every broker position has exactly one owner |
| Re-entry limit | `RISK_MAX_REENTRIES`, `POSITIONAL_REENTRY`, `ZERODTE_REENTRY` | one re-entry per signal (per leg for 0DTE), at cost, same day (intraday), inside the entry window; never after a MANUAL_EXIT |
| Stop-loss protection | strategy rules | every entry must carry a stop; stops are evaluated on every completed bar (0DTE also on quotes) |
| Duplicate orders | automatic | every intent id is saved before sending; the same id is never sent twice (survives restarts); the same contract can't be opened twice |
| Market hours / entry window | NSE calendar, `ENTRY_START_TIME`, `ENTRY_END_TIME`, `FORCE_EXIT_TIME` | no entries outside the session or the window; a signal after `ENTRY_END_TIME` is refused, never opened and then force-exited |
| Intraday | `INTRADAY_ONLY`, `FORCE_EXIT_TIME` | everything still open is squared off at `FORCE_EXIT_TIME` |
| Stale data / signal | `RISK_MAX_DATA_LAG_SECONDS` | no entries if the newest bar or the signal bar is older (e.g. catching up after a restart) |
| API budget | `RISK_MIN_API_BUDGET` | no entries when Breeze calls run low |
| Order-status verification | `KITE_FILL_TIMEOUT_S`, `KITE_MAX_REPRICES` | every order is polled to a terminal status; ids and statuses are audited |
| Rejected / partial orders | automatic (zerodha executor) | a failed hedge means no short is sold; a failed or partial main leg is unwound; nothing is recorded as open |
| Broker / network failure | automatic | an unclear result halts entries and reconciles immediately; failed exits retry every poll, and after `ENGINE_EXIT_RETRY_LIMIT` a CRITICAL alert and halt |
| Data outage | `RISK_DATA_OUTAGE_SECONDS`, `RISK_SQUARE_OFF_ON_DATA_OUTAGE` | CRITICAL alert; optional square-off |
| Reconciliation | `ENGINE_RECONCILE_SECONDS`, `ENGINE_RECONCILE_CONFIRMATIONS`, `ENGINE_HALT_ON_UNKNOWN_POSITIONS` | the broker is the truth: see §9 |
| Manual changes in Kite | automatic | a position closed in Kite becomes `MANUAL_EXIT`: no orders, no re-entry for that signal. One the engine cannot interpret is left alone, and new entries halt |
| Emergency square-off | `ENGINE_KILL_FILE` | see §10 |

## 8. Starting the engine

```bash
python3 -m live run                      # PAPER, every instance; idles until 09:16, stops at 15:35
                                         # (exits at once on a holiday). Ctrl-C stops WITHOUT closing positions
python3 -m live run --instance posH300   # only some instances
python3 -m live status [--instance ID]   # per instance: positions, halt, caps status, blocked signals
python3 -m live resume [--instance ID]   # clear an instance's halt (without --instance: also the account's)
```

At start the engine reconciles every instance's saved state (`data/live/paper/<ID>.json`) with the
broker before trading (§9). A carried positional spread is checked leg by leg against Zerodha, then
managed as normal. In PAPER the simulated exchange keeps its own positions in
`data/live/paper/_paper_exchange.json`, so this check is real. Edit that file to rehearse a manual
exit.

## 9. Monitoring and reconciliation

- **Console / `logs/live_paper.log`**: one line per decision. WARNING and above need attention.
- **Audit trail `logs/live/audit_paper_<ID>_<date>.jsonl`** (per instance, plus `audit_paper_account_<date>.jsonl` for reconciliation, kill switches and account caps): one JSON object per event, each with its `instance`.

  | Event | What it records |
  |---|---|
  | `signal` | timestamp, underlying price, signal, contract, strike, expiry, reference price, stop, hedge details, re-entry flag |
  | `risk_check` | every check (passed or failed), sized lots and the binding limit, estimated risk and margin |
  | `order` | plan, order ids, statuses, fills, anything unwound, margin |
  | `position_opened` / `position_closed` | entry/exit prices, stop, hedge (strike, prices, net credit, max loss), quantity, exit reason, P&L |
  | Operational | `decision` (range, skipped signals, chosen 0DTE entry time), `reconcile`, `halt_new_entries`, `exit_failed`, `data_outage`, `square_off` |

- **`logs/live/trades_paper_<ID>.csv`**: one row per closed position (instance, status incl. MANUAL_EXIT, P&L total and today, estimated flag, days held, entry and exit order ids).
- **Reconciliation: the broker's positions are the truth.** It runs at startup, after every order
  and every `ENGINE_RECONCILE_SECONDS`, comparing each engine position (all its legs) with Zerodha's
  net positions. Broker position reports can lag a fill, so a mismatch must repeat on
  `ENGINE_RECONCILE_CONFIRMATIONS` (default 2) consecutive checks before the engine acts, except at
  startup, where it acts immediately.

  | Broker shows | Engine does |
  |---|---|
  | every leg flat | **`MANUAL_EXIT`**: records the position as closed outside the engine (P&L estimated at the last price, flagged). **Never** a stop. No order, and re-entry is disabled for that signal. |
  | same side, consistently smaller | adopts the broker's quantity and keeps managing (stop, exit) |
  | anything else (larger, flipped, one leg of a spread gone) | **stops managing it**: no stop, re-entry or orders for that signal. It stays read-only as `UNMANAGED` in state, and new entries halt. |
  | a sent entry that was never recorded (crash mid-order) | broker holds the legs: rebuilt from the broker quantities and the saved signal (stop, strategy, re-entry count; entry price = the signal's reference price, flagged). Broker flat: dropped. |
  | an option of the underlying the engine never opened | read-only `UNMANAGED`, CRITICAL alert, halt (`ENGINE_HALT_ON_UNKNOWN_POSITIONS`). Never touched. |
  | open orders at startup | halt: the engine cannot know what they belong to |

  **Several instances.**
  - Every order carries its instance id as the Kite tag. Open orders at startup that carry an
    instance's tag halt that instance; untagged ones are treated as yours and ignored.
  - With `SHARED_CONTRACTS=false` (default), every contract has one owner, so the rules above apply to
    that instance.
  - With `true`, a contract held by several instances is checked as a sum. If it doesn't match, the
    difference can't be attributed, so every instance holding it halts and nothing is changed.

  After a halt, check the positions in Kite and run `python3 -m live resume [--instance ID]`. Positions that stay
  `UNMANAGED` are yours to manage by hand. They drop out of the state once the broker no longer
  holds them.

## 10. Emergency square-off

```bash
python3 -m live squareoff          # writes the kill file; the running engine closes everything and stops
python3 -m live squareoff --now    # engine not running: closes the saved positions directly
```

```bash
python3 -m live squareoff --instance posH300   # only that instance (KILL_posH300); the others keep running
```

A running engine checks the kill files at every poll.
- **`ENGINE_KILL_FILE`** (default `data/live/KILL`) closes every position of every instance,
  including overnight spreads, then stops the process.
  - Exit orders go main legs first, wings last, and failures are retried.
  - The engine refuses to start while the file exists.
- **`KILL_<ID>`** closes that instance's positions and skips the instance while the file exists.
  After deleting the file, run `python3 -m live resume --instance ID`.
- **Delete kill files only after checking positions in Kite.**
- Your manual positions are never touched. Ctrl-C only stops the loop; it does
not close positions. As a last resort, square off in the Kite app: the next reconciliation will
report the difference and halt.

---

## What the backtest does (the specification live reproduces)

| Item | Backtest behaviour (file:line) |
|---|---|
| Underlying | NIFTY 50 index (Breeze `NIFTY`/NSE cash); options NFO NIFTY weeklies; lot 65, strike step 50 (`config/settings.toml`) |
| Data source | 1-minute candles from Breeze `historical_data_v2`, stored in DuckDB (`backtest/data.py:22`); missing option contracts fetched on demand (`:62`); last traded price fallback (`:82`) |
| **Positional** range | 09:15–11:14 high/low (`backtest/rules.py: range_bars`, `strategies.py:74`) |
| Entry | first 1-minute **close** above the high (UP) or below the low (DOWN), 11:15–15:00; UP checked first; one signal per day |
| Contract | next weekly expiry strictly after the entry day; ATM = round(spot/50)×50; UP → sell PUT ATM+100, DOWN → sell CALL ATM−100 |
| Entry price | option close of the signal minute |
| Stop | NIFTY 1-minute close ≥/≤ 0.5% against the entry spot, checked only up to 15:15 each day (Breeze freezes 15:17–15:19) |
| Re-entry | once, when NIFTY closes back at or through the original entry spot, until 15:00 on expiry day; new 0.5% stop from that close |
| Exit | 15:15 bar on expiry day |
| One at a time | a new signal is skipped while a position is open; the backtest also uses hindsight (it skips while a *future* re-entry is pending) |
| **0DTE** days | expiry days only |
| Entry time | walk-forward: the 09:20–14:30 (10-min) candidate with the best net P&L over the previous 8 expiry days; ties → earliest |
| Contracts | CALL at ATM−100 and PUT at ATM+100 (both ITM), ATM from the NIFTY close at the entry minute |
| Stop | per leg: option 1-minute high ≥ entry × 1.30, filled at the stop (or the bar's open when it gaps past) |
| Re-entry | once per leg, when the option closes ≤ the original entry premium, before 15:15; new 30% stop |
| Exit | 15:15 |
| Sizing | fixed 5 lots × 65 = 325 per leg |
| Naked or hedged | **naked**. A hedged wrapper exists (`backtest/hedged.py`: a wing N points further OTM, opened and closed with the short), chosen with `--hedge-width` |

Live keeps both modes: `POSITION_MODE=NAKED` reproduces the backtest, and `POSITION_MODE=HEDGED`
adds the wing (`HEDGE_WIDTH`), bought before the short and sold after it. Each hedged position
records its net credit, maximum loss ((width − credit) × qty), margin and combined P&L.

## Live vs backtest differences

1. **One-minute delay.** A bar can only be acted on once it has completed (plus Breeze's delay), so
   orders go out in the minute after the backtest's fill minute. BACKTEST replay uses the backtest's
   own prices, so any real slippage is extra.
2. **0DTE stops.** The backtest fills exactly at the stop price. Live detects the stop from a
   completed bar (or a quote at or above the stop) and sends a marketable order, so it usually fills
   worse. In the June 2025 – September 2026 replay, stopped 0DTE legs lost a few hundred to a few
   thousand ₹ more each.
3. **Positional hindsight.** Live can't know whether a pending re-entry will happen.
   `POSITIONAL_NEW_SIGNAL_CANCELS_REENTRY=true` (default) lets a new breakout replace it; `false`
   keeps waiting. Both reproduce 51 of 54 backtest trades over June 2025 – September 2026; the
   default is closer on P&L.
4. **Data gaps.** If the 15:15 expiry bar is missing, live exits on the next bar; the backtest marks
   the position to market at the end of its data. The range needs `POSITIONAL_MIN_RANGE_BARS`
   (default 100 of 120) bars; the backtest checks ≥ 100 bars for the whole day.
5. **Risk limits** can size a trade below 5 lots or block it. The backtest has no limits.
   `replay --parity` lifts them.
6. **Intraday only (default).** Positional exits at `FORCE_EXIT_TIME` on the entry day instead of on
   expiry; its re-entry is same-day only. This is the largest difference by far (see
   [Intraday only](#intraday-only-default-and-what-it-does-to-the-positional-strategy)).
   `replay --parity` compares with it off; add `--intraday` to measure it.
7. **Daily caps, `MANUAL_EXIT` and reconciliation** have no backtest equivalent.

## Implemented, simulated, and what to verify before LIVE

**Implemented and tested** (`python3 -m pytest live/tests zerodha/tests tests`):

- signal generation for both strategies on the shared rules
- Breeze polling provider
- contract, expiry and wing selection
- the full risk manager
- engine order flow with audit
- exit retries, daily-loss halt, max-profit square-off, data-health alerts, kill switch and square-off
- intraday force exit and the entry window
- broker-truth reconciliation at startup and while running: `MANUAL_EXIT`, adopted quantities,
  unmanaged/unknown positions, and recovery of an entry sent just before a crash
- saved state (positions, pending entries, duplicate guard)
- NAKED/HEDGED modes
- BACKTEST replay with a comparison against the backtest
- PAPER trading through the real Zerodha order path

**Simulated or estimated:**

- **Fills:** at the Breeze price ± slippage. There is no queue, depth or impact.
- **Margin:** the SPAN+exposure estimate in PAPER.
- **Partial fills and rejections:** only simulated in tests.
- **Reconciliation:** runs against the paper broker.
- **Instrument symbols:** synthetic in PAPER.

**Not done:** wiring LIVE execution (`build_live_broker`) into the engine.

**Verify before ever setting `ENABLE_LIVE_TRADING=true`:**

1. Several full PAPER sessions, including an expiry day and an overnight positional carry. The audit
   trail should show sensible signals, sizing and exits, and `python3 -m live status` should match.
2. `python3 -m live replay --parity` over the latest data still matches the backtest.
3. **Breeze timing on a live day:** how many seconds after each minute the completed bar arrives, and
   whether `get_quotes` LTP is timely for NIFTY weekly options. This decides how late signals and
   stops really are.
4. The Breeze API budget used on an expiry day (`api_usage` table) stays comfortably under the limit.
5. **Kite, read-only:** `python3 -m zerodha status`.
   - `kite.instruments("NFO")` lot size matches `LOT_SIZE`.
   - Tradingsymbol and expiry lookups work for the next weekly expiry.
   - Basket margin for a 1-lot spread matches the Kite app.
   - `kite.positions()` / `kite.orders()` return what reconciliation expects, including carried NRML positions.
6. `KITE_FREEZE_QTY` and the lot size match NSE's current values. Check `KITE_ORDER_TYPE=LIMIT` with
   `KITE_LIMIT_BUFFER_PCT` against real option spreads (Zerodha may reject MARKET orders for
   options).
7. Start with the smallest size: `POSITIONAL_LOTS=1`, `ZERODTE_LOTS=1`, `RISK_MAX_LOTS_PER_TRADE=1`,
   a low `RISK_MAX_DAILY_LOSS`. Consider `POSITION_MODE=HEDGED` for defined risk.
8. Rehearse the emergency procedure (§10) and know how to square off in the Kite app yourself.
9. Only then wire `build_live_broker` into `live/app.py` and run with all four switches.
