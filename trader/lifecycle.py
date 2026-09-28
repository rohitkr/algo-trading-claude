"""Trade lifecycle: the statuses a trade moves through and the only transitions allowed between them.

    DRAFT -> READY -> ENTRY_ORDER_PLACED -> ENTRY_PENDING -> ENTRY_EXECUTED -> POSITION_ACTIVE
          -> EXIT_ORDER_PLACED -> EXIT_PENDING -> EXITED (exit_reason: TARGET_HIT, STOP_LOSS_HIT,
             TRAILING_SL_HIT, AUTO_EXIT, SQUARE_OFF, USER_EXIT, DAILY_LIMIT)

Side exits: CANCELLED (entry never filled), REJECTED (entry rejected), MANUALLY_EXITED (the position was
closed outside this system, e.g. in Kite), ERROR, UNKNOWN_REQUIRES_RECONCILIATION (the broker shows
something this system cannot explain: it stops sending orders for that trade until a human resolves it).

A partially filled entry is managed as a position (SL order for the filled quantity) while the rest of
the entry order keeps working, so ENTRY_PENDING and POSITION_ACTIVE can both carry filled_qty > 0.
"""
from __future__ import annotations

DRAFT = "DRAFT"
READY = "READY"
ENTRY_ORDER_PLACED = "ENTRY_ORDER_PLACED"
ENTRY_PENDING = "ENTRY_PENDING"
ENTRY_EXECUTED = "ENTRY_EXECUTED"
POSITION_ACTIVE = "POSITION_ACTIVE"
EXIT_ORDER_PLACED = "EXIT_ORDER_PLACED"
EXIT_PENDING = "EXIT_PENDING"
EXITED = "EXITED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
MANUALLY_EXITED = "MANUALLY_EXITED"
ERROR = "ERROR"
UNKNOWN = "UNKNOWN_REQUIRES_RECONCILIATION"
EXPIRED = "EXPIRED"            # a DRAFT/READY trade that was never confirmed

# exit reasons
TARGET_HIT = "TARGET_HIT"
STOP_LOSS_HIT = "STOP_LOSS_HIT"
TRAILING_SL_HIT = "TRAILING_SL_HIT"
AUTO_EXIT = "AUTO_EXIT"
SQUARE_OFF = "SQUARE_OFF"
USER_EXIT = "USER_EXIT"
DAILY_LIMIT = "DAILY_LIMIT"
MANUAL_EXIT = "MANUAL_EXIT"    # exit_reason for MANUALLY_EXITED

TERMINAL = {EXITED, CANCELLED, REJECTED, MANUALLY_EXITED, EXPIRED}
UNCONFIRMED = {DRAFT, READY}
# Trades the monitor manages (orders may be working / a position may be open).
LIVE_STATUSES = {ENTRY_ORDER_PLACED, ENTRY_PENDING, ENTRY_EXECUTED, POSITION_ACTIVE, EXIT_ORDER_PLACED,
                 EXIT_PENDING}
# Trades still counted as open for risk limits and reconciliation (incl. ones needing a human).
OPEN_STATUSES = LIVE_STATUSES | {ERROR, UNKNOWN}

_T = {
    DRAFT: {READY, EXPIRED, CANCELLED},
    READY: {ENTRY_ORDER_PLACED, EXPIRED, CANCELLED, DRAFT},
    ENTRY_ORDER_PLACED: {ENTRY_PENDING, ENTRY_EXECUTED, POSITION_ACTIVE, CANCELLED, REJECTED, ERROR, UNKNOWN,
                         MANUALLY_EXITED, EXIT_ORDER_PLACED},
    ENTRY_PENDING: {ENTRY_EXECUTED, POSITION_ACTIVE, CANCELLED, REJECTED, ERROR, UNKNOWN, MANUALLY_EXITED,
                    EXIT_ORDER_PLACED, EXITED},
    ENTRY_EXECUTED: {POSITION_ACTIVE, EXIT_ORDER_PLACED, MANUALLY_EXITED, ERROR, UNKNOWN, EXITED},
    POSITION_ACTIVE: {EXIT_ORDER_PLACED, EXITED, MANUALLY_EXITED, ERROR, UNKNOWN},
    EXIT_ORDER_PLACED: {EXIT_PENDING, EXITED, POSITION_ACTIVE, MANUALLY_EXITED, ERROR, UNKNOWN},
    EXIT_PENDING: {EXITED, POSITION_ACTIVE, MANUALLY_EXITED, ERROR, UNKNOWN, EXIT_ORDER_PLACED},
    # A human resolves these (python3 -m trader resolve / the UI), or reconciliation proves what happened.
    ERROR: {POSITION_ACTIVE, ENTRY_PENDING, EXITED, MANUALLY_EXITED, CANCELLED, REJECTED, UNKNOWN},
    UNKNOWN: {POSITION_ACTIVE, ENTRY_PENDING, EXITED, MANUALLY_EXITED, CANCELLED, REJECTED, ERROR},
}


class IllegalTransition(RuntimeError):
    pass


def can_transition(old: str, new: str) -> bool:
    return old == new or new in _T.get(old, set())


def check_transition(old: str, new: str) -> None:
    if not can_transition(old, new):
        raise IllegalTransition(f"trade status {old} -> {new} is not allowed")


def direction(side: str) -> int:
    """+1 for a long (BUY entry), -1 for a short (SELL entry)."""
    return 1 if side == "BUY" else -1


def exit_side(side: str) -> str:
    return "SELL" if side == "BUY" else "BUY"
