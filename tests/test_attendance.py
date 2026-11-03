"""
Who is in, since when, and the one rule that keeps it honest.

`users.on_duty` and the open shift are two statements about the same fact.
Everything that can change either — the staff member's own "I'm in", the baker
duty toggle, the admin roster toggle, deactivating an account — goes through
app/services/attendance.py so the two are written together.

They have to be. The assignment engine reads `on_duty` to decide who gets the
next cake, and the roster reads the shift. Let them drift and the screen says a
baker has been in since nine while his orders are being handed to somebody
else, which is worse than having no roster at all.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.shift import Shift
from app.models.user import UserRole
from app.services.attendance import (
    MAX_SHIFT_HOURS, clock_in, clock_out, hours_today, open_shift, set_on_duty,
)

from tests.conftest import auth, make_user


@pytest.fixture
def off_duty_baker(db):
    return make_user(db, "Imran Baker", UserRole.BAKER, on_duty=False)


# ─── I'M IN ───────────────────────────────────────────

def test_pressing_im_in_starts_a_shift_and_puts_them_on_duty(client, db, off_duty_baker):
    res = client.post("/attendance/clock-in", headers=auth(off_duty_baker))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["on_duty"] is True
    assert body["clocked_in_at"] is not None

    db.refresh(off_duty_baker)
    assert off_duty_baker.on_duty is True
    assert open_shift(db, off_duty_baker.id) is not None


def test_pressing_it_twice_does_not_reset_the_arrival_time(client, db, off_duty_baker):
    """
    A double tap on a phone in a kitchen is not a second shift — and it must
    not overwrite the time somebody is going to be asked about later.
    """
    first = client.post("/attendance/clock-in", headers=auth(off_duty_baker)).json()
    second = client.post("/attendance/clock-in", headers=auth(off_duty_baker)).json()

    assert first["clocked_in_at"] == second["clocked_in_at"]
    assert db.query(Shift).filter(Shift.user_id == off_duty_baker.id).count() == 1


def test_clocking_out_closes_the_shift_and_takes_them_off_duty(client, db, off_duty_baker):
    client.post("/attendance/clock-in", headers=auth(off_duty_baker))

    res = client.post("/attendance/clock-out", headers=auth(off_duty_baker))

    assert res.status_code == 200, res.text
    assert res.json()["on_duty"] is False
    assert res.json()["clocked_in_at"] is None

    db.refresh(off_duty_baker)
    assert off_duty_baker.on_duty is False
    assert open_shift(db, off_duty_baker.id) is None

    closed = db.query(Shift).filter(Shift.user_id == off_duty_baker.id).one()
    assert closed.clocked_out_at is not None
    assert closed.closed_by == "self"


def test_a_deactivated_account_cannot_clock_in(client, db):
    """Refused by the auth dependency, before the route is even reached."""
    gone = make_user(db, "Former Rider", UserRole.RIDER, is_active=False)
    res = client.post("/attendance/clock-in", headers=auth(gone))
    assert res.status_code == 401, res.text
    assert open_shift(db, gone.id) is None


def test_an_admin_cannot_put_a_deactivated_person_on_duty(client, db, admin):
    """
    The gap the route-level check was hiding. A deactivated person cannot
    reach the clock-in route at all, but the admin duty toggle reaches the
    same service — and opening a shift against an account that can never log
    in to close it also hands them assignments nobody will ever see.
    """
    gone = make_user(db, "Former Baker", UserRole.BAKER, on_duty=False, is_active=False)

    res = client.patch(f"/admin/staff/{gone.id}/duty",
                       json={"on_duty": True}, headers=auth(admin))

    assert res.status_code == 400, res.text
    assert "deactivated" in res.text
    db.refresh(gone)
    assert gone.on_duty is False
    assert open_shift(db, gone.id) is None


def test_a_customer_has_no_shift_to_clock(client, customer):
    assert client.post("/attendance/clock-in", headers=auth(customer)).status_code == 403


def test_a_rider_clocks_in_the_same_way_a_baker_does(client, db):
    rider = make_user(db, "Sana Rider", UserRole.RIDER, on_duty=False)
    res = client.post("/attendance/clock-in", headers=auth(rider))
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "rider"


# ─── THE FLAG AND THE CLOCK STAY TOGETHER ─────────────

def test_the_baker_duty_toggle_also_clocks_them_in(client, db, off_duty_baker):
    """
    The toggle used to assign on_duty and commit, recording no time at all.
    """
    res = client.patch("/baker/duty-status", json={"on_duty": True},
                       headers=auth(off_duty_baker))

    assert res.status_code == 200, res.text
    assert res.json()["on_duty"] is True
    assert open_shift(db, off_duty_baker.id) is not None


def test_the_baker_duty_toggle_also_clocks_them_out(client, db, off_duty_baker):
    client.patch("/baker/duty-status", json={"on_duty": True}, headers=auth(off_duty_baker))
    client.patch("/baker/duty-status", json={"on_duty": False}, headers=auth(off_duty_baker))

    assert open_shift(db, off_duty_baker.id) is None
    db.refresh(off_duty_baker)
    assert off_duty_baker.on_duty is False


def test_the_admin_duty_toggle_keeps_the_two_in_step(client, db, admin, off_duty_baker):
    res = client.patch(f"/admin/staff/{off_duty_baker.id}/duty",
                       json={"on_duty": True}, headers=auth(admin))

    assert res.status_code == 200, res.text
    assert res.json()["on_duty"] is True
    assert res.json()["clocked_in_at"] is not None
    assert open_shift(db, off_duty_baker.id) is not None


def test_deactivating_an_account_ends_its_open_shift(client, db, admin, off_duty_baker):
    """
    Otherwise the shift runs forever against an account that can no longer log
    in to close it.
    """
    client.post("/attendance/clock-in", headers=auth(off_duty_baker))

    res = client.patch(f"/admin/staff/{off_duty_baker.id}/activate", headers=auth(admin))

    assert res.status_code == 200, res.text
    assert res.json()["is_active"] is False
    assert open_shift(db, off_duty_baker.id) is None


def test_the_staff_list_says_since_when(client, db, admin, off_duty_baker):
    client.post("/attendance/clock-in", headers=auth(off_duty_baker))

    row = next(s for s in client.get("/admin/staff", headers=auth(admin)).json()
               if s["id"] == off_duty_baker.id)
    assert row["on_duty"] is True
    assert row["clocked_in_at"] is not None


# ─── THE ROSTER ───────────────────────────────────────

def test_the_roster_puts_the_people_who_are_here_first(client, db, admin):
    away = make_user(db, "Aarti Baker", UserRole.BAKER, on_duty=False)
    here = make_user(db, "Zubin Rider", UserRole.RIDER, on_duty=False)
    client.post("/attendance/clock-in", headers=auth(here))

    rows = client.get("/attendance/roster", headers=auth(admin)).json()
    names = [r["name"] for r in rows]

    # Zubin sorts last alphabetically and is listed first anyway, because the
    # useful half of a roster is the people in the building.
    assert names[0] == "Zubin Rider"
    assert rows[0]["present"] is True
    assert next(r for r in rows if r["name"] == "Aarti Baker")["present"] is False
    assert away.id in [r["user_id"] for r in rows]


def test_the_roster_remembers_when_somebody_left(client, db, admin, off_duty_baker):
    client.post("/attendance/clock-in", headers=auth(off_duty_baker))
    client.post("/attendance/clock-out", headers=auth(off_duty_baker))

    row = next(r for r in client.get("/attendance/roster", headers=auth(admin)).json()
               if r["user_id"] == off_duty_baker.id)
    assert row["present"] is False
    assert row["last_seen_at"] is not None


def test_only_an_admin_reads_the_roster(client, off_duty_baker):
    assert client.get("/attendance/roster", headers=auth(off_duty_baker)).status_code == 403


def test_an_admin_can_send_somebody_home_who_forgot(client, db, admin, off_duty_baker):
    client.post("/attendance/clock-in", headers=auth(off_duty_baker))

    res = client.post(f"/attendance/{off_duty_baker.id}/clock-out", headers=auth(admin))

    assert res.status_code == 200, res.text
    assert res.json()["on_duty"] is False
    closed = db.query(Shift).filter(Shift.user_id == off_duty_baker.id).one()
    assert closed.closed_by == "admin"


def test_there_is_no_way_for_an_admin_to_clock_somebody_IN(client, db, admin, off_duty_baker):
    """
    Being present is a thing you say about yourself. An arrival time typed by
    somebody who was not watching the door is worse than no arrival time.
    """
    res = client.post(f"/attendance/{off_duty_baker.id}/clock-in", headers=auth(admin))
    assert res.status_code in (404, 405), res.text
    assert open_shift(db, off_duty_baker.id) is None


# ─── FORGOTTEN CLOCK-OUTS ─────────────────────────────

def test_a_shift_left_open_overnight_is_not_counted_for_ever(db, off_duty_baker):
    stale = Shift(
        user_id=off_duty_baker.id,
        clocked_in_at=datetime.now(timezone.utc) - timedelta(hours=MAX_SHIFT_HOURS + 3),
    )
    db.add(stale)
    db.commit()

    assert open_shift(db, off_duty_baker.id) is None

    db.refresh(stale)
    assert stale.clocked_out_at is not None
    # Closed at the cutoff, and marked as a guess rather than a reading.
    assert stale.closed_by == "stale"


def test_clocking_in_after_a_forgotten_clock_out_starts_a_fresh_shift(db, off_duty_baker):
    db.add(Shift(
        user_id=off_duty_baker.id,
        clocked_in_at=datetime.now(timezone.utc) - timedelta(hours=MAX_SHIFT_HOURS + 3),
    ))
    db.commit()

    shift = clock_in(db, off_duty_baker)

    assert shift.clocked_out_at is None
    assert db.query(Shift).filter(Shift.user_id == off_duty_baker.id).count() == 2


def test_a_shift_still_inside_the_cutoff_is_left_alone(db, off_duty_baker):
    running = Shift(
        user_id=off_duty_baker.id,
        clocked_in_at=datetime.now(timezone.utc) - timedelta(hours=MAX_SHIFT_HOURS - 1),
    )
    db.add(running)
    db.commit()

    assert open_shift(db, off_duty_baker.id) is not None


# ─── HOURS ────────────────────────────────────────────

def test_hours_today_counts_a_closed_shift(db, off_duty_baker):
    now = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
    db.add(Shift(
        user_id=off_duty_baker.id,
        clocked_in_at=now - timedelta(hours=5),
        clocked_out_at=now - timedelta(hours=1),
        closed_by="self",
    ))
    db.commit()

    assert hours_today(db, off_duty_baker.id, now=now) == 4.0


def test_hours_today_counts_the_shift_still_running(db, off_duty_baker):
    now = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
    db.add(Shift(user_id=off_duty_baker.id, clocked_in_at=now - timedelta(hours=2)))
    db.commit()

    assert hours_today(db, off_duty_baker.id, now=now) == 2.0


def test_hours_today_does_not_count_yesterday(db, off_duty_baker):
    now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    db.add(Shift(
        user_id=off_duty_baker.id,
        clocked_in_at=now - timedelta(days=1, hours=3),
        clocked_out_at=now - timedelta(days=1),
        closed_by="self",
    ))
    db.commit()

    assert hours_today(db, off_duty_baker.id, now=now) == 0.0


def test_a_shift_that_began_before_midnight_counts_only_todays_part(db, off_duty_baker):
    now = datetime.now(timezone.utc).replace(hour=3, minute=0, second=0, microsecond=0)
    db.add(Shift(user_id=off_duty_baker.id, clocked_in_at=now - timedelta(hours=6)))
    db.commit()

    assert hours_today(db, off_duty_baker.id, now=now) == 3.0


def test_set_on_duty_is_the_one_door(db, off_duty_baker):
    """Both toggles call it, so it has to do the whole job on its own."""
    set_on_duty(db, off_duty_baker, True)
    assert off_duty_baker.on_duty is True
    assert open_shift(db, off_duty_baker.id) is not None

    set_on_duty(db, off_duty_baker, False, by="admin")
    assert off_duty_baker.on_duty is False
    assert open_shift(db, off_duty_baker.id) is None
