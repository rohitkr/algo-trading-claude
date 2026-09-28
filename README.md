# Breeze → DuckDB market-data pipeline

Downloads historical **index/underlying** and **options** candles from the ICICI Direct **Breeze API**, validates them and stores them in a local **DuckDB** database that a future backtesting engine can query directly.

```text
ICICI Breeze → Authentication (manual OTP) → Market data / Options downloaders → Validation → DuckDB
```

**In scope today:** login + session token, a reusable Breeze client, a generic market-data downloader, a daily missing-day updater, a generic options downloader, validation, resume, data-quality reports and tests.

**Not built yet:** backtesting engine, web UI, strategy builder, paper/live trading, order execution, user accounts. The data layer is designed for them: queries go through `trading_data.storage.CandleStore` and need no knowledge of Breeze, auth or downloads.

---

## Requirements

- macOS (developed on a MacBook Air) with Python 3.11+ (tested with 3.13 from Homebrew)
- An ICICI Direct account with Breeze API access, and a Breeze app (API key + secret) whose redirect URL is set (for example `http://localhost:3000/`)
- Google Chrome (used by the login automation; otherwise Playwright's Chromium, see below)

## Installation

```bash
cd /Users/rohit/git/algo-trading-claude

python3 -m venv venv

source venv/bin/activate

pip install -r requirements.txt
```

Only needed if Google Chrome is **not** installed (the login script falls back to Playwright's Chromium):

```bash
python3 -m playwright install chromium
```

Every script runs the same way: activate the venv, then `python3 <script>.py`:

```bash
cd /Users/rohit/git/algo-trading-claude
source venv/bin/activate
python3 scripts/download_market_data.py
```

> The old `venv/` in this folder had been copied from another project (`ICICIDirectAlgo`), so `source venv/bin/activate` put a non-existent path on `PATH`. It was recreated in place; if you ever copy the project again, recreate the venv with the commands above.

## Environment configuration (`.env`)

Secrets live only in `.env`, which is gitignored. Copy the template and fill it in:

```bash
cp .env.example .env
```

| Variable | Required | Purpose |
|---|---|---|
| `ICICI_USER_ID` | for login | ICICI Direct user id, typed into the login page by the session script |
| `ICICI_PASSWORD` | for login | ICICI Direct password (never logged) |
| `BREEZE_API_KEY` | yes | Breeze app API key |
| `BREEZE_API_SECRET` | yes | Breeze app secret (never logged) |
| `EXPIRY_START` | no | Options expiry range start (YYYY-MM-DD); overrides `expiry_start` of every options profile |
| `EXPIRY_END` | no | Options expiry range end |
| `BREEZE_SESSION_TOKEN` | no | Use a token obtained elsewhere instead of `data/.breeze_session.json` |
| `MARKET_DATA_INSTRUMENT` | no | Instrument(s) for the market downloader, e.g. `NIFTY` or `NIFTY,BANKNIFTY` (default `NIFTY`) |
| `MARKET_DATA_EXCHANGE` | no | Safety check: must match the instrument's exchange in `settings.toml` (`NSE`, `BSE`) |
| `MARKET_DATA_INTERVAL` | no | `1minute` (default), `5minute`, `30minute`, `1day` |
| `MARKET_DATA_START_DATE` | no | First day of the dataset (default `2023-09-27`) |
| `MARKET_DATA_END_DATE` | no | `today` (default, moving) or a fixed `YYYY-MM-DD` |
| `MARKET_DATA_STOCK_CODE` | no | Only for an instrument that is not in `settings.toml`: its Breeze stock code |
| `BREEZE_DAILY_API_LIMIT` | no | Lower the daily API ceiling (default 4900) |
| `DUCKDB_PATH` | no | Alternative database file |

Non-secret settings (instruments, exchanges, strike steps, market hours, API limits, expiry rules) are in [`config/settings.toml`](config/settings.toml). Holidays are in [`config/holidays.toml`](config/holidays.toml).

> `test.py` contains a hard-coded API key and secret. It is gitignored now, but move those values into `.env` and consider regenerating the secret in the Breeze portal.

## 1. Session token (manual OTP)

Breeze needs a fresh session token every day.

```bash
source venv/bin/activate
python3 scripts/get_session_token.py
```

What happens:

1. Reads `ICICI_USER_ID`, `ICICI_PASSWORD`, `BREEZE_API_KEY` and `BREEZE_API_SECRET` from `.env`.
2. Opens Chrome on the official Breeze login page (`https://api.icicidirect.com/apiuser/login?api_key=…`) and fills the user id and password.
3. Asks **you** for the OTP in the terminal (typed visibly so you can check it), types it in and submits.
4. ICICI redirects to your app's redirect URL with `?apisession=<token>`; the script captures the token from that redirect. The redirect page itself does not need to load.
5. Saves the token to `data/.breeze_session.json` (gitignored, owner-only permissions) with today's date, then opens a test session to verify it.

If the page layout does not match the script's selectors, it tells you to finish the login yourself in the same window and still captures the token. Other modes:

```bash
python3 scripts/get_session_token.py --manual   # no automation: log in in any browser, paste the redirected URL
python3 scripts/get_session_token.py --check    # just verify the stored token
```

All other scripts read the stored token. A token from a previous day is refused with a message to run this script again.

## 2. Market data: backfill and catch-up (one script)

`scripts/download_market_data.py` does both the initial historical download and every later update. What it downloads comes from configuration, never from the code.

### Configure

Defaults live in `[market_data]` in `config/settings.toml`: NIFTY, NSE, 1-minute, `2023-09-27` → `today` (about 3 years). Override any of them in `.env`:

```text
MARKET_DATA_INSTRUMENT=NIFTY
MARKET_DATA_EXCHANGE=NSE
MARKET_DATA_INTERVAL=1minute
MARKET_DATA_START_DATE=2023-09-27
MARKET_DATA_END_DATE=today
```

- `MARKET_DATA_END_DATE=today` is a moving end: each run resolves it to the last **finished** session (today counts after 15:45 IST; on a weekend or holiday it's the last trading day). A fixed date such as `2024-06-30` downloads exactly that range.
- The script never computes "3 years ago" itself; the start date is whatever you configure.
- The instrument name maps to its Breeze stock code, exchange and trading calendar in `[instruments.*]` in `config/settings.toml`. NIFTY → `NIFTY`/NSE, BANKNIFTY → `CNXBAN`/NSE, SENSEX → `BSESEN`/BSE with the BSE calendar. If `MARKET_DATA_EXCHANGE` disagrees with the registry, the script stops with a clear error instead of fetching the wrong thing.

### Run the initial 3-year NIFTY download

```bash
cd /Users/rohit/git/algo-trading-claude
source venv/bin/activate
python3 scripts/get_session_token.py        # once per day
python3 scripts/download_market_data.py
```

The script first prints a plan from DuckDB alone (no API calls), then downloads in batches with progress:

```text
Market Data Downloader

Instrument : NIFTY (Breeze stock code NIFTY)
Exchange   : NSE  (calendar NSE)
Interval   : 1minute
Configured : 2023-09-27 → today  (resolves to 2026-09-25)
Existing   : no data yet  (0/726 trading days complete)
Missing    : 726 trading days: 2023-09-27 → 2026-09-25

NIFTY batch 1/155 2023-09-27..2023-10-03 | downloaded 1548 | inserted 1500 (total 1500) | API calls 2 (today 4/4900) | failed batches 0
...
NIFTY: batches 155/155 | rows downloaded 287,152 | rows inserted 272,250 | API calls 316 | failed batches 0
```

That's a real run from 27 Sep 2026: 3 years of NIFTY 1-minute data took about 4 minutes and 316 API calls.

### Re-run later (catch-up)

Run **the same command** again whenever you like, for example after the close each day:

```bash
source venv/bin/activate
python3 scripts/download_market_data.py
```

The same script can be executed repeatedly. It checks DuckDB for existing data and downloads only missing or new periods through the configured end date. You never change `MARKET_DATA_START_DATE` for updates.

How it decides what to fetch:

1. It lists every trading day from START to the resolved END using the instrument's exchange calendar (weekends and holidays are skipped).
2. It counts the candles DuckDB actually holds for each of those days.
3. A day is **complete** at 370 or more of the 375 bars (09:15–15:29). Anything with fewer bars is missing (0) or incomplete (under 370), wherever it sits in the range, so a gap in the middle is repaired just like new days at the end. It does **not** use `MAX(timestamp) + 1`.
4. Missing days are grouped into batches and only those are downloaded.

A re-run with nothing missing makes no API calls. A live check deleted one whole day in Jan 2025, half of 2 Sep 2026 and the last two days; the next run found exactly those 4 days and fixed them with 3 API calls.

`scripts/daily_update.py` still exists and runs the same code.

### Batching and API limits

- Breeze `get_historical_data_v2` returns at most **1000 candles per request**, and it returns the most recent 1000 in the window (verified live). For NIFTY 1-minute data a request can span about 2.6 trading days.
- Missing days are grouped into batches of consecutive trading days spanning at most `chunk_days` = 7 calendar days (about 1,900 rows). Within a batch the client pages backwards past the 1000-row cap, which is 2 calls for a 5-day batch.
- Each batch is fetched, validated and committed **in one transaction** on its own, so it can be retried and resumed independently.
- Throttling: 0.5 s after every call, a 100 calls/minute ceiling (Breeze's documented limit) and a 4900 calls/day hard stop. Usage is stored in DuckDB (`api_usage`) and shared by every script.
- Network errors and 5xx responses are retried 3 times with exponential backoff (2 s, 4 s, 8 s). A batch that still fails is recorded as `failed` in `market_day_status`, reported, and the remaining batches continue. Failed days are always retried on the next run.

### Validation

Every batch is checked before it is stored: the response must be well formed, timestamps must parse, OHLC must be positive numbers with high ≥ open/close ≥ low, and there must be no duplicate timestamps. Weekend and holiday rows and bars outside 09:15–15:29 are dropped (Breeze returns index bars up to 15:39), and rows are stored in time order. No candle is ever invented: a day with fewer than 370 bars is reported as incomplete, not filled.

Special or partial sessions such as Muhurat trading can be added per calendar in `config/holidays.toml` under `[NSE.special_sessions]`, for example `"2024-11-01" = { open = "18:00", close = "18:59" }`. That day then counts as a trading day, and its expected bar count scales to the session length. None are configured by default, because Breeze's coverage of those sessions is unverified.

### Switch instrument

Change `.env` and run the same script. The data goes into the same table, keyed by instrument and exchange.

```text
MARKET_DATA_INSTRUMENT=BANKNIFTY
MARKET_DATA_EXCHANGE=NSE
```

```text
MARKET_DATA_INSTRUMENT=SENSEX
MARKET_DATA_EXCHANGE=BSE
```

Or, for a one-off without editing `.env`:

```bash
python3 scripts/download_market_data.py --instruments BANKNIFTY SENSEX --start 2024-01-01 --end 2024-06-30
python3 scripts/download_market_data.py --dry-run          # plan only
```

To add a new instrument permanently, add an `[instruments.NAME]` block to `config/settings.toml` with its Breeze `stock_code`, `exchange` and `calendar`. For a quick experiment, set `MARKET_DATA_INSTRUMENT`, `MARKET_DATA_STOCK_CODE` and `MARKET_DATA_EXCHANGE` in `.env` instead.

### Verify

```bash
python3 scripts/verify_market_data.py
python3 scripts/verify_market_data.py --instruments SENSEX
```

It reads DuckDB only and shows the earliest and latest timestamp, total candles, trading days, duplicates, missing and incomplete days, and the candle count and first/last bar of the first and latest trading day. The exit code is 0 when everything is clean.

### Read candles (OHLCV) for a period

```bash
python3 scripts/show_candles.py --start 2025-08-01 --end 2025-08-01                          # one day of 1-minute bars
python3 scripts/show_candles.py --start "2025-08-01 09:15" --end "2025-08-01 10:00"
python3 scripts/show_candles.py --instrument BANKNIFTY --start 2025-08-01 --end 2025-08-31 --resample 1D
python3 scripts/show_candles.py --start 2024-01-01 --end 2024-12-31 --resample 1h --tail 20 --csv nifty_2024_1h.csv
```

It reads DuckDB only (no API calls, no session needed). Dates are inclusive and times are IST. `--resample` builds bigger bars (`5min`, `15min`, `1h`, `1D`) from the stored 1-minute data. `--head`/`--tail` limit what is printed, and `--csv` saves the rows to a file.

### Resume after interruption or failure

- **Ctrl+C, a crash or a closed laptop:** every finished batch is already committed. Run the same command again and it continues with whatever DuckDB is still missing. A batch that was mid-flight is simply fetched again; upserts make that safe.
- **Daily API limit reached:** the run stops cleanly. Run it again tomorrow.
- **Expired session:** run `get_session_token.py`, then the downloader again.
- **Progress bookkeeping lost:** `market_day_status` only counts attempts. Whether a day exists is always decided from `market_candles`, so deleting the status rows (or the whole table) loses nothing.

## 3. Options

```bash
python3 scripts/download_options.py                                      # NIFTY, EXPIRY_START..EXPIRY_END
python3 scripts/download_options.py --underlying BANKNIFTY --expiry-start 2025-08-01 --expiry-end 2025-08-31
python3 scripts/download_options.py --underlying SENSEX --threads 2
python3 scripts/download_options.py --retry-no-data                      # re-try contracts that returned nothing before
python3 scripts/download_options.py --forward-fill                       # legacy fill, stored separately (see below)
```

Each underlying is an `[options.<NAME>]` profile in `config/settings.toml`. The NIFTY profile keeps the legacy defaults:

| Setting | NIFTY default | Meaning |
|---|---|---|
| `stock_code` / `exchange` | `NIFTY` / `NFO` | Breeze identifiers (BANKNIFTY is `CNXBAN`/`NFO`, SENSEX is `BSESEN`/`BFO`) |
| `expiry_start` / `expiry_end` | 2025-08-01 / 2025-08-10 | expiry range (`.env` `EXPIRY_START/END` override) |
| `strike_step` | 50 | strike grid, also used for ATM rounding |
| `strikes_each_side` | 20 | base range ATM ± 20 strikes |
| `dynamic_strike_buffer` | 500 | range widened to cover spot min/max over the pre-expiry window ± 500 |
| `days_before_expiry` | 45 | download window per contract and spot window for dynamic strikes |
| `lot_size` | 65 | contract metadata |
| `max_threads` | 3 | concurrent contract downloads |
| `api_delay` | 0.5 s | pause after every call, per thread |
| `daily_api_limit` | 4900 | hard daily ceiling |
| `retry_last_days` | 10 | empty contract → retry the last 10 days before expiry |
| `expiry_rules` | weekly Thu until 2025-08-31, weekly Tue from 2025-09-01 | holiday expiries move to the previous trading day |

How a run works, per expiry:

1. **Expiries** come from the profile's `expiry_rules` (weekly or monthly-last-weekday segments with date ranges), adjusted back from holidays, de-duplicated. For NIFTY this reproduces the legacy `get_weekly_expiries()` exactly (tested against a port of it for 2023–2026).
2. **ATM** = `round(open / strike_step) * strike_step`, using the underlying's daily open on the **entry day**, which is the previous expiry (`atm_reference = "previous_expiry"`). The underlying's daily candles are downloaded automatically.
3. **Strikes**: ATM ± `strikes_each_side`, widened by the spot range over `days_before_expiry` plus the buffer (legacy `get_dynamic_strikes()`). ATM and range are saved in `option_expiry_plan` (the legacy ATM cache).
4. **CALL and PUT** for every strike are fetched by `max_threads` workers from `expiry − days_before_expiry` to expiry, paging past the 1000-row cap. If nothing comes back, the last `retry_last_days` days are retried. A retry never overwrites stored data.
5. Each contract is validated and written in **one transaction**, and its status goes to `option_contracts` (`complete`, `no_data`, `failed`).
6. A report lists missing CE/PE pairs and **entry-day failures**: from the second expiry on, the ATM CALL and PUT must have candles on the entry day.

**Resume:** a contract is skipped only if `option_contracts` says complete **and** `option_candles` holds its rows, so the database stays the source of truth. Ctrl+C, a crash or the API limit all leave finished contracts saved, and the next run continues with the rest.

**API budget:** a full NIFTY expiry is roughly 100–130 contracts × up to ~17 paged calls (45 days × 375 bars / 1000) when everything traded, so expect about 2–3 expiries per day within 4900 calls. Far out-of-the-money strikes trade less, need fewer pages and are cheaper.

**Forward fill (legacy, opt-in):** the old code padded every day to 375 bars. That is off by default because invented candles distort backtests. `--forward-fill` (or `forward_fill = true`) writes the generated bars to `option_candles_synthetic` only. The view `option_candles_with_synthetic` combines both tables with an `is_synthetic` flag. The canonical `option_candles` table only ever holds real Breeze data.

## 4. Data-quality report

```bash
python3 scripts/data_report.py                                    # market instruments + NIFTY options
python3 scripts/data_report.py --instruments NIFTY --options NIFTY BANKNIFTY
```

It reads only DuckDB (no API calls) and reports, per instrument: trading days requested/downloaded/missing/incomplete, duplicates, out-of-session and weekend/holiday rows. Per options profile it reports expiries, contract counts by status, missing CE/PE pairs, entry-day failures and synthetic rows, plus API calls used today. Text and JSON copies go to `reports/`.

## 5. Backtest (NIFTY option selling)

```bash
python3 scripts/run_backtest.py                                   # last 2 months of stored data
python3 scripts/run_backtest.py --start 2026-07-27 --end 2026-09-25 --capital 1000000
python3 scripts/run_backtest.py --offline                         # only option data already in DuckDB
```

Runs two strategies from `backtest/strategies.py` and writes `reports/backtest_<underlying>_<start>_<end>.html` (charts, stats, every trade) plus a `.json` with the same data:

- **Positional range breakout:** a close above the 09:15–11:15 NIFTY range sells a 100-point ITM PUT, and a close below it sells a 100-point ITM CALL, on the next weekly expiry. The stop is 0.5% of NIFTY against the entry, with one re-entry at the entry level, and the position is held to expiry.
- **0DTE ITM straddle:** on each expiry day it sells a 100-point ITM CALL and PUT, with a 30% premium stop per leg and one re-entry at cost, exiting at 15:15. The entry time is chosen walk-forward from the previous 8 expiry days.

Sizing is 5 lots (325 qty) per leg. Charges use Indian F&O rates plus 0.5-point slippage per side. Every parameter is a dataclass field in `backtest/strategies.py` (`RangeBreakoutParams`, `ZeroDteParams`).

Option contracts that the trades need are downloaded from Breeze on first use (only those days) and stored in `option_candles`, so re-runs are offline.

### Hedged variants (credit spreads)

Both strategies sell naked options. `backtest/hedged.py` wraps either one without changing it: same entries, exits, stops and re-entries, plus one bought wing per sold leg, N points further OTM (sold PUT K → bought PUT K−N, sold CALL K → bought CALL K+N). Wing prices are real Breeze candles, fetched on first use like the sold legs.

```bash
python3 scripts/run_backtest.py --hedge-width 200                 # HTML report for the hedged variant (default 0 = naked)
python3 scripts/compare_hedged.py --widths 200 300                # naked vs hedged: reports/naked_vs_hedged.md + _metrics.csv
python3 scripts/paper_replay.py --hedge-width 200                 # replay signals through zerodha/ with a paper broker
```

`compare_hedged.py` reports trades, net P&L, win rate, max drawdown, avg win/loss, profit factor, largest loss, estimated peak margin/capital (`backtest/margin.py`, SPAN + exposure approximation) and defined risk per trade.

## 6. Zerodha execution (`zerodha/`)

Everything Zerodha-specific (Kite Connect login, NFO instruments, orders, basket margin, paper broker, executor) lives in [`zerodha/`](zerodha/README.md). Strategies talk to it only through the neutral `strategy_signals` package (`OrderIntent`, `OptionLeg`); `backtest/signals.py` converts backtest trades into intents. Nothing in `backtest/` or `trading_data/` imports `zerodha`, and `kiteconnect` is optional.

## 7. Live trading engine (`live/`)

`live/` runs the two strategies above on live data: ICICI Breeze for market data (Zerodha's free
Personal plan has none) and Zerodha Kite for execution. The flow is
Breeze → `MarketDataProvider` → `Strategy` → `RiskManager` → `ExecutionBroker` → Zerodha, and each
part can be replaced on its own. The strategy rules are shared with the backtest
(`backtest/rules.py`), both NAKED and HEDGED are selectable, and every risk limit comes from `.env`.
Modes are BACKTEST (DuckDB replay), PAPER (default) and LIVE (**not wired yet**). Several independently
configured algo instances (`INSTANCES=...`, e.g. a hedged positional and a 0DTE) run in one process,
each with its own risk caps, state file, audit trail and Kite order tag.

```bash
python3 -m live replay --start 2026-07-27 --end 2026-09-25 --parity   # live engine vs backtest on stored data
python3 -m live run                                                    # PAPER trading for today
python3 -m live squareoff                                              # emergency: flatten and stop
```

Architecture, configuration, Breeze/Zerodha requirements, PAPER/LIVE setup, risk settings,
monitoring, reconciliation, emergency square-off and the pre-LIVE checklist are in
[`live/README.md`](live/README.md).

## DuckDB

- **File:** `data/market_data.duckdb` (gitignored). Every script creates the file and schema automatically on first use; `python3 scripts/init_db.py` does only that and prints the table sizes. No database server is needed.
- **Timestamps** are exchange-local (IST) wall-clock times in naive `TIMESTAMP` columns; the timestamp is the start of the candle (09:15 … 15:29).
- **No duplicates:** primary keys on the logical identity, with every write an upsert. Nothing is ever deleted by a download.

| Table | Key | Contents |
|---|---|---|
| `market_candles` | instrument, exchange, timeframe, ts | OHLCV (+ `open_interest` if any) for indices/underlyings |
| `option_candles` | underlying, exchange, expiry, strike, option_right, timeframe, ts | option OHLCV + open interest (real data only) |
| `option_candles_synthetic` | same | forward-filled bars (opt-in) |
| `market_day_status` | instrument, exchange, timeframe, trade_date | per-day status (`complete`, `incomplete`, `empty`, `failed`), bar count, attempts |
| `option_contracts` | underlying, exchange, expiry, strike, option_right, timeframe | contract status, rows, first/last ts, attempts, retry used |
| `option_expiry_plan` | underlying, exchange, expiry | ATM day, spot open, ATM, strike range |
| `api_usage` | usage_date (IST) | Breeze calls made that day by any script |

Inspect it with the DuckDB CLI (`brew install duckdb`) or Python:

```bash
duckdb -readonly data/market_data.duckdb
```

```sql
-- coverage of one instrument
SELECT min(ts), max(ts), count(*) FROM market_candles
WHERE instrument = 'NIFTY' AND exchange = 'NSE' AND timeframe = '1minute';

-- daily candle counts (375 = full session)
SELECT CAST(ts AS DATE) AS trading_date, count(*) AS candle_count FROM market_candles
WHERE instrument = 'NIFTY' AND timeframe = '1minute'
GROUP BY 1 ORDER BY 1;

-- days that are not complete
SELECT CAST(ts AS DATE) AS trading_date, count(*) AS candle_count FROM market_candles
WHERE instrument = 'NIFTY' AND timeframe = '1minute'
GROUP BY 1 HAVING count(*) < 370 ORDER BY 1;

-- duplicate check (always empty: the primary key forbids duplicates)
SELECT ts, count(*) AS cnt FROM market_candles
WHERE instrument = 'NIFTY' AND exchange = 'NSE' AND timeframe = '1minute'
GROUP BY ts HAVING count(*) > 1 ORDER BY ts;

-- per-day download status (complete / incomplete / empty / failed)
SELECT status, count(*) FROM market_day_status WHERE instrument = 'NIFTY' GROUP BY 1;

-- NIFTY 1-minute candles for a range
SELECT * FROM market_candles
WHERE instrument = 'NIFTY' AND timeframe = '1minute'
  AND ts BETWEEN '2025-08-01' AND '2025-08-10 23:59:59'
ORDER BY ts;

-- bars per day (should be 375)
SELECT CAST(ts AS DATE) AS day, count(*) FROM market_candles
WHERE instrument = 'BANKNIFTY' AND timeframe = '1minute' GROUP BY 1 ORDER BY 1;

-- one option contract
SELECT ts, open, high, low, close, volume, open_interest FROM option_candles
WHERE underlying = 'NIFTY' AND expiry = DATE '2025-08-07' AND strike = 24000 AND option_right = 'CALL'
ORDER BY ts;

-- option chain snapshot at 10:00
SELECT strike, option_right, close, open_interest FROM option_candles
WHERE underlying = 'NIFTY' AND expiry = DATE '2025-08-07' AND ts = TIMESTAMP '2025-08-06 10:00:00'
ORDER BY strike, option_right;

-- download progress
SELECT expiry, status, count(*) FROM option_contracts GROUP BY ALL ORDER BY ALL;
SELECT * FROM api_usage ORDER BY usage_date DESC;
```

From Python (what a backtester would use; no Breeze involved):

```python
from trading_data.storage import CandleStore
store = CandleStore("data/market_data.duckdb", read_only=True)
spot = store.get_market_candles("NIFTY", "2025-08-01", "2025-08-10", timeframe="1minute")
ce = store.get_option_candles("NIFTY", expiry="2025-08-07", strike=24000, right="CALL")
```

## Tests

```bash
source venv/bin/activate
python3 -m pytest -q tests zerodha/tests live/tests               # unit tests, no network, no credentials
BREEZE_LIVE=1 python3 -m pytest tests/test_live_breeze.py -v      # optional live API checks (needs today's session)
```

The unit tests cover config/env loading, trading days, weekends, holidays, expiry generation (compared with a port of the legacy function), holiday adjustment, ATM, dynamic strikes, DuckDB initialisation/insertion/duplicate prevention, missing-day detection and catch-up, API-limit stop and resume, option contract identity, CE/PE pairing, and paging past the 1000-row cap. They use a fake Breeze SDK.

## Project architecture

```text
config/
  settings.toml         instruments, options profiles, calendars, API limits, paths
  holidays.toml         exchange holidays by calendar (extend per year)
trading_data/
  config.py             loads settings.toml + holidays.toml + .env into typed dataclasses
  log.py                console (INFO) + logs/<script>.log (DEBUG), secret redaction
  calendar.py           TradingCalendar (trading days, sessions, special sessions) + expiry generation
  strikes.py            ATM and dynamic strike range (pure functions)
  validation.py         normalise Breeze rows, clean/validate, day classification, isolated forward-fill
  storage.py            CandleStore: all DuckDB SQL (schema, upserts, progress, read API)
  reports.py            data-quality report from DuckDB
  app.py                shared wiring for scripts
  breeze/
    client.py           BreezeClient: session, API budget, throttling, retries, pagination
    auth.py             login flow (Playwright + manual OTP, or manual paste)
    session_store.py    daily session token file
  downloaders/
    market.py           generic market-data downloader: end-date resolution, missing-day planning, batches
    options.py          generic options downloader
backtest/               data feed, strategies (rules in rules.py, shared with live/), hedged overlay, margin
                        estimates, naked-vs-hedged comparison, trade -> OrderIntent conversion, cost model, report
strategy_signals/       broker-agnostic OrderIntent / OptionLeg + ExecutionBroker/ExecutionResult (no dependencies;
                        the only strategy <-> broker contract)
zerodha/                Kite Connect adapter: auth, instruments, orders, margin, paper broker, executor,
                        ExecutionBroker adapter (+ its own tests)
live/                   live trading engine: strategies, risk, engine, state, audit, replay (+ README, tests)
scripts/                command-line entry points (python3 scripts/<name>.py):
                          get_session_token.py, download_market_data.py (backfill + catch-up),
                          verify_market_data.py, show_candles.py, download_options.py, data_report.py, init_db.py,
                          daily_update.py (alias of download_market_data.py)
tests/                  pytest suite (live tests opt-in)
data/  logs/  reports/  generated locally, gitignored
```

The flow is config → `BreezeClient` → downloaders → validation → `CandleStore`. Only `breeze/` knows about the SDK, only `storage.py` contains SQL, and only `calendar.py` knows about weekdays and holidays. Instrument differences (stock code, exchange, strike step, lot size, expiry rules, calendar) are configuration, so there is one downloader for all instruments.

**Changes from the legacy options script, and why:**

- DuckDB replaces CSV, so filename helpers (`expiry_to_prefix`, `parse_strike_from_filename`, `file_is_valid`/`MIN_FILE_SIZE_BYTES`) are replaced by proper columns and by row counts.
- Paging was added. The legacy single request per contract would have received only the last ~2.7 days of a 45-day window because of the 1000-row cap (verified live).
- Forward fill is opt-in and stored separately, so real and generated candles cannot be confused.
- API calls are counted centrally (including session creation, spot, option and retry calls) in DuckDB, so the daily limit is shared across scripts and restarts. A 100 calls/minute ceiling was added on top of the 0.5 s delay, because 3 threads at 0.5 s could otherwise exceed Breeze's per-minute limit.
- Bars outside 09:15–15:29 are dropped during cleaning (as in the legacy `clean_file`). Breeze returns index/BFO bars up to 15:39.
- The legacy `main()` was not part of the brief, so two choices were inferred: the ATM reference day is the **previous expiry**, and the dynamic range uses daily **closes**. Both are configurable or isolated in `strikes.py`.

## Troubleshooting

| Problem | What to do |
|---|---|
| `Public Key does not exist` | `BREEZE_API_KEY` in `.env` is wrong. |
| Invalid credentials / login page error | Check `ICICI_USER_ID` / `ICICI_PASSWORD`; try `--manual` and log in yourself. |
| OTP not accepted or never asked | Re-run the script (OTPs expire quickly). If the form changed, finish in the open browser window or use `--manual`. |
| `Stored session token is from …` / `session rejected` | Tokens last one day: run `python3 scripts/get_session_token.py`. |
| `Daily API limit reached` | Everything so far is saved. Run the same command tomorrow and it continues. `api_usage` shows the count. |
| Network errors | Calls are retried 3 times with backoff; if a window still fails the run stops and a re-run resumes. |
| Missing days | Run `python3 scripts/download_market_data.py`; it repairs gaps anywhere in the range. Days listed as skipped returned no data 3 times (unlisted closure or no data); retry with `download_market_data.py --force`. |
| Incomplete days (< 370 bars) | Listed in the report. Re-running retries them; genuine exchange gaps stay incomplete and are not filled. |
| Duplicate data | Not possible by design (primary keys + upserts); the report shows the count anyway. |
| Options missing CE/PE | Listed under "missing CE/PE pairs". Often an illiquid strike; try `--retry-no-data`. |
| New holiday year | Append the dates to `config/holidays.toml`. |
| `MARKET_DATA_EXCHANGE=… does not match …` | The instrument trades on a different exchange in `settings.toml` (SENSEX is BSE). Fix `.env`. |
| `Unknown instrument` | Add it to `[instruments.*]` in `settings.toml`, or set `MARKET_DATA_STOCK_CODE` + `MARKET_DATA_EXCHANGE`. |
| Failed batches in the summary | A batch kept failing after 3 retries (network or API error). It's logged in `logs/market_data.log`; re-running retries it. |
| `database is locked` | Another script has the DuckDB file open (DuckDB allows one writer). Close the other process or `duckdb` CLI session. |
| Days before a certain date always come back empty | Breeze has no 1-minute history that far back for that instrument; after 3 attempts those days are reported as skipped. Move `MARKET_DATA_START_DATE` later. |
| NIFTY500 returns nothing | Its Breeze stock code is not verified (`NIF500`, `NIFTY500` and `NIFTY 500` all returned no data). Set the right code in `settings.toml`. |
