"""Candle normalisation, cleaning and validation (pure pandas, no I/O).

Cleaning keeps the legacy rules (drop duplicate timestamps, weekends and
out-of-session bars) and adds holiday / numeric checks. It never invents bars:
incomplete days are reported, not filled. The one explicit exception is
`forward_fill_day`, used only by the opt-in options forward-fill step, whose
output is stored separately and flagged as synthetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from .calendar import TradingCalendar

PRICE_COLS = ["open", "high", "low", "close"]


@dataclass
class ValidationReport:
    input_rows: int = 0
    kept_rows: int = 0
    missing_datetime: int = 0
    malformed_datetime: int = 0
    invalid_numeric: int = 0
    duplicate_timestamps: int = 0
    weekend_rows: int = 0
    holiday_rows: int = 0
    outside_session_rows: int = 0
    reordered: bool = False       # input was not in chronological order (it is sorted before storing)
    bars_per_day: dict[date, int] = field(default_factory=dict)

    @property
    def dropped(self) -> int:
        return self.input_rows - self.kept_rows

    def summary(self) -> str:
        parts = [f"{self.kept_rows}/{self.input_rows} rows kept"]
        for name in ("missing_datetime", "malformed_datetime", "invalid_numeric", "duplicate_timestamps",
                     "weekend_rows", "holiday_rows", "outside_session_rows"):
            v = getattr(self, name)
            if v:
                parts.append(f"{name}={v}")
        return ", ".join(parts)


def records_to_frame(records: list[dict]) -> pd.DataFrame:
    """Breeze `Success` list -> DataFrame with ts/open/high/low/close/volume/open_interest."""
    df = pd.DataFrame.from_records(records or [])
    for col in ("datetime", *PRICE_COLS, "volume", "open_interest"):
        if col not in df.columns:
            df[col] = np.nan
    out = pd.DataFrame({
        "raw_datetime": df["datetime"],
        "ts": pd.to_datetime(df["datetime"], errors="coerce", format="mixed"),
    })
    for col in (*PRICE_COLS, "volume", "open_interest"):
        out[col] = pd.to_numeric(df[col], errors="coerce")
    return out


def clean_candles(df: pd.DataFrame, calendar: TradingCalendar, timeframe: str) -> tuple[pd.DataFrame, ValidationReport]:
    """Apply every validation rule; return only rows safe to store plus a report."""
    rep = ValidationReport(input_rows=len(df))
    if df.empty:
        return df.assign(ts=pd.Series(dtype="datetime64[ns]")).iloc[0:0], rep

    raw = df["raw_datetime"] if "raw_datetime" in df.columns else df["ts"]
    missing = raw.isna() | (raw.astype(str).str.strip() == "")
    malformed = df["ts"].isna() & ~missing
    rep.missing_datetime = int(missing.sum())
    rep.malformed_datetime = int(malformed.sum())
    df = df[~(missing | malformed)]

    prices = df[PRICE_COLS]
    bad = prices.isna().any(axis=1) | (prices <= 0).any(axis=1) | ~np.isfinite(prices).all(axis=1)
    bad |= df["high"] < df[["open", "close", "low"]].max(axis=1)
    bad |= df["low"] > df[["open", "close", "high"]].min(axis=1)
    bad |= df["volume"].fillna(0) < 0
    rep.invalid_numeric = int(bad.sum())
    df = df[~bad]

    dup = df.duplicated(subset="ts", keep="last")
    rep.duplicate_timestamps = int(dup.sum())
    df = df[~dup]

    days = df["ts"].dt.date
    special = days.map(calendar.is_special_session).astype(bool) if calendar.special_sessions \
        else pd.Series(False, index=df.index)
    weekend = (df["ts"].dt.weekday >= 5) & ~special
    holiday = days.map(calendar.is_holiday).astype(bool) & ~weekend & ~special
    rep.weekend_rows = int(weekend.sum())
    rep.holiday_rows = int(holiday.sum())
    df = df[~(weekend | holiday)]

    if timeframe != "1day":
        t = df["ts"].dt.time
        if calendar.special_sessions:
            bounds = df["ts"].dt.date.map(calendar.session)
            outside = pd.Series([not (o <= x <= c) for x, (o, c) in zip(t, bounds)], index=df.index, dtype=bool)
        else:
            outside = (t < calendar.market_open) | (t > calendar.market_close)
        rep.outside_session_rows = int(outside.sum())
        df = df[~outside]

    rep.reordered = bool(len(df)) and not df["ts"].is_monotonic_increasing
    df = df.sort_values("ts").drop(columns=["raw_datetime"], errors="ignore").reset_index(drop=True)
    rep.kept_rows = len(df)
    rep.bars_per_day = df.groupby(df["ts"].dt.date).size().to_dict() if len(df) else {}
    return df, rep


def classify_day(bar_count: int, expected: int, minimum: int) -> str:
    """complete | incomplete | empty, from the bar count of one day."""
    if bar_count <= 0:
        return "empty"
    return "complete" if bar_count >= minimum else "incomplete"


def min_bars_for(timeframe: str, expected_1m: int, min_1m: int, expected: int) -> int:
    """Scale the configured 1-minute threshold (e.g. 370/375) to other timeframes."""
    if timeframe == "1minute":
        return min_1m
    return max(1, int(expected * min_1m / expected_1m))


def forward_fill_day(day_df: pd.DataFrame, calendar: TradingCalendar, d: date) -> pd.DataFrame:
    """Legacy forward_fill_file(): rows for missing minutes of one day.

    Returns ONLY the generated rows (never modifies actual data). Minutes before
    the first real bar are not back-filled. Generated bars repeat the previous
    close as O/H/L/C with zero volume and carry forward open interest.
    """
    if day_df.empty:
        return day_df.iloc[0:0]
    start, end = calendar.session_bounds(d)
    grid = pd.date_range(start, end, freq="1min")
    real = day_df.set_index("ts").sort_index()
    full = real.reindex(grid)
    generated_mask = full["close"].isna()
    full["close"] = full["close"].ffill()
    for col in ("open", "high", "low"):
        full[col] = full[col].fillna(full["close"])
    full["volume"] = full["volume"].where(~generated_mask, 0)
    full["open_interest"] = full["open_interest"].ffill()
    gen = full[generated_mask & full["close"].notna()].copy()
    gen.index.name = "ts"
    return gen.reset_index()[["ts", *PRICE_COLS, "volume", "open_interest"]]
