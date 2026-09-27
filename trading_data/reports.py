"""Data-quality report built only from what is in DuckDB."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .breeze.session_store import now_ist, today_ist
from .calendar import TradingCalendar, generate_expiries
from .config import Settings
from .downloaders.market import resolve_end_date
from .storage import CandleStore
from .validation import min_bars_for


@dataclass
class MarketQuality:
    instrument: str
    exchange: str
    timeframe: str
    start: str
    end: str
    earliest_ts: str | None
    latest_ts: str | None
    trading_days_requested: int
    trading_days_downloaded: int
    missing_days: list[str]
    incomplete_days: list[str]
    duplicate_candles: int
    out_of_session_rows: int
    non_trading_day_rows: int
    total_rows: int
    failed_days: list[str] = field(default_factory=list)
    first_day: dict | None = None
    latest_day: dict | None = None


@dataclass
class OptionsQuality:
    profile: str
    underlying: str
    exchange: str
    expiries: list[str]
    contracts_planned: int
    contracts_complete: int
    contracts_no_data: int
    contracts_failed: int
    contracts_not_attempted: int
    missing_ce_pe_pairs: list[dict]
    entry_day_failures: list[dict]
    duplicate_candles: int
    total_rows: int
    synthetic_rows: int


@dataclass
class QualityReport:
    generated_at: str
    api_calls_today: int
    api_daily_limit: int
    market: list[MarketQuality] = field(default_factory=list)
    options: list[OptionsQuality] = field(default_factory=list)


def market_quality(settings: Settings, store: CandleStore, instrument: str, start: date | None = None,
                   end: date | None = None, timeframe: str | None = None) -> MarketQuality:
    inst = settings.instrument(instrument)
    md = settings.market_data
    timeframe = timeframe or md.timeframe
    cal = TradingCalendar(settings.calendars[inst.calendar])
    start = start or md.start_date
    end = resolve_end_date(end or md.end_date, cal, md.day_complete_after)
    days = cal.trading_days(start, end)
    per_day = store.market_day_summaries(inst.name, inst.exchange, timeframe, start, end)
    counts = {pd_date(r.trade_date): int(r.candles) for r in per_day.itertuples()}

    def minimum_for(d):
        if timeframe == "1minute" and not cal.is_special_session(d):
            return md.min_bars_threshold
        exp = cal.expected_bars(timeframe, d)
        return max(1, int(exp * md.min_bars_threshold / md.expected_bars_per_day)) if timeframe == "1minute" \
            else min_bars_for(timeframe, md.expected_bars_per_day, md.min_bars_threshold, exp)

    missing = [d for d in days if counts.get(d, 0) == 0]
    incomplete = [d for d in days if 0 < counts.get(d, 0) < minimum_for(d)]
    params = [inst.name, inst.exchange, timeframe, start, end]
    where = "instrument = ? AND exchange = ? AND timeframe = ? AND ts >= ? AND ts < ? + INTERVAL 1 DAY"
    dup = store.query_df(f"SELECT count(*) AS n FROM (SELECT ts FROM market_candles WHERE {where} "
                         "GROUP BY ts HAVING count(*) > 1)", params)["n"].iloc[0]
    total = int(sum(counts.values()))
    oos = 0
    if timeframe != "1day":
        oos = sum(1 for r in per_day.itertuples()
                  if not (cal.in_session(r.first_ts.to_pydatetime()) and cal.in_session(r.last_ts.to_pydatetime())))
    stored_days = [d for d in counts if not cal.is_trading_day(d)]
    ntd = sum(counts[d] for d in stored_days)
    def day_info(row):
        return None if row is None else {"date": str(pd_date(row.trade_date)), "candles": int(row.candles),
                                         "first_ts": str(row.first_ts), "last_ts": str(row.last_ts)}

    rows = list(per_day.itertuples())
    return MarketQuality(
        inst.name, inst.exchange, timeframe, str(start), str(end),
        str(rows[0].first_ts) if rows else None, str(rows[-1].last_ts) if rows else None,
        len(days), len(days) - len(missing) - len(incomplete), [str(d) for d in missing],
        [f"{d} ({counts[d]} bars)" for d in incomplete], int(dup), int(oos), int(ntd), total,
        failed_days=[str(d) for d in store.failed_days(inst.name, inst.exchange, timeframe) if start <= d <= end and counts.get(d, 0) == 0],
        first_day=day_info(rows[0] if rows else None), latest_day=day_info(rows[-1] if rows else None))


def pd_date(x) -> date:
    if isinstance(x, datetime):          # includes pandas.Timestamp
        return x.date()
    return x


def options_quality(settings: Settings, store: CandleStore, profile_name: str,
                    start: date | None = None, end: date | None = None) -> OptionsQuality:
    p = settings.options_profile(profile_name)
    cal = TradingCalendar(settings.calendars[p.calendar])
    expiries = generate_expiries(p.expiry_rules, start or p.expiry_start, end or p.expiry_end, cal)
    planned = complete = no_data = failed = not_attempted = 0
    pairs: list[dict] = []
    entry: list[dict] = []
    total = synthetic = dup = 0
    for i, expiry in enumerate(expiries):
        plan = store.get_expiry_plan(p.underlying, p.exchange, expiry)
        statuses = store.contract_statuses(p.underlying, p.exchange, expiry)
        if plan:
            step = int(float(plan["strike_step"]))
            strikes = list(range(int(float(plan["strike_low"])), int(float(plan["strike_high"])) + step, step))
        else:
            strikes = sorted({int(s) for s, _ in statuses})
        planned += len(strikes) * 2
        for strike in strikes:
            have = {}
            for right in ("CALL", "PUT"):
                status, rows = statuses.get((float(strike), right), (None, 0))
                if status is None:
                    not_attempted += 1
                elif status == "complete" and rows > 0:
                    complete += 1
                elif status == "no_data":
                    no_data += 1
                else:
                    failed += 1
                have[right] = rows > 0
            if have["CALL"] != have["PUT"]:
                pairs.append({"expiry": str(expiry), "strike": strike,
                              "missing": "PUT" if have["CALL"] else "CALL"})

        # Entry-day check (legacy): from the second expiry on, the ATM CE/PE must
        # have data on the entry day (the ATM reference day, normally the previous expiry).
        if i > 0 and plan:
            atm, atm_day = float(plan["atm"]), plan["atm_date"]
            atm_day = atm_day.date() if hasattr(atm_day, "date") else atm_day
            df = store.query_df(
                "SELECT option_right, count(*) AS n FROM option_candles WHERE underlying = ? AND exchange = ? "
                "AND expiry = ? AND strike = ? AND CAST(ts AS DATE) = ? GROUP BY 1",
                [p.underlying, p.exchange, expiry, atm, atm_day])
            present = set(df["option_right"]) if len(df) else set()
            missing = [r for r in ("CALL", "PUT") if r not in present]
            if missing:
                entry.append({"expiry": str(expiry), "entry_day": str(atm_day), "atm": int(atm),
                              "missing": missing})
        elif i > 0 and not plan:
            entry.append({"expiry": str(expiry), "entry_day": None, "atm": None, "missing": ["NO PLAN"]})

        total += int(store.query_df("SELECT count(*) AS n FROM option_candles WHERE underlying = ? AND exchange = ? "
                                    "AND expiry = ?", [p.underlying, p.exchange, expiry])["n"].iloc[0])
        synthetic += int(store.query_df("SELECT count(*) AS n FROM option_candles_synthetic WHERE underlying = ? "
                                        "AND exchange = ? AND expiry = ?", [p.underlying, p.exchange, expiry])["n"].iloc[0])
        dup += int(store.query_df(
            "SELECT count(*) AS n FROM (SELECT 1 FROM option_candles WHERE underlying = ? AND exchange = ? AND expiry = ? "
            "GROUP BY strike, option_right, timeframe, ts HAVING count(*) > 1)",
            [p.underlying, p.exchange, expiry])["n"].iloc[0])

    return OptionsQuality(p.name, p.underlying, p.exchange, [str(e) for e in expiries], planned, complete,
                          no_data, failed, not_attempted, pairs, entry, dup, total, synthetic)


def build_report(settings: Settings, store: CandleStore, instruments: list[str] | None = None,
                 option_profiles: list[str] | None = None, start: date | None = None,
                 end: date | str | None = None, timeframe: str | None = None) -> QualityReport:
    rep = QualityReport(generated_at=now_ist().isoformat(timespec="seconds"),
                        api_calls_today=store.api_calls_on(today_ist()), api_daily_limit=settings.api.daily_limit)
    for name in instruments if instruments is not None else settings.market_data.instruments:
        rep.market.append(market_quality(settings, store, name, start, end, timeframe))
    for name in option_profiles or []:
        rep.options.append(options_quality(settings, store, name))
    return rep


def render_text(rep: QualityReport, max_items: int = 15) -> str:
    def short(items):
        items = list(items)
        s = ", ".join(map(str, items[:max_items]))
        return s + (f" ... (+{len(items) - max_items} more)" if len(items) > max_items else "")

    lines = [f"DATA QUALITY REPORT  ({rep.generated_at} IST)",
             f"API calls used today: {rep.api_calls_today}/{rep.api_daily_limit}", ""]
    if rep.market:
        lines.append(f"MARKET DATA  ({len(rep.market)} instruments)")
    for m in rep.market:
        ok = not (m.missing_days or m.incomplete_days or m.duplicate_candles or m.out_of_session_rows or m.failed_days)
        lines += [
            f"  {m.instrument} [{m.exchange}, {m.timeframe}] {m.start} -> {m.end}  {'OK' if ok else 'NEEDS ATTENTION'}",
            f"    trading days requested {m.trading_days_requested}, downloaded (complete) {m.trading_days_downloaded}, "
            f"missing {len(m.missing_days)}, incomplete {len(m.incomplete_days)}",
            f"    stored {m.earliest_ts or '-'} -> {m.latest_ts or '-'}",
            f"    rows {m.total_rows}, duplicates {m.duplicate_candles}, days with out-of-session bars {m.out_of_session_rows}, "
            f"weekend/holiday rows {m.non_trading_day_rows}, failed-batch days {len(m.failed_days)}",
        ]
        if m.missing_days:
            lines.append(f"    missing: {short(m.missing_days)}")
        if m.incomplete_days:
            lines.append(f"    incomplete: {short(m.incomplete_days)}")
    for o in rep.options:
        lines += [
            "", f"OPTIONS  {o.profile} [{o.exchange}]  expiries: {short(o.expiries)}",
            f"    contracts planned {o.contracts_planned}: complete {o.contracts_complete}, no data {o.contracts_no_data}, "
            f"failed {o.contracts_failed}, not attempted {o.contracts_not_attempted}",
            f"    rows {o.total_rows} (+{o.synthetic_rows} synthetic, stored separately), duplicates {o.duplicate_candles}",
            f"    missing CE/PE pairs: {len(o.missing_ce_pe_pairs)}"
            + (f"  {short(f'{x['expiry']} {x['strike']} missing {x['missing']}' for x in o.missing_ce_pe_pairs)}"
               if o.missing_ce_pe_pairs else ""),
            f"    entry-day failures: {len(o.entry_day_failures)}"
            + (f"  {short(f'{x['expiry']} ATM {x['atm']} on {x['entry_day']} missing {'/'.join(x['missing'])}' for x in o.entry_day_failures)}"
               if o.entry_day_failures else ""),
        ]
    return "\n".join(lines)


def write_report(rep: QualityReport, report_dir: Path) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt, js = report_dir / f"data_quality_{stamp}.txt", report_dir / f"data_quality_{stamp}.json"
    txt.write_text(render_text(rep, max_items=10_000))
    js.write_text(json.dumps(asdict(rep), indent=2, default=str))
    return txt, js
