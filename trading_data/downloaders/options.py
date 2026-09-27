"""Generic historical options downloader (one implementation for every [options.*] profile).

Per expiry (legacy behaviour, DuckDB instead of CSV):
    ATM from the underlying's open on the ATM/entry day -> dynamic strike range
    -> CALL + PUT for each strike, fetched by up to `max_threads` workers
    -> no data? retry the last `retry_last_days` days before expiry
    -> validated rows upserted into option_candles, status into option_contracts

Resume: a contract is skipped when option_contracts says complete AND
option_candles actually holds rows for it. Each contract is written in one
transaction after all its pages arrive, so an interrupt never leaves a
half-written contract. Hitting the daily API limit stops cleanly and the next
run continues with the remaining contracts.
"""
from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

from ..breeze.client import ApiLimitReached, BreezeClient, BreezeError, HistoricalRequest, SessionExpiredError
from ..calendar import TradingCalendar, generate_expiries, previous_expiry
from ..config import OptionsProfile, Settings
from ..log import get_logger
from ..storage import CandleStore, OptionContract
from ..strikes import atm_strike, dynamic_strikes
from ..validation import ValidationReport, clean_candles, forward_fill_day, records_to_frame
from .market import MarketDownloader

log = get_logger("options")
TIMEFRAME = "1minute"
RIGHTS = ("CALL", "PUT")


@dataclass
class ExpiryPlan:
    expiry: date
    atm_date: date
    spot_open: float
    atm: int
    strikes: list[int]


@dataclass
class FetchResult:
    contract: OptionContract
    frame: pd.DataFrame
    report: ValidationReport
    used_retry: bool
    error: str | None = None


@dataclass
class OptionsRunSummary:
    profile: str
    expiries: list[date] = field(default_factory=list)
    planned_contracts: int = 0
    skipped_existing: int = 0
    downloaded: int = 0
    no_data: int = 0
    failed: int = 0
    retried: int = 0
    rows_written: int = 0
    synthetic_rows: int = 0
    api_calls: int = 0
    stopped_reason: str | None = None


class OptionsDownloader:
    def __init__(self, settings: Settings, store: CandleStore, client: BreezeClient | None,
                 profile: OptionsProfile):
        self.settings = settings
        self.store = store
        self.client = client
        self.p = profile
        self.cal = TradingCalendar(settings.calendars[profile.calendar])
        self.underlying = settings.instrument(profile.underlying)
        self._stop = threading.Event()

    # -- expiries & ATM ------------------------------------------------------
    def expiries(self, start: date | None = None, end: date | None = None) -> list[date]:
        return generate_expiries(self.p.expiry_rules, start or self.p.expiry_start, end or self.p.expiry_end, self.cal)

    def atm_date_for(self, expiry: date) -> date:
        if self.p.atm_reference == "previous_expiry":
            prev = previous_expiry(self.p.expiry_rules, expiry, self.cal)
            if prev:
                return prev
        if self.p.atm_reference == "expiry_day":
            return expiry
        return self.cal.previous_trading_day(expiry)

    def spot_window(self, expiries: list[date]) -> tuple[date, date]:
        first = min(expiries)
        start = min(self.atm_date_for(first), first - timedelta(days=self.p.days_before_expiry))
        return start - timedelta(days=5), max(expiries)

    def ensure_spot(self, expiries: list[date]) -> None:
        """Make sure the underlying's daily candles cover every ATM day and strike window."""
        start, end = self.spot_window(expiries)
        MarketDownloader(self.settings, self.store, self.client).download(
            self.underlying.name, start, end, timeframe="1day")

    def _spot_daily(self, start: date, end: date) -> pd.DataFrame:
        return self.store.get_market_candles(self.underlying.name, start, end, timeframe="1day",
                                             exchange=self.underlying.exchange)

    def plan_expiry(self, expiry: date) -> ExpiryPlan | None:
        cached = self.store.get_expiry_plan(self.p.underlying, self.p.exchange, expiry)
        atm_date = self.atm_date_for(expiry)
        start = min(atm_date, expiry - timedelta(days=self.p.days_before_expiry))
        daily = self._spot_daily(start - timedelta(days=5), expiry)
        by_day = {ts.date(): float(o) for ts, o in zip(daily["ts"], daily["open"])}
        closes = {ts.date(): float(c) for ts, c in zip(daily["ts"], daily["close"])}

        if cached:
            atm = int(float(cached["atm"]))
            lo, hi, step = int(float(cached["strike_low"])), int(float(cached["strike_high"])), int(float(cached["strike_step"]))
            return ExpiryPlan(expiry, pd.Timestamp(cached["atm_date"]).date(), float(cached["spot_open"]),
                              atm, list(range(lo, hi + step, step)))

        # ATM day open; fall back to the nearest earlier day with data (e.g. unlisted closure)
        d, spot = atm_date, by_day.get(atm_date)
        for _ in range(5):
            if spot:
                break
            d = self.cal.previous_trading_day(d)
            spot = by_day.get(d)
        if not spot:
            log.error("%s %s: no underlying open price around %s; cannot determine ATM", self.p.name, expiry, atm_date)
            return None
        if d != atm_date:
            log.warning("%s %s: no spot data on %s, using %s open for ATM", self.p.name, expiry, atm_date, d)

        atm = atm_strike(spot, self.p.strike_step)
        strikes = dynamic_strikes(atm, expiry, closes, step=self.p.strike_step,
                                  strikes_each_side=self.p.strikes_each_side,
                                  days_before_expiry=self.p.days_before_expiry,
                                  buffer=self.p.dynamic_strike_buffer, round_to=self.p.dynamic_strike_round)
        self.store.save_expiry_plan(self.p.underlying, self.p.exchange, expiry, d, spot, atm,
                                    strikes[0], strikes[-1], self.p.strike_step)
        log.info("%s %s: ATM %d (open %.2f on %s), %d strikes %d..%d", self.p.name, expiry, atm, spot, d,
                 len(strikes), strikes[0], strikes[-1])
        return ExpiryPlan(expiry, d, spot, atm, strikes)

    # -- single contract --------------------------------------------------------
    def _request(self, c: OptionContract) -> HistoricalRequest:
        return HistoricalRequest(self.p.stock_code, self.p.exchange, self.p.product_type, TIMEFRAME,
                                 expiry=c.expiry, right=c.right.lower(), strike=c.strike)

    def fetch_contract(self, c: OptionContract) -> FetchResult:
        try:
            return self._fetch_contract(c)
        except (ApiLimitReached, SessionExpiredError, _Stopped):
            raise
        except BreezeError as exc:
            return FetchResult(c, pd.DataFrame(), ValidationReport(), False, error=str(exc)[:500])

    def _fetch_contract(self, c: OptionContract) -> FetchResult:
        """Primary window (expiry - days_before_expiry .. expiry); if empty, retry the last N days."""
        req = self._request(c)
        end = self.cal.session_bounds(c.expiry)[1]
        windows = [(c.expiry - timedelta(days=self.p.days_before_expiry), False),
                   (c.expiry - timedelta(days=self.p.retry_last_days), True)]
        frame, rep, used_retry = None, ValidationReport(), False
        for start_day, is_retry in windows:
            if self._stop.is_set():
                raise _Stopped()
            start = datetime.combine(start_day, self.cal.market_open)
            records = self.client.fetch_candles(req, start, end)
            frame, rep = clean_candles(records_to_frame(records), self.cal, TIMEFRAME)
            used_retry = is_retry
            if len(frame):
                break
            if not is_retry:
                log.info("%s: no data for %s..%s, retrying last %d days", c.label, start_day, c.expiry,
                         self.p.retry_last_days)
        return FetchResult(c, frame, rep, used_retry)

    def _persist(self, r: FetchResult, summary: OptionsRunSummary) -> None:
        c = r.contract
        if r.used_retry:
            summary.retried += 1
        if r.error:
            summary.failed += 1
            self.store.record_contract_status(c, TIMEFRAME, "failed", self.store.option_row_count(c), detail=r.error)
            log.error("%s: FAILED %s", c.label, r.error)
            return
        if r.frame.empty:
            summary.no_data += 1
            self.store.record_contract_status(c, TIMEFRAME, "no_data", self.store.option_row_count(c),
                                              used_retry=r.used_retry, detail=r.report.summary())
            log.warning("%s: no data (after retry)", c.label)
            return
        out = r.frame.assign(underlying=c.underlying, exchange=c.exchange, expiry=c.expiry, strike=c.strike,
                             option_right=c.right, timeframe=TIMEFRAME)
        out["volume"] = out["volume"].astype("Int64")
        out["open_interest"] = out["open_interest"].astype("Int64")
        summary.rows_written += self.store.upsert_option_candles(out)
        summary.downloaded += 1
        days = r.report.bars_per_day
        self.store.record_contract_status(c, TIMEFRAME, "complete", self.store.option_row_count(c),
                                          first_ts=r.frame["ts"].min(), last_ts=r.frame["ts"].max(),
                                          used_retry=r.used_retry,
                                          detail=None if not r.report.dropped else r.report.summary())
        log.info("%s: %d rows over %d days%s", c.label, len(r.frame), len(days),
                 " (retry window)" if r.used_retry else "")

    # -- main loop -------------------------------------------------------------
    def contracts_to_fetch(self, plan: ExpiryPlan, retry_no_data: bool = False) -> tuple[list[OptionContract], int]:
        statuses = self.store.contract_statuses(self.p.underlying, self.p.exchange, plan.expiry, TIMEFRAME)
        todo, skipped = [], 0
        for strike in plan.strikes:
            for right in RIGHTS:
                status, rows = statuses.get((float(strike), right), (None, 0))
                if status == "complete" and rows > 0:
                    skipped += 1
                    continue
                if status == "no_data" and not retry_no_data:
                    skipped += 1
                    continue
                todo.append(OptionContract(self.p.underlying, self.p.exchange, plan.expiry, strike, right))
        return todo, skipped

    def run(self, start: date | None = None, end: date | None = None, retry_no_data: bool = False,
            forward_fill: bool | None = None, dry_run: bool = False) -> OptionsRunSummary:
        summary = OptionsRunSummary(self.p.name)
        expiries = self.expiries(start, end)
        summary.expiries = expiries
        if not expiries:
            log.warning("%s: no expiries between %s and %s", self.p.name, start or self.p.expiry_start,
                        end or self.p.expiry_end)
            return summary
        log.info("%s options: %d expiries %s", self.p.name, len(expiries), ", ".join(map(str, expiries)))
        calls_before = self.client.budget.used_this_run if self.client else 0

        try:
            if not dry_run:
                self.ensure_spot(expiries)
            for expiry in expiries:
                plan = self.plan_expiry(expiry)
                if plan is None:
                    continue
                todo, skipped = self.contracts_to_fetch(plan, retry_no_data)
                summary.planned_contracts += len(plan.strikes) * 2
                summary.skipped_existing += skipped
                log.info("%s %s: %d contracts planned, %d already done, %d to fetch",
                         self.p.name, expiry, len(plan.strikes) * 2, skipped, len(todo))
                if dry_run or not todo:
                    continue
                self._run_contracts(todo, summary)
                if summary.stopped_reason:
                    break
        except ApiLimitReached as exc:
            summary.stopped_reason = str(exc)
        except SessionExpiredError as exc:
            summary.stopped_reason = str(exc)

        if forward_fill if forward_fill is not None else self.p.forward_fill:
            summary.synthetic_rows = self.forward_fill(expiries)
        if self.client:
            summary.api_calls = self.client.budget.used_this_run - calls_before
        if summary.stopped_reason:
            log.warning("Stopped: %s. Everything downloaded so far is saved; re-run to continue.",
                        summary.stopped_reason)
        return summary

    def _run_contracts(self, todo: list[OptionContract], summary: OptionsRunSummary) -> None:
        self._stop.clear()
        pending: set[Future] = set()
        queue = list(todo)
        with ThreadPoolExecutor(max_workers=self.p.max_threads, thread_name_prefix="opt") as pool:
            try:
                while queue or pending:
                    while queue and len(pending) < self.p.max_threads and not self._stop.is_set():
                        pending.add(pool.submit(self.fetch_contract, queue.pop(0)))
                    if not pending:
                        break
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in done:
                        self._handle(fut, summary)
            except KeyboardInterrupt:
                self._stop.set()
                summary.stopped_reason = "interrupted (Ctrl+C)"
                log.warning("Interrupted: finishing in-flight contracts, then saving progress")
                for fut in pending:
                    self._handle(fut, summary)
                raise

    def _handle(self, fut: Future, summary: OptionsRunSummary) -> None:
        try:
            result = fut.result()
        except _Stopped:
            return
        except ApiLimitReached as exc:
            self._stop.set()
            summary.stopped_reason = str(exc)
            return
        except SessionExpiredError as exc:
            self._stop.set()
            summary.stopped_reason = str(exc)
            return
        self._persist(result, summary)

    # -- optional legacy forward fill --------------------------------------------
    def forward_fill(self, expiries: list[date]) -> int:
        """Opt-in legacy fill of missing minutes. Writes ONLY to option_candles_synthetic."""
        total = 0
        for expiry in expiries:
            df = self.store.query_df(
                "SELECT * FROM option_candles WHERE underlying = ? AND exchange = ? AND expiry = ? AND timeframe = ?",
                [self.p.underlying, self.p.exchange, expiry, TIMEFRAME])
            if df.empty:
                continue
            parts = []
            for (strike, right, d), day in df.groupby([df["strike"], df["option_right"], df["ts"].dt.date]):
                gen = forward_fill_day(day[["ts", "open", "high", "low", "close", "volume", "open_interest"]], self.cal, d)
                if len(gen):
                    parts.append(gen.assign(underlying=self.p.underlying, exchange=self.p.exchange, expiry=expiry,
                                            strike=float(strike), option_right=right, timeframe=TIMEFRAME))
            if parts:
                out = pd.concat(parts, ignore_index=True)
                out["volume"] = out["volume"].astype("Int64")
                out["open_interest"] = out["open_interest"].astype("Int64")
                total += self.store.upsert_synthetic_option_candles(out)
        log.info("%s: forward-fill generated %d synthetic rows (stored separately, flagged is_synthetic)",
                 self.p.name, total)
        return total


class _Stopped(Exception):
    pass
