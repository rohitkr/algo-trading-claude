"""Configuration loading.

Two sources, with a strict split:
  * config/settings.toml + config/holidays.toml  -> non-secret, versioned defaults
  * .env (never committed)                       -> credentials and per-machine overrides

Everything is exposed as frozen dataclasses so the rest of the code never
reads TOML or environment variables directly.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = REPO_ROOT / "config" / "settings.toml"
DEFAULT_HOLIDAYS = REPO_ROOT / "config" / "holidays.toml"
DEFAULT_ENV = REPO_ROOT / ".env"


class ConfigError(ValueError):
    pass


def _parse_date(value, name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: expected YYYY-MM-DD, got {value!r}") from exc


def _parse_time(value, name: str) -> time:
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: expected HH:MM, got {value!r}") from exc


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Credentials:
    user_id: str | None
    password: str | None
    api_key: str | None
    api_secret: str | None

    def require_api(self) -> None:
        missing = [n for n, v in (("BREEZE_API_KEY", self.api_key), ("BREEZE_API_SECRET", self.api_secret)) if not v]
        if missing:
            raise ConfigError(f"Missing in .env: {', '.join(missing)}")

    def require_login(self) -> None:
        self.require_api()
        missing = [n for n, v in (("ICICI_USER_ID", self.user_id), ("ICICI_PASSWORD", self.password)) if not v]
        if missing:
            raise ConfigError(f"Missing in .env: {', '.join(missing)}")

    def __repr__(self) -> str:  # never leak secrets through repr/logging
        return "Credentials(<redacted>)"


@dataclass(frozen=True)
class ApiSettings:
    daily_limit: int = 4900
    max_calls_per_minute: int = 100
    delay_seconds: float = 0.5
    max_rows_per_request: int = 1000
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    timeout_seconds: int = 30


@dataclass(frozen=True)
class CalendarSettings:
    name: str
    market_open: time
    market_close: time
    holidays: frozenset[date]
    # Special / partial sessions (e.g. Muhurat trading): date -> (open, last-bar time).
    # A special session makes the day a trading day even on a weekend or holiday.
    special_sessions: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Instrument:
    name: str           # logical name used everywhere in the DB, e.g. "BANKNIFTY"
    stock_code: str     # Breeze stock_code, e.g. "CNXBAN"
    exchange: str       # Breeze exchange_code, e.g. "NSE"
    product_type: str   # Breeze product_type, e.g. "cash"
    calendar: str
    verified: bool = True


@dataclass(frozen=True)
class MarketDataSettings:
    timeframe: str
    instruments: tuple[str, ...]
    start_date: date
    end_date: str                 # "today" (moving) or YYYY-MM-DD (fixed)
    expected_bars_per_day: int
    min_bars_threshold: int
    max_attempts_per_day: int
    chunk_days: int
    day_complete_after: time


@dataclass(frozen=True)
class ExpiryRule:
    frequency: str           # "weekly" | "monthly"
    weekday: int             # 0=Mon .. 6=Sun
    start: date | None = None
    until: date | None = None


@dataclass(frozen=True)
class OptionsProfile:
    name: str
    underlying: str          # key into instruments (spot data for ATM)
    stock_code: str
    exchange: str
    product_type: str
    calendar: str
    expiry_start: date
    expiry_end: date
    strike_step: int
    strikes_each_side: int
    days_before_expiry: int
    lot_size: int
    max_threads: int
    api_delay: float
    daily_api_limit: int
    dynamic_strike_buffer: int
    dynamic_strike_round: int
    retry_last_days: int
    atm_reference: str
    forward_fill: bool
    expiry_rules: tuple[ExpiryRule, ...]


@dataclass(frozen=True)
class Paths:
    database: Path
    session_file: Path
    log_dir: Path
    report_dir: Path


@dataclass(frozen=True)
class Settings:
    paths: Paths
    api: ApiSettings
    calendars: dict[str, CalendarSettings]
    market_data: MarketDataSettings
    instruments: dict[str, Instrument]
    options: dict[str, OptionsProfile]
    credentials: Credentials = field(repr=False)

    def instrument(self, name: str) -> Instrument:
        try:
            return self.instruments[name.upper()]
        except KeyError:
            raise ConfigError(f"Unknown instrument {name!r}. Configured: {', '.join(self.instruments)}") from None

    def options_profile(self, name: str) -> OptionsProfile:
        try:
            return self.options[name.upper()]
        except KeyError:
            raise ConfigError(f"Unknown options profile {name!r}. Configured: {', '.join(self.options)}") from None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _load_calendars(raw: dict, holidays_raw: dict) -> dict[str, CalendarSettings]:
    calendars = {}
    for name, c in raw.items():
        source = c.get("holidays_from", name)
        days = holidays_raw.get(source, {}).get("holidays", [])
        special = {}
        for d, sess in holidays_raw.get(source, {}).get("special_sessions", {}).items():
            special[_parse_date(d, f"holidays.{source}.special_sessions")] = (
                _parse_time(sess["open"], f"special_sessions.{d}.open"),
                _parse_time(sess["close"], f"special_sessions.{d}.close"))
        calendars[name] = CalendarSettings(
            special_sessions=special,
            name=name,
            market_open=_parse_time(c.get("market_open", "09:15"), f"calendars.{name}.market_open"),
            market_close=_parse_time(c.get("market_close", "15:29"), f"calendars.{name}.market_close"),
            holidays=frozenset(_parse_date(d, f"holidays.{source}") for d in days),
        )
    return calendars


def _load_expiry_rules(raw: list, name: str) -> tuple[ExpiryRule, ...]:
    rules = []
    for i, r in enumerate(raw or []):
        freq = str(r.get("frequency", "weekly")).lower()
        if freq not in ("weekly", "monthly"):
            raise ConfigError(f"options.{name}.expiry_rules[{i}].frequency must be weekly|monthly")
        wd = str(r.get("weekday", "")).upper()[:3]
        if wd not in _WEEKDAYS:
            raise ConfigError(f"options.{name}.expiry_rules[{i}].weekday must be MON..SUN")
        rules.append(ExpiryRule(
            frequency=freq,
            weekday=_WEEKDAYS[wd],
            start=_parse_date(r["from"], "from") if "from" in r else None,
            until=_parse_date(r["until"], "until") if "until" in r else None,
        ))
    if not rules:
        raise ConfigError(f"options.{name}.expiry_rules is empty")
    return tuple(rules)


def load_settings(
    settings_path: Path | str = DEFAULT_SETTINGS,
    holidays_path: Path | str = DEFAULT_HOLIDAYS,
    env_path: Path | str | None = DEFAULT_ENV,
    environ: dict[str, str] | None = None,
) -> Settings:
    """Load settings.toml + holidays.toml + .env into a Settings object.

    `environ` lets tests inject environment values without touching os.environ.
    """
    if environ is None:
        if env_path and Path(env_path).exists():
            load_dotenv(env_path, override=False)
        environ = dict(os.environ)

    def env(key: str) -> str | None:
        v = environ.get(key)
        return v.strip() if v and v.strip() else None

    with open(settings_path, "rb") as fh:
        raw = tomllib.load(fh)
    with open(holidays_path, "rb") as fh:
        holidays_raw = tomllib.load(fh)

    p = raw.get("paths", {})
    paths = Paths(
        database=REPO_ROOT / (env("DUCKDB_PATH") or p.get("database", "data/market_data.duckdb")),
        session_file=REPO_ROOT / p.get("session_file", "data/.breeze_session.json"),
        log_dir=REPO_ROOT / p.get("log_dir", "logs"),
        report_dir=REPO_ROOT / p.get("report_dir", "reports"),
    )

    a = raw.get("api", {})
    api = ApiSettings(**{k: a[k] for k in ApiSettings.__dataclass_fields__ if k in a})
    if env("BREEZE_DAILY_API_LIMIT"):
        api = ApiSettings(**{**api.__dict__, "daily_limit": int(env("BREEZE_DAILY_API_LIMIT"))})

    calendars = _load_calendars(raw.get("calendars", {}), holidays_raw)

    instruments = {}
    for name, i in raw.get("instruments", {}).items():
        cal = i.get("calendar", "NSE")
        if cal not in calendars:
            raise ConfigError(f"instruments.{name}.calendar {cal!r} is not defined")
        instruments[name.upper()] = Instrument(
            name=name.upper(), stock_code=i["stock_code"], exchange=i["exchange"].upper(),
            product_type=i.get("product_type", "cash"), calendar=cal, verified=bool(i.get("verified", True)),
        )

    m = raw.get("market_data", {})
    # .env MARKET_DATA_* values override [market_data] in settings.toml, so the
    # instrument / exchange / interval / date range can change without code edits.
    env_instruments = env("MARKET_DATA_INSTRUMENT")
    instrument_names = tuple(x.strip().upper() for x in env_instruments.split(",") if x.strip()) \
        if env_instruments else tuple(s.upper() for s in m.get("instruments", ["NIFTY"]))
    end_raw = (env("MARKET_DATA_END_DATE") or str(m.get("end_date", "today"))).strip()
    if end_raw.lower() != "today":
        _parse_date(end_raw, "MARKET_DATA_END_DATE / market_data.end_date")
    market = MarketDataSettings(
        timeframe=env("MARKET_DATA_INTERVAL") or m.get("timeframe", "1minute"),
        instruments=instrument_names,
        start_date=_parse_date(env("MARKET_DATA_START_DATE") or env("MARKET_START_DATE")
                               or m.get("start_date", "2025-01-01"), "MARKET_DATA_START_DATE / market_data.start_date"),
        end_date=end_raw.lower() if end_raw.lower() == "today" else end_raw,
        expected_bars_per_day=int(m.get("expected_bars_per_day", 375)),
        min_bars_threshold=int(m.get("min_bars_threshold", 370)),
        max_attempts_per_day=int(m.get("max_attempts_per_day", 3)),
        chunk_days=int(m.get("chunk_days", 7)),
        day_complete_after=_parse_time(m.get("day_complete_after", "15:45"), "market_data.day_complete_after"),
    )
    valid_intervals = ("1minute", "5minute", "30minute", "1day")
    if market.timeframe not in valid_intervals:
        raise ConfigError(f"MARKET_DATA_INTERVAL must be one of {', '.join(valid_intervals)}, got {market.timeframe!r}")

    env_exchange = (env("MARKET_DATA_EXCHANGE") or "").upper() or None
    env_stock_code = env("MARKET_DATA_STOCK_CODE")
    for name in market.instruments:
        if name in instruments:
            inst = instruments[name]
            if env_exchange and env_instruments and env_exchange != inst.exchange:
                raise ConfigError(
                    f"MARKET_DATA_EXCHANGE={env_exchange} does not match {name}, which trades on {inst.exchange} "
                    f"according to config/settings.toml")
            if env_stock_code and env_instruments and len(market.instruments) == 1:
                instruments[name] = Instrument(name, env_stock_code, inst.exchange, inst.product_type,
                                               inst.calendar, verified=False)
            continue
        # Not in the registry: allow a one-off instrument defined entirely in .env.
        if env_instruments and len(market.instruments) == 1 and env_stock_code and env_exchange:
            cal = env_exchange if env_exchange in calendars else "NSE"
            instruments[name] = Instrument(name, env_stock_code, env_exchange, "cash", cal, verified=False)
            continue
        raise ConfigError(
            f"Unknown instrument {name!r}. Add an [instruments.{name}] entry to config/settings.toml, or set "
            f"MARKET_DATA_STOCK_CODE and MARKET_DATA_EXCHANGE in .env. Configured: {', '.join(instruments)}")

    options = {}
    for name, o in raw.get("options", {}).items():
        name = name.upper()
        underlying = o.get("underlying", name).upper()
        if underlying not in instruments:
            raise ConfigError(f"options.{name}.underlying {underlying!r} is not a configured instrument")
        cal = o.get("calendar", instruments[underlying].calendar)
        if cal not in calendars:
            raise ConfigError(f"options.{name}.calendar {cal!r} is not defined")
        # EXPIRY_START / EXPIRY_END in .env override the expiry range of every profile.
        start = _parse_date(env("EXPIRY_START") or o["expiry_start"], f"options.{name}.expiry_start")
        end = _parse_date(env("EXPIRY_END") or o["expiry_end"], f"options.{name}.expiry_end")
        if end < start:
            raise ConfigError(f"options.{name}: expiry_end {end} is before expiry_start {start}")
        options[name] = OptionsProfile(
            name=name, underlying=underlying, stock_code=o.get("stock_code", instruments[underlying].stock_code),
            exchange=o["exchange"].upper(), product_type=o.get("product_type", "options"), calendar=cal,
            expiry_start=start, expiry_end=end,
            strike_step=int(o["strike_step"]), strikes_each_side=int(o["strikes_each_side"]),
            days_before_expiry=int(o["days_before_expiry"]), lot_size=int(o["lot_size"]),
            max_threads=max(1, int(o.get("max_threads", 3))), api_delay=float(o.get("api_delay", api.delay_seconds)),
            daily_api_limit=int(o.get("daily_api_limit", api.daily_limit)),
            dynamic_strike_buffer=int(o.get("dynamic_strike_buffer", 0)),
            dynamic_strike_round=int(o.get("dynamic_strike_round", 100)),
            retry_last_days=int(o.get("retry_last_days", 10)),
            atm_reference=o.get("atm_reference", "previous_expiry"),
            forward_fill=bool(o.get("forward_fill", False)),
            expiry_rules=_load_expiry_rules(o.get("expiry_rules"), name),
        )

    credentials = Credentials(
        user_id=env("ICICI_USER_ID"), password=env("ICICI_PASSWORD"),
        api_key=env("BREEZE_API_KEY"), api_secret=env("BREEZE_API_SECRET"),
    )
    return Settings(paths=paths, api=api, calendars=calendars, market_data=market,
                    instruments=instruments, options=options, credentials=credentials)
