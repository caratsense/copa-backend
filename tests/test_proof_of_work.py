"""
A cake cannot move through the shop on somebody's word.

"Baked" and "delivered" were buttons. Pressing one advanced the order, and the
shop's only record that a cake had been made, looked at, or handed to anybody
was that a button had been pressed. Three photographs close that:

    proof_image       the baker's shot of the finished cake
    proof_verified_at Shriya having looked at that shot
    handover_image    the doorstep, or the counter

The gates live in update_order_status rather than in the routes, because the
website, the admin table and the WhatsApp commands all land there and a rule
only one of them honours is not a rule. These tests pin that.
"""

import pytest
from fastapi import HTTPException

from app.models.order import Fulfilment, OrderStatus
from app.schemas import StatusUpdate
from app.services.order_service import update_order_status

from tests.conftest import make_order


def test_a_cake_cannot_be_offered_for_approval_unphotographed(db, customer, baker):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)

    with pytest.raises(HTTPException) as e:
        update_order_status(db, order.id, StatusUpdate(status="AWAITING_APPROVAL"))

    assert e.value.status_code == 400
    assert "photo" in str(e.value.detail).lower()


def test_a_photographed_cake_can_be_offered_for_approval(db, customer, baker):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION,
                       baker=baker, with_proof=False)
    order.proof_image = "/media/bake_1.jpg"
    db.commit()

    update_order_status(db, order.id, StatusUpdate(status="AWAITING_APPROVAL"))
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL


def test_an_unchecked_cake_cannot_be_packed(db, customer, baker):
    """The photo existing is not the same as somebody having looked at it."""
    order = make_order(db, customer, status=OrderStatus.AWAITING_APPROVAL,
                       baker=baker, with_proof=False)
    order.proof_image = "/media/bake_1.jpg"      # taken, never checked
    db.commit()

    with pytest.raises(HTTPException) as e:
        update_order_status(db, order.id, StatusUpdate(status="PACKAGED"))

    assert e.value.status_code == 400
    assert "checked" in str(e.value.detail).lower()


def test_a_delivery_cannot_be_closed_without_a_handover_photo(db, customer, rider):
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY,
                       rider=rider, with_proof=False)
    # Everything the earlier gates want, so only the handover is missing.
    order.proof_image = "/media/bake_1.jpg"
    db.commit()

    with pytest.raises(HTTPException) as e:
        update_order_status(db, order.id, StatusUpdate(status="DELIVERED"))

    assert e.value.status_code == 400
    assert "photo" in str(e.value.detail).lower()


def test_a_collection_closes_without_a_handover_photo(db, customer):
    """
    The counter is not the doorstep. The staff hand the cake over themselves,
    with the customer standing there, so a photograph records nothing nobody
    saw. Requiring one here had a worse effect than being pointless: the only
    camera for a handover lives on the rider screen, a collection never
    reaches a rider, so every pickup order was stuck at READY_FOR_PICKUP for
    good.
    """
    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment=Fulfilment.PICKUP.value, with_proof=False)
    assert order.handover_image is None

    update_order_status(db, order.id, StatusUpdate(status="COLLECTED"))

    db.refresh(order)
    assert order.status == OrderStatus.COLLECTED


def test_a_fully_evidenced_order_still_goes_all_the_way_through(db, customer, rider, baker):
    """The gates must not make the ordinary path impossible."""
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY,
                       rider=rider, baker=baker)

    update_order_status(db, order.id, StatusUpdate(status="DELIVERED"))
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERED
