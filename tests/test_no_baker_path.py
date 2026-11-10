"""
A shop with no baker can still get a cake out of the door.

THE MORNING THIS COMES FROM
---------------------------
The bakery's first public order arrived, paid, for collection. There was no
baker on the books - the test accounts had been cleared out before launch and
the real hire had not started. The admin screen offered one control on a
confirmed order, Assign Baker, which calls the auto-assigner; the auto-assigner
answers "No bakers available" when there are none. The order detail drawer
offered nothing at all.

So: a paid order, a customer expecting a cake, and no way forward anywhere in
the product. Nothing was broken in the sense of an exception - every status
move the order needed was legal, and the server would have allowed every one
of them. There was simply no screen that asked for them.

WHAT THESE PIN DOWN
-------------------
That the whole walk is possible over HTTP as the admin, with no baker and no
rider in the database at all, and that the gates which SHOULD stop an order
are still standing - an unpaid one cannot be started, and a collection still
cannot be given to a rider.

Route-level on purpose. The services were never the problem: 709 service tests
passed while four photo endpoints returned 500, and the whole failure here was
an endpoint nothing called.
"""

from datetime import datetime, timezone

import io

import pytest

from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.user import UserRole
from tests.conftest import auth, make_order, make_user


@pytest.fixture
def paid_pickup(db, customer):
    """
    Order #45, in essence: paid online, for collection, no baker, no photos.

    with_proof=False matters - the photographs are the gates being walked
    through here, and staging them would walk past the thing being tested.
    """
    order = make_order(
        db, customer,
        status=OrderStatus.CONFIRMED,
        fulfilment="PICKUP",
        with_proof=False,
    )
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PAID
    db.commit()
    db.refresh(order)
    return order


def _png() -> bytes:
    """The smallest thing save_image will accept as a photograph."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
        b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _move(client, admin, order_id, status, **body):
    return client.patch(
        f"/orders/{order_id}/status",
        json={"status": status, **body},
        headers=auth(admin),
    )


# ── The walk ─────────────────────────────────────────────────────────────


def test_no_bakers_exist_at_all(db):
    """The premise. If a fixture ever adds one, every test below is vacuous."""
    from app.models.user import User

    assert db.query(User).filter(User.role == UserRole.BAKER).count() == 0


def test_a_paid_collection_goes_all_the_way_through_with_no_baker(
    client, db, admin, paid_pickup
):
    """
    The whole point, end to end, over HTTP.

    Every step the admin screen now offers, in the order it offers them. If
    any one of these starts refusing, the shop is stuck again.
    """
    oid = paid_pickup.id

    # Into the kitchen. ASSIGNED means "the kitchen has it" - with nobody to
    # assign, it is a state the order passes through rather than sits in,
    # which is why the button takes both steps at once.
    assert _move(client, admin, oid, "ASSIGNED",
                 reason="No baker assigned - the shop is making this in-house").status_code == 200
    assert _move(client, admin, oid, "IN_PRODUCTION").status_code == 200

    # She may still photograph it — the endpoint takes an admin — and this
    # walk does, because a shop that wants the record should be able to keep
    # one. What it must NOT do is demand it: see
    # test_an_in_house_cake_needs_no_photograph for the same walk with no
    # photograph at all.
    shot = client.post(
        f"/orders/{oid}/proof",
        files={"file": ("cake.png", io.BytesIO(_png()), "image/png")},
        headers=auth(admin),
    )
    assert shot.status_code == 200, shot.text
    assert shot.json()["proof_image"]

    assert _move(client, admin, oid, "AWAITING_APPROVAL").status_code == 200
    assert client.post(f"/orders/{oid}/verify-proof", headers=auth(admin)).status_code == 200
    assert _move(client, admin, oid, "PACKAGED").status_code == 200

    # And out across the counter. No rider is involved and no handover photo
    # is demanded, because the staff hand it over themselves.
    assert _move(client, admin, oid, "READY_FOR_PICKUP").status_code == 200
    assert _move(client, admin, oid, "COLLECTED").status_code == 200

    db.refresh(paid_pickup)
    assert paid_pickup.status == OrderStatus.COLLECTED
    assert paid_pickup.assigned_baker_id is None
    assert paid_pickup.assigned_rider_id is None


def test_the_reason_lands_on_the_timeline(client, db, admin, paid_pickup):
    """
    Why it was done this way has to be readable later.

    "Made in-house" is the difference between an order a baker worked on and
    one the shop did itself, and the only place that distinction can live on
    an order with no baker is its history.
    """
    _move(client, admin, paid_pickup.id, "ASSIGNED",
          reason="No baker assigned - the shop is making this order in-house")

    events = client.get(f"/orders/{paid_pickup.id}/events", headers=auth(admin)).json()
    reasons = [e["payload"].get("reason") for e in events if e["event_type"] == "STATUS_CHANGED"]
    assert any(r and "in-house" in r for r in reasons), events


def test_the_admin_may_photograph_the_cake_herself(client, admin, paid_pickup):
    """
    The gate needs a screen.

    AWAITING_APPROVAL requires a photograph, and the only screen that took one
    was the baker's queue. A rule with no way for anybody present to satisfy
    it is a dead end, so this endpoint has to keep accepting an admin.
    """
    response = client.post(
        f"/orders/{paid_pickup.id}/proof",
        files={"file": ("cake.png", io.BytesIO(_png()), "image/png")},
        headers=auth(admin),
    )
    assert response.status_code == 200, response.text


# ── What must still stop an order ────────────────────────────────────────


def test_an_unpaid_order_cannot_be_made_in_house_either(client, db, customer, admin):
    """
    The new path is not a way round the payment gate.

    It would be an easy thing to lose: the gate is on the transition, and the
    in-house route takes two of them in a row.
    """
    order = make_order(db, customer, status=OrderStatus.CONFIRMED,
                       fulfilment="PICKUP", with_proof=False)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PENDING
    db.commit()

    refused = _move(client, admin, order.id, "ASSIGNED")
    assert refused.status_code == 400
    assert "not paid" in refused.json()["detail"].lower()

    db.refresh(order)
    assert order.status == OrderStatus.CONFIRMED


def test_a_collection_still_cannot_be_given_to_a_rider(client, db, admin, paid_pickup):
    """Nobody carries a cake the customer is coming for."""
    refused = client.post(
        f"/admin/orders/{paid_pickup.id}/assign-rider", json={}, headers=auth(admin),
    )
    assert refused.status_code == 400
    assert "pickup" in refused.json()["detail"].lower()


def test_asking_for_a_baker_when_there_are_none_says_what_to_do_instead(
    client, admin, paid_pickup
):
    """
    The message is the product here.

    "No bakers available. No active bakers found." is true and useless: it
    reads as a dead end on the one screen that has to keep moving. Whoever is
    standing there needs to know the shop can take the order itself.
    """
    refused = client.post(
        f"/admin/orders/{paid_pickup.id}/assign-baker", json={}, headers=auth(admin),
    )
    assert refused.status_code == 400
    detail = refused.json()["detail"]
    assert "in-house" in detail.lower(), detail


def test_a_real_baker_is_still_preferred_when_there_is_one(client, db, admin, paid_pickup):
    """
    The in-house path is a fallback, not a replacement.

    Once Shriya hires somebody, auto-assignment has to go back to working -
    nothing about this change should make a shop with a baker behave like a
    shop without one.
    """
    baker = make_user(db, "Shreyansh Baker", UserRole.BAKER)

    assigned = client.post(
        f"/admin/orders/{paid_pickup.id}/assign-baker", json={}, headers=auth(admin),
    )
    assert assigned.status_code == 200, assigned.text

    db.refresh(paid_pickup)
    assert paid_pickup.assigned_baker_id == baker.id


# ── The photograph is for work somebody else did ─────────────────────────


def test_an_in_house_cake_needs_no_photograph(client, db, admin, paid_pickup):
    """
    Shriya photographing a cake so that Shriya can approve the photograph is
    a step between the shop and its own counter.

    The gate exists so the shop can see work it did not watch being done. With
    nobody else involved there is nothing to check up on — the quality check
    is her looking at the cake in front of her.
    """
    oid = paid_pickup.id
    assert _move(client, admin, oid, "ASSIGNED").status_code == 200
    assert _move(client, admin, oid, "IN_PRODUCTION").status_code == 200

    # No photo on the order at all.
    db.refresh(paid_pickup)
    assert paid_pickup.proof_image is None
    assert paid_pickup.assigned_baker_id is None

    assert _move(client, admin, oid, "AWAITING_APPROVAL").status_code == 200
    assert _move(client, admin, oid, "PACKAGED").status_code == 200
    assert _move(client, admin, oid, "READY_FOR_PICKUP").status_code == 200

    db.refresh(paid_pickup)
    assert paid_pickup.status == OrderStatus.READY_FOR_PICKUP


def test_a_baker_still_cannot_wave_a_cake_through_unseen(client, db, admin, customer):
    """
    The other half, and the reason the gate exists at all.

    "Baked" used to be a button, and the shop's only record that a cake had
    been made was that somebody pressed it. The moment an order belongs to
    somebody else, the photograph is required again.
    """
    baker = make_user(db, "Shreyansh Baker", UserRole.BAKER)
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       fulfilment="PICKUP", baker=baker, with_proof=False)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PAID
    db.commit()

    refused = _move(client, admin, order.id, "AWAITING_APPROVAL")
    assert refused.status_code == 400
    assert "photo" in refused.json()["detail"].lower()

    shot = client.post(
        f"/orders/{order.id}/proof",
        files={"file": ("cake.png", io.BytesIO(_png()), "image/png")},
        headers=auth(admin),
    )
    assert shot.status_code == 200
    assert _move(client, admin, order.id, "AWAITING_APPROVAL").status_code == 200

    # And packing still waits on somebody having looked at that photograph.
    assert _move(client, admin, order.id, "PACKAGED").status_code == 400
    assert client.post(f"/orders/{order.id}/verify-proof", headers=auth(admin)).status_code == 200
    assert _move(client, admin, order.id, "PACKAGED").status_code == 200
