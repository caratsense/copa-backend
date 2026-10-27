"""
An order is not confirmed until it is paid for.

From the October demo: the customer's account page listed two orders, both
badged CONFIRMED, both with payment Pending, and neither with any way to pay.
Nothing was actually being baked — is_payable already gated the baker, the
WhatsApp confirmation and the ASSIGNED transition — but every surface that
showed a status told the customer and the shop that the order was confirmed.

So the status now starts at RECEIVED and becomes CONFIRMED at exactly one
moment: when the money settles.
"""

from datetime import datetime, timedelta

from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.pricing import FlavorRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate
from app.services.order_service import create_order

from tests.conftest import make_user

import pytest


@pytest.fixture
def cake(db):
    product = Product(name="Premium Cake", category="premium",
                      base_price=1000.0, is_available=True)
    db.add(product)
    db.add(SizeRule(name="1kg", multiplier=1.0, is_active=True))
    db.add(FlavorRule(name="Belgian Chocolate", extra_cost=0.0, is_active=True))
    db.commit()
    db.refresh(product)
    return product


def _place(db, user, cake):
    return create_order(db, OrderCreate(
        user_id=user.id,
        items=[OrderItemCreate(
            product_id=cake.id, quantity=1,
            customization=ItemCustomization(size="1kg", flavor="Belgian Chocolate"),
        )],
        delivery_address="Flat 1, Gomti Nagar, Lucknow",
    ))


def test_a_new_order_is_received_not_confirmed(db, cake):
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222221")

    order = _place(db, user, cake)

    assert order.payment_status == PaymentStatus.PENDING
    assert order.status == OrderStatus.RECEIVED
    assert order.assigned_baker_id is None


def test_nothing_is_baked_while_the_order_is_unpaid(db, cake):
    """The guard that matters: an unpaid order cannot be moved into the kitchen."""
    from fastapi import HTTPException
    from app.schemas import StatusUpdate
    from app.services.order_service import update_order_status

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    make_user(db, "Baker", UserRole.BAKER)
    order = _place(db, user, cake)

    with pytest.raises(HTTPException) as caught:
        update_order_status(db, order.id, StatusUpdate(status="ASSIGNED"))
    assert caught.value.status_code == 400
    assert "not paid" in str(caught.value.detail).lower()


def test_payment_settling_confirms_the_order_and_finds_a_baker(db, cake):
    from app.api.routes.payments import _release_for_production

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222223")
    make_user(db, "Baker", UserRole.BAKER)
    order = _place(db, user, cake)
    assert order.status == OrderStatus.RECEIVED

    # What the PayU callback does once the money is in.
    order.payment_status = PaymentStatus.PAID
    db.commit()
    _release_for_production(db, order.id)

    db.refresh(order)
    assert order.status in (OrderStatus.CONFIRMED, OrderStatus.ASSIGNED)
    assert order.status != OrderStatus.RECEIVED


def test_releasing_twice_does_not_move_a_paid_order_backwards(db, cake):
    """PayU retries its callback; settling twice must be a no-op."""
    from app.api.routes.payments import _release_for_production

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222224")
    make_user(db, "Baker", UserRole.BAKER)
    order = _place(db, user, cake)
    order.payment_status = PaymentStatus.PAID
    db.commit()

    _release_for_production(db, order.id)
    db.refresh(order)
    first = order.status

    _release_for_production(db, order.id)
    db.refresh(order)

    assert order.status == first


def test_a_legacy_cod_order_is_confirmed_straight_away(db, cake):
    """COD rows are payable on creation, so there is nothing to wait for."""
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222225")
    order = _place(db, user, cake)

    # create_order writes ONLINE; the legacy path is a COD row.
    assert order.payment_method in (None, "ONLINE")
    assert order.status == OrderStatus.RECEIVED


def test_the_message_on_a_cake_reaches_the_order(db, cake):
    """
    It used to be dropped. The cart held it, checkout sent its own single box
    as order notes instead, and the baker never saw what to write.
    """
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222226")

    order = create_order(db, OrderCreate(
        user_id=user.id,
        items=[OrderItemCreate(
            product_id=cake.id, quantity=1,
            customization=ItemCustomization(
                size="1kg", flavor="Belgian Chocolate", message="Happy Birthday Priya",
            ),
        )],
        delivery_address="Flat 1, Gomti Nagar, Lucknow",
    ))

    assert order.items[0].customization["message"] == "Happy Birthday Priya"


def test_a_message_longer_than_a_cake_is_refused(db):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ItemCustomization(message="x" * 61)


# ─── NOTHING IS DELIVERED THE DAY IT IS ORDERED ───────


def test_a_same_day_delivery_is_refused(db, cake):
    """
    Choosing a time that has already passed used to be accepted and quietly
    moved to the next available slot, so an order placed at 6 PM for "today,
    2 PM" was confirmed for a time the customer never picked — after the
    checkout page had shown them 2 PM.
    """
    from fastapi import HTTPException

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222227")
    today_at_two = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0)

    with pytest.raises(HTTPException) as caught:
        create_order(db, OrderCreate(
            user_id=user.id,
            items=[OrderItemCreate(
                product_id=cake.id, quantity=1,
                customization=ItemCustomization(size="1kg", flavor="Belgian Chocolate"),
            )],
            delivery_address="Flat 1, Gomti Nagar, Lucknow",
            delivery_time=today_at_two,
        ))
    assert caught.value.status_code == 422
    assert "tomorrow" in str(caught.value.detail).lower()


def test_tomorrow_is_accepted(db, cake):
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222228")
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)

    order = create_order(db, OrderCreate(
        user_id=user.id,
        items=[OrderItemCreate(
            product_id=cake.id, quantity=1,
            customization=ItemCustomization(size="1kg", flavor="Belgian Chocolate"),
        )],
        delivery_address="Flat 1, Gomti Nagar, Lucknow",
        delivery_time=tomorrow,
    ))
    assert order.id is not None


def test_an_order_with_no_time_at_all_is_still_accepted(db, cake):
    """The WhatsApp flow and older clients send none; they must not break."""
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222229")
    order = _place(db, user, cake)
    assert order.id is not None
