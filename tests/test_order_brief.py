"""
The brief the kitchen works from, and the deadline it works to.

TWO THINGS A BAKER COULD NOT SEE
--------------------------------
What the cake is meant to look like. `orders.notes` is a single box the
CUSTOMER fills in at checkout; there was no field in the other direction at
all. Reference photos, the spelling of a name, "pipe it like last week's" —
all of it travelled by WhatsApp, attached to nothing, and a baker opening the
order saw none of it.

When it has to be finished. The queue showed the delivery time and left the
baker to subtract the hour it takes to box a cake and drive it across Lucknow.
An 8 PM cake read as an 8 PM deadline, so cakes went out late while every
screen said they were on time.

These tests call the HTTP routes the way the browser does, because a route can
be broken in ways the service underneath it is not — constructing the row,
parsing the upload, serialising the reply.
"""

import io
from datetime import datetime, timedelta, timezone

import pytest

from app.models.order import OrderStatus
from app.models.order_attachment import AttachmentKind, OrderAttachment
from app.models.site_settings import SiteSettings
from app.models.user import UserRole
from app.schemas import OrderRead, OrderTrackingRead
from app.services.order_service import (
    DEFAULT_KITCHEN_LEAD_MINUTES,
    _enrich_order,
    kitchen_lead_minutes,
    ready_by,
)

from tests.conftest import auth, make_order, make_user


# A real PNG. save_image sniffs the bytes, so a text file named .png is
# refused and would fail these for the wrong reason.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
# An EBML header — what Chrome's MediaRecorder writes at the top of a .webm.
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60


def _photo(name="reference.png"):
    return {"file": (name, io.BytesIO(PNG), "image/png")}


def _voice(name="note.webm"):
    return {"file": (name, io.BytesIO(WEBM), "audio/webm")}


@pytest.fixture
def assigned(db, customer, baker):
    return make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)


# ─── LEAVING A BRIEF ──────────────────────────────────

def test_the_admin_leaves_a_note_and_the_baker_reads_it(client, db, admin, baker, assigned):
    res = client.post(
        f"/orders/{assigned.id}/attachments/note",
        json={"body": "The name is spelled Aarav, not Arav."},
        headers=auth(admin),
    )
    assert res.status_code == 201, res.text
    assert res.json()["kind"] == AttachmentKind.NOTE
    assert res.json()["author_name"] == admin.name
    assert res.json()["author_role"] == "admin"

    seen = client.get(f"/orders/{assigned.id}/attachments", headers=auth(baker))
    assert seen.status_code == 200, seen.text
    bodies = [a["body"] for a in seen.json()]
    assert bodies == ["The name is spelled Aarav, not Arav."]


def test_every_reference_picture_is_kept_not_just_the_last_one(client, db, admin, assigned):
    """
    The point of multiple slots. A customer sends four photos of what they
    want; all four belong on the order. A single column would have held one.
    """
    for _ in range(4):
        res = client.post(f"/orders/{assigned.id}/attachments/photo",
                          files=_photo(), headers=auth(admin))
        assert res.status_code == 201, res.text

    photos = [a for a in client.get(f"/orders/{assigned.id}/attachments",
                                    headers=auth(admin)).json()
              if a["kind"] == AttachmentKind.PHOTO]
    assert len(photos) == 4
    # Each got its own file rather than overwriting the one before.
    assert len({p["file_url"] for p in photos}) == 4


def test_a_reference_picture_can_carry_a_caption(client, db, admin, assigned):
    res = client.post(
        f"/orders/{assigned.id}/attachments/photo",
        files=_photo(), data={"caption": "This piping, not the one on the box"},
        headers=auth(admin),
    )
    assert res.status_code == 201, res.text
    assert res.json()["body"] == "This piping, not the one on the box"
    assert res.json()["file_url"]


def test_a_voice_note_is_stored_with_how_long_it_runs(client, db, admin, assigned):
    res = client.post(
        f"/orders/{assigned.id}/attachments/voice",
        files=_voice(), data={"duration_seconds": "14"},
        headers=auth(admin),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == AttachmentKind.VOICE
    assert body["duration_seconds"] == 14
    assert body["file_url"].endswith(".webm")


def test_an_absurd_duration_is_dropped_rather_than_displayed(client, db, admin, assigned):
    """The browser supplies it, so it is a label, not a fact."""
    res = client.post(f"/orders/{assigned.id}/attachments/voice",
                      files=_voice(), data={"duration_seconds": "-5"},
                      headers=auth(admin))
    assert res.status_code == 201, res.text
    assert res.json()["duration_seconds"] is None


def test_a_file_that_is_not_a_recording_is_refused(client, db, admin, assigned):
    """
    Same rule the image path already enforces, and for the same reason:
    UPLOAD_DIR is served by StaticFiles, which types a file by its extension.
    A .webm that is really HTML would come back from /media as a live document
    on the API's own origin.
    """
    bad = {"file": ("note.webm", io.BytesIO(b"<html><script>alert(1)</script>"), "audio/webm")}
    res = client.post(f"/orders/{assigned.id}/attachments/voice",
                      files=bad, headers=auth(admin))
    assert res.status_code == 400, res.text


def test_an_empty_note_is_refused(client, db, admin, assigned):
    res = client.post(f"/orders/{assigned.id}/attachments/note",
                      json={"body": "   "}, headers=auth(admin))
    assert res.status_code == 422, res.text


# ─── WHO CAN SEE IT ───────────────────────────────────

def test_a_baker_cannot_read_another_bakers_order(client, db, admin, baker, assigned):
    other = make_user(db, "Ravi Baker", UserRole.BAKER)
    client.post(f"/orders/{assigned.id}/attachments/note",
                json={"body": "Secret piping technique"}, headers=auth(admin))

    res = client.get(f"/orders/{assigned.id}/attachments", headers=auth(other))
    assert res.status_code == 403, res.text


def test_the_customer_cannot_read_the_kitchens_brief(client, db, admin, customer, assigned):
    client.post(f"/orders/{assigned.id}/attachments/note",
                json={"body": "Quote them 1500 if they ask"}, headers=auth(admin))

    res = client.get(f"/orders/{assigned.id}/attachments", headers=auth(customer))
    assert res.status_code == 403, res.text


def test_the_baker_can_answer_back(client, db, baker, assigned):
    res = client.post(f"/orders/{assigned.id}/attachments/note",
                      json={"body": "Do you want it in the gold box?"},
                      headers=auth(baker))
    assert res.status_code == 201, res.text
    assert res.json()["author_role"] == "baker"


def test_a_baker_cannot_delete_the_instructions_they_were_given(client, db, admin, baker, assigned):
    """
    Otherwise "I was never told" is one button away from being true.
    """
    left = client.post(f"/orders/{assigned.id}/attachments/note",
                       json={"body": "Deliver before 6, they are travelling"},
                       headers=auth(admin)).json()

    res = client.delete(f"/orders/{assigned.id}/attachments/{left['id']}",
                        headers=auth(baker))
    assert res.status_code == 403, res.text
    assert db.query(OrderAttachment).filter(OrderAttachment.id == left["id"]).first()


def test_a_baker_can_remove_their_own_note(client, db, baker, assigned):
    mine = client.post(f"/orders/{assigned.id}/attachments/note",
                       json={"body": "oops wrong order"},
                       headers=auth(baker)).json()

    res = client.delete(f"/orders/{assigned.id}/attachments/{mine['id']}",
                        headers=auth(baker))
    assert res.status_code == 204, res.text
    assert db.query(OrderAttachment).filter(OrderAttachment.id == mine["id"]).first() is None


def test_the_brief_travels_on_the_order_itself(client, db, admin, baker, assigned):
    """
    The baker's queue reads /baker/my-queue, not the attachments route. If the
    brief is not on OrderRead the kitchen screen shows an empty order however
    many notes are attached to it.
    """
    client.post(f"/orders/{assigned.id}/attachments/note",
                json={"body": "Two tiers, not three"}, headers=auth(admin))

    queue = client.get("/baker/my-queue", headers=auth(baker))
    assert queue.status_code == 200, queue.text
    row = next(o for o in queue.json() if o["id"] == assigned.id)
    assert [a["body"] for a in row["attachments"]] == ["Two tiers, not three"]


# ─── THE DEADLINE ─────────────────────────────────────

def test_an_eight_pm_delivery_is_due_out_of_the_kitchen_at_seven(db, customer, baker):
    order = make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)
    order.delivery_time = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    db.commit()

    # Compared against what the column actually returns rather than against a
    # literal: these tests run on SQLite, which has no timestamptz and hands
    # back a naive datetime, while production is Postgres and hands back an
    # aware one. The subtraction is the thing under test either way.
    assert ready_by(order) == order.delivery_time - timedelta(hours=1)
    assert ready_by(order).hour == 19
    assert ready_by(order).day == 2


def test_an_order_with_no_slot_has_no_deadline(db, customer, baker):
    order = make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)
    order.delivery_time = None
    db.commit()

    assert ready_by(order) is None


def test_the_deadline_reaches_the_bakers_queue(client, db, baker, customer):
    order = make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)
    order.delivery_time = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    db.commit()

    row = next(o for o in client.get("/baker/my-queue", headers=auth(baker)).json()
               if o["id"] == order.id)
    assert row["ready_by"] is not None
    assert row["ready_by"].startswith("2026-11-02T19:00")


def test_the_customer_is_not_shown_the_kitchens_deadline(db, customer, baker):
    """
    Deliberately absent from the tracking schema. Telling a customer who booked
    8 PM that their cake is due at 7 reads as the order being late.
    """
    assert "ready_by" in OrderRead.model_fields
    assert "ready_by" not in OrderTrackingRead.model_fields


def test_moving_the_delivery_moves_the_deadline_with_it(db, customer, baker):
    """Derived rather than stored, so the two can never disagree."""
    order = make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)
    order.delivery_time = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    db.commit()
    first = ready_by(order)

    order.delivery_time = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)
    db.commit()

    assert ready_by(order) == first + timedelta(hours=2)


# ─── THE LEAD TIME SETTING ────────────────────────────

def test_the_lead_time_defaults_to_an_hour(db):
    assert kitchen_lead_minutes(db) == DEFAULT_KITCHEN_LEAD_MINUTES == 60


def test_the_shop_can_change_the_lead_time(db):
    db.add(SiteSettings(key="kitchen_lead_minutes", value="90"))
    db.commit()
    assert kitchen_lead_minutes(db) == 90


@pytest.mark.parametrize("value", ["-30", "abc", "", "99999"])
def test_a_nonsense_lead_time_falls_back_rather_than_breaking_the_queue(db, value):
    """
    A negative lead puts the deadline after the delivery; an absurd one marks
    every order overdue the moment it is placed. Either way the kitchen screen
    is the thing that breaks, so bad input is ignored.
    """
    db.add(SiteSettings(key="kitchen_lead_minutes", value=value))
    db.commit()
    assert kitchen_lead_minutes(db) == DEFAULT_KITCHEN_LEAD_MINUTES


def test_enrich_uses_the_lead_time_it_is_given(db, customer, baker):
    order = make_order(db, customer, status=OrderStatus.ASSIGNED, baker=baker)
    order.delivery_time = datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc)
    db.commit()

    _enrich_order(order, lead_minutes=90)
    assert order.ready_by == order.delivery_time - timedelta(minutes=90)
    assert (order.ready_by.hour, order.ready_by.minute) == (18, 30)
