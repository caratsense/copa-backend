"""
The four photo endpoints, called the way the browser calls them.

tests/test_proof_of_work.py pins the *rules* - it calls update_order_status
directly and checks that an unphotographed cake cannot advance. Every one of
those passed while all four HTTP endpoints returned 500 to every request in
production, because none of them went through the routes.

The bug was `OrderEvent(..., description=...)`, and OrderEvent has no
description column. A single real call to any of these four would have caught
it. So these tests make that call: multipart upload, auth header, status code,
and the row that comes back.
"""

import io

import pytest

from app.models.event import OrderEvent
from app.models.order import Fulfilment, OrderStatus

from tests.conftest import auth, make_order


# A real PNG. save_image sniffs the bytes rather than trusting the filename,
# so a text file called .png is rejected and would fail these for the wrong
# reason.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _photo(name="cake.png"):
    return {"file": (name, io.BytesIO(PNG), "image/png")}


# ─── THE BAKER'S PHOTO ────────────────────────────────

def test_baker_uploads_a_photo(db, client, customer, baker):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)

    res = client.post(f"/orders/{order.id}/proof",
                      files=_photo(), headers=auth(baker))

    assert res.status_code == 200, res.text
    assert res.json()["proof_image"]
    db.refresh(order)
    assert order.proof_image


def test_uploading_a_photo_records_an_event(db, client, customer, baker):
    """The crash was in building this row, so it is worth asserting it exists."""
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)

    client.post(f"/orders/{order.id}/proof", files=_photo(), headers=auth(baker))

    event = (db.query(OrderEvent)
             .filter(OrderEvent.order_id == order.id,
                     OrderEvent.event_type == "BAKE_PROOF_UPLOADED")
             .first())
    assert event is not None
    assert event.payload.get("note")


def test_a_customer_cannot_photograph_their_own_cake(db, client, customer):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       with_proof=False)

    res = client.post(f"/orders/{order.id}/proof",
                      files=_photo(), headers=auth(customer))

    assert res.status_code in (401, 403)


# ─── SHRIYA LOOKING AT IT ─────────────────────────────

def test_admin_verifies_a_photo(db, client, customer, baker, admin):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)
    client.post(f"/orders/{order.id}/proof", files=_photo(), headers=auth(baker))

    res = client.post(f"/orders/{order.id}/verify-proof", headers=auth(admin))

    assert res.status_code == 200, res.text
    db.refresh(order)
    assert order.proof_verified_at is not None


def test_verifying_nothing_is_refused(db, client, customer, baker, admin):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)

    res = client.post(f"/orders/{order.id}/verify-proof", headers=auth(admin))

    assert res.status_code == 400
    assert "no photo" in res.json()["detail"].lower()


def test_admin_sends_a_cake_back(db, client, customer, baker, admin):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)
    client.post(f"/orders/{order.id}/proof", files=_photo(), headers=auth(baker))

    res = client.post(f"/orders/{order.id}/reject-proof",
                      json={"reason": "The piping is smudged"}, headers=auth(admin))

    assert res.status_code == 200, res.text
    db.refresh(order)
    # The photo goes with the rejection, so the cake cannot be waved through
    # on a picture of the one that was turned down.
    assert order.proof_image is None
    assert order.proof_verified_at is None


# ─── THE DOORSTEP, OR THE COUNTER ─────────────────────

def test_rider_uploads_a_handover_photo(db, client, customer, rider):
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY,
                       rider=rider, with_proof=False)

    res = client.post(f"/orders/{order.id}/handover",
                      files=_photo(), headers=auth(rider))

    assert res.status_code == 200, res.text
    db.refresh(order)
    assert order.handover_image


def test_a_rider_cannot_photograph_somebody_elses_delivery(db, client, customer, rider):
    from tests.conftest import make_user
    from app.models.user import UserRole

    other = make_user(db, "Someone Else", UserRole.RIDER, phone="+919000000123")
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY,
                       rider=rider, with_proof=False)

    res = client.post(f"/orders/{order.id}/handover",
                      files=_photo(), headers=auth(other))

    assert res.status_code == 403


def test_the_counter_can_photograph_a_collection(db, client, customer, baker):
    """No rider is assigned to a collection, so whoever is at the counter does it."""
    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment=Fulfilment.PICKUP.value, with_proof=False)

    res = client.post(f"/orders/{order.id}/handover",
                      files=_photo(), headers=auth(baker))

    assert res.status_code == 200, res.text


# ─── NOT A PHOTOGRAPH ─────────────────────────────────

def test_a_text_file_renamed_png_is_refused(db, client, customer, baker):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)

    res = client.post(
        f"/orders/{order.id}/proof",
        files={"file": ("cake.png", io.BytesIO(b"#!/bin/sh\nrm -rf /"), "image/png")},
        headers=auth(baker),
    )

    assert res.status_code == 400
    db.refresh(order)
    assert order.proof_image is None


# ─── SHRIYA BEING TOLD ────────────────────────────────

def test_uploading_a_photo_tells_the_shop(db, client, customer, baker, admin,
                                          wa_secret, sent_messages):
    """
    "Shriya receives it" was the point of the whole feature.

    The call that does this is wrapped in a broad except, so if it breaks the
    photo still saves, the baker sees success, and the only trace is a line in
    a log nobody reads. That is exactly the shape of failure worth a test.
    """
    admin.phone = "+919000000001"
    admin.whatsapp_opt_in = True
    db.commit()

    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)
    sent_messages.clear()

    res = client.post(f"/orders/{order.id}/proof",
                      files=_photo(), headers=auth(baker))

    assert res.status_code == 200, res.text
    assert sent_messages, "the photo was saved but nobody was told about it"
    assert any(str(order.id) in str(m) for m in sent_messages)
