"""
Who is in, and since when.

THE RULE THAT MAKES THIS WORK
-----------------------------
`users.on_duty` and the open shift are two statements about the same fact, so
nothing is allowed to set one without the other. Every path in and out of the
building goes through clock_in / clock_out here — the staff member's own "I'm
in" button, the baker's duty toggle, and the admin roster — and the one place
that previously wrote on_duty directly does so no longer.

Get that wrong and the roster says a baker has been in since nine while the
assignment engine thinks he is off duty and gives his cakes to somebody else.

STALE SHIFTS
------------
People forget to clock out. A shift left open is closed automatically once it
has run longer than MAX_SHIFT_HOURS, marked `closed_by="stale"` so the roster
can say the leaving time is a guess rather than presenting it as a reading.
"""

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.shift import Shift
from app.models.user import User

# Longer than any real shift, short enough that a forgotten clock-out is
# cleaned up before the next day's roster is read.
MAX_SHIFT_HOURS = 16


def _aware(dt: datetime | None) -> datetime | None:
    """
    Treat a naive timestamp as UTC.

    Postgres hands back an aware datetime from a timestamptz column; SQLite,
    which the tests run on, hands back a naive one. Comparing the two raises,
    so everything that does arithmetic on a shift goes through here first.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def open_shift(db: Session, user_id: int) -> Shift | None:
    """The shift this person is currently on, if any — stale ones excluded."""
    shift = (
        db.query(Shift)
        .filter(Shift.user_id == user_id, Shift.clocked_out_at.is_(None))
        .order_by(Shift.clocked_in_at.desc())
        .first()
    )
    if shift is None:
        return None
    if _is_stale(shift):
        _close(db, shift, closed_by="stale",
               at=_aware(shift.clocked_in_at) + timedelta(hours=MAX_SHIFT_HOURS))
        db.commit()
        return None
    return shift


def _is_stale(shift: Shift) -> bool:
    started = _aware(shift.clocked_in_at)
    if started is None:
        return False
    return datetime.now(timezone.utc) - started > timedelta(hours=MAX_SHIFT_HOURS)


def _close(db: Session, shift: Shift, closed_by: str, at: datetime | None = None) -> None:
    shift.clocked_out_at = at or datetime.now(timezone.utc)
    shift.closed_by = closed_by


def clock_in(db: Session, user: User) -> Shift:
    """
    Start a shift, and mark the person on duty.

    Idempotent: pressing "I'm in" twice is a double tap, not a second shift,
    and returns the one already running. Anything older than the cutoff is
    closed first, so a forgotten clock-out does not swallow today.

    The deactivated check lives here rather than on the route. A deactivated
    person cannot reach the route at all — the auth dependency refuses their
    token first — but the ADMIN duty toggle calls this too, and it had no such
    guard: an admin could put a deactivated baker on duty, opening a shift
    against an account that can never log in to close it and handing them
    assignments they will never see.
    """
    if not user.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"{user.name}'s account is deactivated. Reactivate it first.",
        )

    existing = open_shift(db, user.id)
    if existing is not None:
        if not user.on_duty:
            user.on_duty = True
            db.commit()
        return existing

    shift = Shift(user_id=user.id, clocked_in_at=datetime.now(timezone.utc))
    db.add(shift)
    user.on_duty = True
    db.commit()
    db.refresh(shift)
    return shift


def clock_out(db: Session, user: User, closed_by: str = "self") -> Shift | None:
    """End the open shift, and mark the person off duty."""
    shift = open_shift(db, user.id)
    if shift is not None:
        _close(db, shift, closed_by=closed_by)
    if user.on_duty:
        user.on_duty = False
    db.commit()
    if shift is not None:
        db.refresh(shift)
    return shift


def set_on_duty(db: Session, user: User, on_duty: bool, by: str = "self") -> Shift | None:
    """
    The single door the duty toggles go through.

    They used to assign user.on_duty directly, which is what let the flag and
    the attendance record disagree.
    """
    if on_duty:
        return clock_in(db, user)
    return clock_out(db, user, closed_by=by)


def hours_today(db: Session, user_id: int, now: datetime | None = None) -> float:
    """
    Hours recorded for this person since midnight UTC, open shift included.

    Rounded to two places — the roster shows "6.5h", and a float carrying
    fifteen digits of a subtraction is not a number anybody reads.
    """
    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total = timedelta()
    shifts = (
        db.query(Shift)
        .filter(Shift.user_id == user_id)
        .order_by(Shift.clocked_in_at.desc())
        .limit(20)
        .all()
    )
    for shift in shifts:
        started = _aware(shift.clocked_in_at)
        ended = _aware(shift.clocked_out_at) or now
        if ended <= midnight:
            continue
        total += ended - max(started, midnight)
    return round(total.total_seconds() / 3600, 2)
