"""Trading calendar and expiry generation.

All holiday / weekend / session-hour knowledge lives here. Nothing else in the
code base checks weekdays or holiday lists directly.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta

from .config import CalendarSettings, ExpiryRule

TIMEFRAME_MINUTES = {"1second": 1 / 60, "1minute": 1, "5minute": 5, "30minute": 30}


def _as_date(d) -> date:
    if isinstance(d, str):
        return datetime.strptime(d, "%Y-%m-%d").date()
    if isinstance(d, datetime):
        return d.date()
    return d


class TradingCalendar:
    def __init__(self, settings: CalendarSettings):
        self.name = settings.name
        self.market_open: time = settings.market_open
        self.market_close: time = settings.market_close   # start time of the last bar
        self.holidays: frozenset[date] = settings.holidays
        self.special_sessions: dict[date, tuple[time, time]] = dict(settings.special_sessions)

    # -- days ---------------------------------------------------------------
    def is_holiday(self, d) -> bool:
        return _as_date(d) in self.holidays

    def is_trading_day(self, d) -> bool:
        """Mon-Fri and not a listed exchange holiday (legacy is_trading_day), or a special session."""
        d = _as_date(d)
        return (d.weekday() < 5 and d not in self.holidays) or d in self.special_sessions

    def is_special_session(self, d) -> bool:
        return _as_date(d) in self.special_sessions

    def trading_days(self, start, end) -> list[date]:
        start, end = _as_date(start), _as_date(end)
        out, d = [], start
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def previous_trading_day(self, d, include_self: bool = False) -> date:
        d = _as_date(d)
        if not include_self:
            d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def next_trading_day(self, d, include_self: bool = False) -> date:
        d = _as_date(d)
        if not include_self:
            d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    # -- session ------------------------------------------------------------
    def session(self, d) -> tuple[time, time]:
        """(open, last-bar time) for the day: the special session if one is configured."""
        return self.special_sessions.get(_as_date(d), (self.market_open, self.market_close))

    def in_session(self, ts: datetime) -> bool:
        open_, close = self.session(ts.date())
        return open_ <= ts.time() <= close

    def session_bounds(self, d) -> tuple[datetime, datetime]:
        d = _as_date(d)
        open_, close = self.session(d)
        return datetime.combine(d, open_), datetime.combine(d, close)

    def expected_bars(self, timeframe: str, d=None) -> int:
        if timeframe == "1day":
            return 1
        minutes = TIMEFRAME_MINUTES[timeframe]
        open_, close = self.session(d) if d is not None else (self.market_open, self.market_close)
        start = datetime.combine(date.min, open_)
        end = datetime.combine(date.min, close)
        return int(((end - start).total_seconds() / 60) // minutes) + 1

    # -- expiries -----------------------------------------------------------
    def adjust_expiry(self, d) -> date:
        """Holiday expiry moves back to the previous trading day (legacy behaviour)."""
        d = _as_date(d)
        if d in self.holidays:
            return self.previous_trading_day(d)
        return d

    def expiries(self, rules: Iterable[ExpiryRule], start, end) -> list[date]:
        return generate_expiries(rules, start, end, self)


# ---------------------------------------------------------------------------
# Expiry generation
# ---------------------------------------------------------------------------
def _rule_applies(rule: ExpiryRule, d: date) -> bool:
    return (rule.start is None or d >= rule.start) and (rule.until is None or d <= rule.until)


def _next_weekday(current: date, weekday: int) -> date:
    return current + timedelta(days=(weekday - current.weekday()) % 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _next_candidate(rule: ExpiryRule, current: date) -> date:
    if rule.frequency == "weekly":
        return _next_weekday(current, rule.weekday)
    d = _last_weekday_of_month(current.year, current.month, rule.weekday)
    if d < current:
        y, m = (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
        d = _last_weekday_of_month(y, m, rule.weekday)
    return d


def _iter_unadjusted(rules: tuple[ExpiryRule, ...], start: date, end: date) -> Iterator[date]:
    """Yield unadjusted scheduled expiries >= start in order.

    Generalises the legacy loop: from `current`, compute the next occurrence
    under every rule, keep candidates that fall inside their rule's validity
    window, take the earliest, then continue from the day after it.
    """
    current = start
    horizon = end + timedelta(days=40)  # enough to find the next monthly candidate
    while current <= end:
        candidates = []
        for rule in rules:
            c = _next_candidate(rule, current)
            # A monthly rule may have its next valid date in a later month.
            while not _rule_applies(rule, c) and c <= horizon and (rule.until is None or c <= rule.until):
                c = _next_candidate(rule, c + timedelta(days=1))
            if _rule_applies(rule, c):
                candidates.append(c)
        if not candidates:
            return
        original = min(candidates)
        if original > end:
            return
        yield original
        current = original + timedelta(days=1)


def generate_expiries(rules, start, end, calendar: TradingCalendar) -> list[date]:
    """Expiry dates in [start, end] after holiday adjustment, sorted and de-duplicated.

    Mirrors legacy get_weekly_expiries(): the loop walks unadjusted dates up to
    `end`; an adjusted (earlier) date is kept only if it is still >= start.
    """
    start, end = _as_date(start), _as_date(end)
    out: set[date] = set()
    for original in _iter_unadjusted(tuple(rules), start, end):
        exp = calendar.adjust_expiry(original)
        if exp >= start:
            out.add(exp)
    return sorted(out)


def previous_expiry(rules, expiry: date, calendar: TradingCalendar, lookback_days: int = 70) -> date | None:
    """The expiry immediately before `expiry` under the same rules (used as entry/ATM day)."""
    expiry = _as_date(expiry)
    earlier = [e for e in generate_expiries(rules, expiry - timedelta(days=lookback_days), expiry, calendar) if e < expiry]
    return earlier[-1] if earlier else None
