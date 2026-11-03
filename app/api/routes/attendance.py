"""
Attendance Routes
=================
- A baker or rider clocks themselves in and out
- They see their own shift and today's hours
- An admin reads the roster: who is in, since when

Deliberately the staff member's own action rather than something an admin
ticks on their behalf. `users.on_duty` was a boolean with no timestamp and no
history, so "present" was a guess somebody else recorded and there was nothing
to answer "what time did he get in?" the morning after a cake went out late.

Every write here goes through app/services/attendance.py, which is also where
the duty toggles now go, so the flag and the shift record cannot disagree.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.auth import require_admin, require_role
from app.db import get_db
from app.models.shift import Shift
from app.models.user import User, UserRole
from app.schemas import AttendanceRead, RosterEntry
from app.services.attendance import (
    clock_in, clock_out, hours_today, open_shift,
)

router = APIRouter(prefix="/attendance", tags=["Attendance"])

# Only these two work shifts. An admin is not on a rota and a customer is not
# staff, so neither has anything to clock.
STAFF = (UserRole.BAKER, UserRole.RIDER)


def _state(db: Session, user: User) -> AttendanceRead:
    shift = open_shift(db, user.id)
    return AttendanceRead(
        user_id=user.id,
        name=user.name,
        role=user.role.value if hasattr(user.role, "value") else user.role,
        on_duty=bool(user.on_duty),
        clocked_in_at=shift.clocked_in_at if shift else None,
        hours_today=hours_today(db, user.id),
    )


@router.get("/me", response_model=AttendanceRead)
def my_attendance(
    user: User = Depends(require_role(*STAFF)),
    db: Session = Depends(get_db),
):
    return _state(db, user)


@router.post("/clock-in", response_model=AttendanceRead)
def i_am_in(
    user: User = Depends(require_role(*STAFF)),
    db: Session = Depends(get_db),
):
    """
    "I'm in."

    Idempotent on purpose. A second press is a double tap on a phone in a
    kitchen, not a second shift, and it must not reset the arrival time
    somebody is going to be asked about later.

    Nothing here checks whether the account is active: the auth dependency
    refuses a deactivated person's token before this runs, and clock_in()
    checks again for the callers that do not come through a request.
    """
    clock_in(db, user)
    return _state(db, user)


@router.post("/clock-out", response_model=AttendanceRead)
def i_am_off(
    user: User = Depends(require_role(*STAFF)),
    db: Session = Depends(get_db),
):
    """Ends the shift and takes them off the assignment roster."""
    clock_out(db, user)
    return _state(db, user)


@router.get("/roster", response_model=list[RosterEntry])
def roster(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Who is in right now, and since when.

    Present first, then absent, each alphabetical — the useful half of this
    list is the people in the building, and burying them among everyone who is
    not there is how a roster stops being read.
    """
    staff = (
        db.query(User)
        .filter(User.role.in_(STAFF))
        .order_by(User.name)
        .all()
    )

    rows: list[RosterEntry] = []
    for person in staff:
        shift = open_shift(db, person.id)
        last = (
            db.query(Shift)
            .filter(Shift.user_id == person.id, Shift.clocked_out_at.isnot(None))
            .order_by(Shift.clocked_out_at.desc())
            .first()
        )
        rows.append(RosterEntry(
            user_id=person.id,
            name=person.name,
            role=person.role.value if hasattr(person.role, "value") else person.role,
            is_active=bool(person.is_active),
            present=shift is not None,
            on_duty=bool(person.on_duty),
            clocked_in_at=shift.clocked_in_at if shift else None,
            last_seen_at=last.clocked_out_at if last else None,
            hours_today=hours_today(db, person.id),
        ))

    rows.sort(key=lambda r: (not r.present, r.name.lower()))
    return rows


@router.post("/{staff_id}/clock-out", response_model=AttendanceRead)
def send_home(
    staff_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Close somebody else's shift.

    For the person who went home at six and left the app open. There is no
    matching clock-IN for an admin: being present is a thing you say about
    yourself, and an arrival time typed by somebody who was not watching the
    door is worse than no arrival time at all.
    """
    person = db.query(User).filter(
        User.id == staff_id, User.role.in_(STAFF),
    ).first()
    if not person:
        raise HTTPException(status_code=404, detail="Staff member not found")

    clock_out(db, person, closed_by="admin")
    return _state(db, person)
