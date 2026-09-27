"""Generic underlying / index candle downloader.

One implementation for every instrument in [instruments.*]. Used by both the
initial history download and the daily updater: both boil down to "find the
trading days in a range that DuckDB does not hold completely, then fetch them".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..breeze.client import ApiLimitReached, BreezeClient, BreezeError, HistoricalRequest, SessionExpiredError
from ..breeze.session_store import now_ist
from ..calendar import TradingCalendar
from ..config import Instrument, Settings
from ..log import get_logger
from ..storage import CandleStore
from ..validation import classify_day, clean_candles, records_to_frame

log = get_logger("market")


@dataclass
class DownloadSummary:
    instrument: str
    timeframe: str
    start: date
    end: date
    trading_days: int = 0
    already_complete: int = 0
    skipped_max_attempts: list[date] = field(default_factory=list)
    requested_days: list[date] = field(default_factory=list)
    complete: list[date] = field(default_factory=list)
    incomplete: list[tuple[date, int]] = field(default_factory=list)
    empty: list[date] = field(default_factory=list)
    rows_written: int = 0          # rows upserted (new + refreshed)
    rows_inserted: int = 0         # rows that were not in DuckDB before
    rows_downloaded: int = 0       # rows returned by Breeze (before validation)
    batches_total: int = 0
    batches_done: int = 0
    failed_batches: list[tuple[date, date, str]] = field(default_factory=list)
    api_calls: int = 0
    existing_first: date | None = None
    existing_last: date | None = None
    stopped_reason: str | None = None
    _last_inserted: int = 0

    def line(self) -> str:
        s = (f"{self.instrument} {self.timeframe} {self.start}..{self.end}: {self.trading_days} trading days, "
             f"{self.already_complete} already complete, {len(self.requested_days)} requested -> "
             f"{len(self.complete)} complete, {len(self.incomplete)} incomplete, {len(self.empty)} empty, "
             f"{self.rows_inserted} rows inserted, {len(self.failed_batches)} failed batches, "
             f"{self.api_calls} API calls")
        if self.skipped_max_attempts:
            s += f", {len(self.skipped_max_attempts)} skipped (max attempts)"
        if self.stopped_reason:
            s += f" [STOPPED: {self.stopped_reason}]"
        return s


def last_complete_session(cal: TradingCalendar, complete_after, now: datetime | None = None) -> date:
    """Latest trading day whose session has finished (today only after `complete_after` IST)."""
    now = now or now_ist()
    today = now.date()
    if cal.is_trading_day(today) and now.time() >= complete_after:
        return today
    return cal.previous_trading_day(today)


def resolve_end_date(end_setting: str | date | None, cal: TradingCalendar, complete_after,
                     now: datetime | None = None) -> date:
    """'today' (or None) -> last finished session; an explicit date is capped at the last finished session."""
    latest = last_complete_session(cal, complete_after, now)
    if end_setting is None or (isinstance(end_setting, str) and end_setting.lower() == "today"):
        return latest
    end = end_setting if isinstance(end_setting, date) else date.fromisoformat(str(end_setting))
    return min(end, latest)


def group_runs(days: list[date], cal: TradingCalendar, max_span_days: int) -> list[list[date]]:
    """Group sorted days into runs of consecutive trading days spanning <= max_span_days calendar days."""
    runs: list[list[date]] = []
    for d in sorted(days):
        if runs:
            run = runs[-1]
            contiguous = cal.next_trading_day(run[-1]) == d
            if contiguous and (d - run[0]).days < max_span_days:
                run.append(d)
                continue
        runs.append([d])
    return runs


class MarketDownloader:
    def __init__(self, settings: Settings, store: CandleStore, client: BreezeClient | None):
        self.settings = settings
        self.store = store
        self.client = client
        self.md = settings.market_data

    def calendar_for(self, inst: Instrument) -> TradingCalendar:
        return TradingCalendar(self.settings.calendars[inst.calendar])

    def thresholds(self, cal: TradingCalendar, timeframe: str, d: date | None = None) -> tuple[int, int]:
        """(expected, minimum) bars for a day. Special sessions scale the 375/370 rule to their length."""
        md = self.md
        if timeframe == "1minute" and (d is None or not cal.is_special_session(d)):
            return md.expected_bars_per_day, md.min_bars_threshold
        expected = cal.expected_bars(timeframe, d)
        return expected, max(1, int(expected * md.min_bars_threshold / md.expected_bars_per_day))

    def resolve_end(self, inst: Instrument, end: date | str | None = None) -> date:
        return resolve_end_date(end if end is not None else self.md.end_date, self.calendar_for(inst),
                                self.md.day_complete_after)

    # -- planning -------------------------------------------------------------
    def missing_days(self, inst: Instrument, start: date, end: date, timeframe: str,
                     force: bool = False) -> tuple[list[date], list[date], int, int]:
        """(days to fetch, days skipped after max attempts, trading days, already complete).

        DuckDB is the source of truth: every trading day in [start, end] is checked
        against the bars actually stored, so gaps in the middle are found as well as
        new days at the end. market_day_status only limits how often a day that keeps
        coming back empty/incomplete is re-requested.
        """
        cal = self.calendar_for(inst)
        days = cal.trading_days(start, end)
        counts = self.store.market_bar_counts(inst.name, inst.exchange, timeframe, start, end)
        attempts = self.store.day_attempts(inst.name, inst.exchange, timeframe)
        todo, skipped, complete = [], [], 0
        for d in days:
            _, minimum = self.thresholds(cal, timeframe, d)
            if counts.get(d, 0) >= minimum:
                complete += 1
                continue
            status, n = attempts.get(d, (None, 0))
            if not force and status in ("empty", "incomplete") and n >= self.md.max_attempts_per_day:
                skipped.append(d)
                continue
            todo.append(d)
        return todo, skipped, len(days), complete

    # -- download -------------------------------------------------------------
    def download(self, instrument: str, start: date | None = None, end: date | str | None = None,
                 timeframe: str | None = None, force: bool = False, dry_run: bool = False) -> DownloadSummary:
        inst = self.settings.instrument(instrument)
        timeframe = timeframe or self.md.timeframe
        cal = self.calendar_for(inst)
        start = start or self.md.start_date
        end = self.resolve_end(inst, end)
        summary = DownloadSummary(inst.name, timeframe, start, end)
        cov = self.store.market_coverage(inst.name, inst.exchange, timeframe, start, end)
        summary.existing_first, summary.existing_last = cov
        if end < start:
            log.info("%s: nothing to do (range ends before it starts)", inst.name)
            return summary
        if not inst.verified:
            log.warning("%s: stock_code %r is not verified; check it if no data comes back", inst.name, inst.stock_code)

        todo, skipped, n_days, complete = self.missing_days(inst, start, end, timeframe, force)
        summary.trading_days, summary.already_complete = n_days, complete
        summary.skipped_max_attempts = skipped
        summary.requested_days = todo
        runs = group_runs(todo, cal, self.md.chunk_days)
        summary.batches_total = len(runs)
        log.info("%s %s %s..%s: %d trading days, %d already complete in DuckDB, %d to download in %d batches%s",
                 inst.name, timeframe, start, end, n_days, complete, len(todo), len(runs),
                 f", {len(skipped)} skipped after {self.md.max_attempts_per_day} attempts" if skipped else "")
        if dry_run or not todo:
            return summary

        req = HistoricalRequest(inst.stock_code, inst.exchange, inst.product_type, interval=timeframe)
        calls_at_start = self.client.budget.used_this_run
        for i, run in enumerate(runs, 1):
            if timeframe == "1day":
                w_start = datetime.combine(run[0], datetime.min.time())
                w_end = datetime.combine(run[-1], datetime.max.time().replace(microsecond=0))
            else:
                w_start, w_end = cal.session_bounds(run[0])[0], cal.session_bounds(run[-1])[1]
            try:
                records = self.client.fetch_candles(req, w_start, w_end)
            except (ApiLimitReached, SessionExpiredError) as exc:
                summary.stopped_reason = str(exc)
                log.warning("%s: %s. Everything stored so far is kept; re-run the same command to continue.",
                            inst.name, exc)
                break
            except BreezeError as exc:
                # Permanent failure for this batch after retries: record it and carry on with the others.
                summary.failed_batches.append((run[0], run[-1], str(exc)[:300]))
                for d in run:
                    self.store.record_day_status(inst.name, inst.exchange, timeframe, d, "failed", 0, str(exc)[:300])
                log.error("%s batch %d/%d %s..%s FAILED: %s", inst.name, i, len(runs), run[0], run[-1], exc)
                continue
            summary.rows_downloaded += len(records)
            self._store_run(inst, cal, timeframe, run, records, summary)
            summary.batches_done += 1
            summary.api_calls = self.client.budget.used_this_run - calls_at_start
            log.info("%s batch %d/%d %s..%s | downloaded %d | inserted %d (total %d) | API calls %d (today %d/%d) "
                     "| failed batches %d", inst.name, i, len(runs), run[0], run[-1], len(records),
                     summary._last_inserted, summary.rows_inserted, summary.api_calls,
                     self.client.budget.used_today(), self.client.budget.daily_limit, len(summary.failed_batches))

        summary.api_calls = self.client.budget.used_this_run - calls_at_start
        log.info(summary.line())
        return summary

    def _store_run(self, inst: Instrument, cal: TradingCalendar, timeframe: str, run: list[date],
                   records: list[dict], summary: DownloadSummary) -> None:
        """Validate one batch and commit it in a single transaction, then re-derive day status from DuckDB."""
        frame, rep = clean_candles(records_to_frame(records), cal, timeframe)
        if rep.dropped or rep.reordered:
            log.info("%s %s..%s validation: %s%s", inst.name, run[0], run[-1], rep.summary(),
                     ", re-sorted" if rep.reordered else "")
        wanted = set(run)
        frame = frame[frame["ts"].dt.date.isin(wanted)] if len(frame) else frame
        before = sum(self.store.market_bar_counts(inst.name, inst.exchange, timeframe, run[0], run[-1]).values())
        if len(frame):
            out = frame.assign(instrument=inst.name, exchange=inst.exchange, timeframe=timeframe)
            out["volume"] = out["volume"].astype("Int64")
            out["open_interest"] = out["open_interest"].astype("Int64")
            summary.rows_written += self.store.upsert_market_candles(out)

        counts = self.store.market_bar_counts(inst.name, inst.exchange, timeframe, run[0], run[-1])
        summary._last_inserted = sum(counts.values()) - before
        summary.rows_inserted += summary._last_inserted
        for d in run:
            n = counts.get(d, 0)
            expected, minimum = self.thresholds(cal, timeframe, d)
            status = classify_day(n, expected, minimum)
            detail = None if status == "complete" else f"{n}/{expected} bars"
            self.store.record_day_status(inst.name, inst.exchange, timeframe, d, status, n, detail)
            if status == "complete":
                summary.complete.append(d)
            elif status == "incomplete":
                summary.incomplete.append((d, n))
                log.warning("%s %s incomplete: %d/%d bars (minimum %d)", inst.name, d, n, expected, minimum)
            else:
                summary.empty.append(d)
                log.warning("%s %s: no data returned", inst.name, d)
