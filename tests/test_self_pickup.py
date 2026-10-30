"""
Self pickup is a thing the system knows about.

Before the `fulfilment` column, it was not. Both front doors recorded a
collection by throwing the fact away - the website sent delivery_address=null
and the bot did the same - which is indistinguishable from a delivery whose
address failed to save. So nothing downstream could tell, and a cake the
customer was coming to fetch was given a rider, put on the live delivery board
and announced as out for delivery. Orders #26 and #27 went out that way.

These tests pin the parts that stopped that happening again.
"""

import pytest
from fastapi import HTTPException

from app.models.order import Fulfilment, OrderStatus
from app.schemas import StatusUpdate
from app.services.fleet_tracking import get_active_deliveries
from app.services.order_service import update_order_status

from tests.conftest import make_order


# ─── THE COLUMN ───────────────────────────────────────

def test_a_pickup_order_says_so_rather_than_having_no_address(db, customer):
    order = make_order(db, customer, status=OrderStatus.PACKAGED,
                       fulfilment=Fulfilment.PICKUP.value)

    assert order.is_pickup is True
    # The old signal. A delivery that lost its address looks exactly like this,
    # which is why it could never be the signal.
    assert order.delivery_address is None


def test_a_delivery_is_the_default(db, customer):
    order = make_order(db, customer, status=OrderStatus.PACKAGED)
    assert order.is_pickup is False
    assert order.fulfilment == Fulfilment.DELIVERY.value


# ─── THE FORK OUT OF PACKAGED ─────────────────────────

def test_a_pickup_cannot_be_sent_out_for_delivery(db, customer, rider):
    """The exact move that happened to order #27."""
    order = make_order(db, customer, status=OrderStatus.PACKAGED,
                       fulfilment=Fulfilment.PICKUP.value, rider=rider)

    with pytest.raises(HTTPException) as e:
        update_order_status(db, order.id, StatusUpdate(status="OUT_FOR_DELIVERY"))

    assert e.value.status_code == 400
    assert "self pickup" in str(e.value.detail).lower()


def test_a_delivery_cannot_be_marked_ready_for_pickup(db, customer, rider):
    order = make_order(db, customer, status=OrderStatus.PACKAGED, rider=rider)

    with pytest.raises(HTTPException) as e:
        update_order_status(db, order.id, StatusUpdate(status="READY_FOR_PICKUP"))

    assert e.value.status_code == 400
    assert "delivery" in str(e.value.detail).lower()


def test_a_pickup_reaching_packaged_is_given_no_rider(db, customer, rider):
    """
    Auto-assignment fired on every PACKAGED transition, pickups included. The
    rider in this test is available and must still not be given the job.
    """
    order = make_order(db, customer, status=OrderStatus.AWAITING_APPROVAL,
                       fulfilment=Fulfilment.PICKUP.value)
    update_order_status(db, order.id, StatusUpdate(status="PACKAGED"))
    db.refresh(order)

    assert order.assigned_rider_id is None


# ─── THE BOARD ────────────────────────────────────────

def test_a_pickup_never_appears_on_the_live_delivery_board(db, customer, rider):
    make_order(db, customer, status=OrderStatus.PACKAGED,
               fulfilment=Fulfilment.PICKUP.value)
    delivery = make_order(db, customer, status=OrderStatus.PACKAGED, rider=rider)

    rows = get_active_deliveries(db)
    ids = [r["order_id"] for r in rows]

    assert delivery.id in ids
    assert len(ids) == 1, "a cake on the counter was listed as an undelivered job"


# ─── CLOSING IT OUT ───────────────────────────────────

def test_collection_runs_ready_then_collected(db, customer):
    order = make_order(db, customer, status=OrderStatus.PACKAGED,
                       fulfilment=Fulfilment.PICKUP.value)

    update_order_status(db, order.id, StatusUpdate(status="READY_FOR_PICKUP"))
    db.refresh(order)
    assert order.status == OrderStatus.READY_FOR_PICKUP

    update_order_status(db, order.id, StatusUpdate(status="COLLECTED"))
    db.refresh(order)
    assert order.status == OrderStatus.COLLECTED


def test_nobody_came_is_recoverable(db, customer):
    """Late is not never: they can still turn up and take it."""
    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment=Fulfilment.PICKUP.value)

    update_order_status(db, order.id, StatusUpdate(status="NOT_COLLECTED"))
    db.refresh(order)
    assert order.status == OrderStatus.NOT_COLLECTED

    update_order_status(db, order.id, StatusUpdate(status="COLLECTED"))
    db.refresh(order)
    assert order.status == OrderStatus.COLLECTED


def test_the_assign_rider_endpoint_refuses_a_collection(db, client, customer, rider, admin):
    """
    Hiding the button is not enforcing the rule.

    The admin table no longer offers Assign Rider for a pickup, but the
    endpoint is reachable without the table, and a UI-only rule is exactly how
    self pickup got a rider in the first place.
    """
    from tests.conftest import auth

    order = make_order(db, customer, status=OrderStatus.PACKAGED,
                       fulfilment=Fulfilment.PICKUP.value)

    res = client.post(f"/admin/orders/{order.id}/assign-rider",
                      json={"staff_id": rider.id}, headers=auth(admin))

    assert res.status_code == 400
    assert "self pickup" in res.json()["detail"].lower()
    db.refresh(order)
    assert order.assigned_rider_id is None


# ─── WHAT THE CUSTOMER IS SHOWN ───────────────────────

def test_the_customer_tracking_view_says_it_is_a_collection(db, client, customer):
    """
    The order page reads /orders/track/{id}, not /orders/{id}.

    fulfilment was added to OrderRead only, so the admin table knew an order
    was a collection and the customer's own page did not - it showed them
    "Delivery" with an empty "Where", which is the confusion this column
    exists to end.
    """
    from tests.conftest import auth

    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment=Fulfilment.PICKUP.value)

    res = client.get(f"/orders/track/{order.id}", headers=auth(customer))

    assert res.status_code == 200, res.text
    assert res.json()["fulfilment"] == "PICKUP"


def test_the_customer_tracking_view_defaults_to_delivery(db, client, customer):
    from tests.conftest import auth

    order = make_order(db, customer, status=OrderStatus.PACKAGED)

    res = client.get(f"/orders/track/{order.id}", headers=auth(customer))

    assert res.json()["fulfilment"] == "DELIVERY"


# ─── CLOSING A COLLECTION NEEDS NO PHOTOGRAPH ─────────

def test_the_status_route_closes_a_collection_with_no_handover_photo(db, client, customer, admin):
    """
    The gate that made every pickup order permanent.

    Closing an order as DELIVERED or COLLECTED required a handover photo. The
    only camera for one lives on the rider screen, and a collection never gets
    a rider, so there was no screen in the product that could satisfy it:
    orders #30 and #32 sat at READY FOR PICKUP with the button greyed out and
    reading "Photo needed", and nothing a human could press would change it.

    A rider is away from the shop and unobserved, so a delivery still needs
    the shot. A collection is handed across the counter by the staff, in front
    of the customer, and needs nothing.
    """
    from tests.conftest import auth

    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment=Fulfilment.PICKUP.value, with_proof=False)
    assert order.handover_image is None

    res = client.patch(f"/orders/{order.id}/status",
                       json={"status": "COLLECTED"}, headers=auth(admin))

    assert res.status_code == 200, res.text
    db.refresh(order)
    assert order.status == OrderStatus.COLLECTED


def test_the_status_route_still_refuses_a_delivery_with_no_handover_photo(db, client, customer, rider, admin):
    """The rule is not gone, it is confined to the case it was written for."""
    from tests.conftest import auth

    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY,
                       rider=rider, with_proof=False)
    order.proof_image = "/media/bake_1.jpg"
    db.commit()

    res = client.patch(f"/orders/{order.id}/status",
                       json={"status": "DELIVERED"}, headers=auth(admin))

    assert res.status_code == 400, res.text
    assert "photo" in res.json()["detail"].lower()
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


def test_the_owner_can_close_a_collection_over_whatsapp(db, customer):
    """
    COLLECTED / READY / NOTCOLLECTED are parsed as admin verbs, but the block
    that ran them sat inside the rider branch, which an admin never reaches.
    Shriya typing COLLECTED 32 got the command list back.
    """
    from app.api.routes.webhook import _handle_staff
    from app.models.user import UserRole
    from tests.conftest import make_user

    admin = make_user(db, "Shriya", UserRole.ADMIN)
    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment=Fulfilment.PICKUP.value, with_proof=False)

    reply = _handle_staff(db, admin, f"COLLECTED {order.id}", "919554444462")

    assert "collected" in reply.lower()
    assert "APPROVE <order no>" not in reply, "fell through to the help text"
    db.refresh(order)
    assert order.status == OrderStatus.COLLECTED


def test_the_owner_can_mark_a_collection_ready_over_whatsapp(db, customer):
    from app.api.routes.webhook import _handle_staff
    from app.models.user import UserRole
    from tests.conftest import make_user

    admin = make_user(db, "Shriya", UserRole.ADMIN)
    order = make_order(db, customer, status=OrderStatus.PACKAGED,
                       fulfilment=Fulfilment.PICKUP.value)

    reply = _handle_staff(db, admin, f"READY {order.id}", "919554444462")

    assert "ready to collect" in reply.lower()
    db.refresh(order)
    assert order.status == OrderStatus.READY_FOR_PICKUP


def test_whatsapp_still_refuses_to_collect_a_delivery(db, customer, rider):
    """The verb is for the counter; a delivery must go through the rider."""
    from app.api.routes.webhook import _handle_staff
    from app.models.user import UserRole
    from tests.conftest import make_user

    admin = make_user(db, "Shriya", UserRole.ADMIN)
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY, rider=rider)

    reply = _handle_staff(db, admin, f"COLLECTED {order.id}", "919554444462")

    assert "delivery, not a collection" in reply.lower()
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY
