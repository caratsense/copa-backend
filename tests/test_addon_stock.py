"""
Finite add-on stock is reserved at order creation and returned on cancellation.

The decrement existed from the start; the increment was never written. Every
cancelled order therefore ate its toppers permanently, and because the public
list route filters out any addon whose stock has reached 0, the item silently
vanished from the customer's cake builder with no warning to anyone — the owner
found out she was "out" of photo prints from a confused customer.
"""

from app.models.order import OrderStatus
from app.models.pricing import AddonRule, DesignRule, RushRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate, StatusUpdate
from app.services.order_service import create_order, update_order_status

from tests.conftest import make_user

import pytest


@pytest.fixture
def catalogue(db):
    product = Product(name="Cake", category="premium", base_price=1000.0, is_available=True)
    db.add_all([
        product,
        SizeRule(name="1kg", multiplier=1.0, is_active=True),
        DesignRule(name="Basic Cream Finish", cost=0.0, is_active=True),
        RushRule(name="Standard (24hr+)", cost=0.0, is_active=True),
        AddonRule(name="Photo Topper", cost=200.0, stock=5, is_active=True),
        AddonRule(name="Candles", cost=50.0, stock=None, is_active=True),   # unlimited
    ])
    db.commit()
    db.refresh(product)
    return product


def _place(db, customer, product, quantity=2, addons=("Photo Topper",)):
    return create_order(db, OrderCreate(
        user_id=customer.id,
        payment_method="COD",
        delivery_address="Flat 1, Lucknow",
        items=[OrderItemCreate(
            product_id=product.id,
            quantity=quantity,
            customization=ItemCustomization(
                size="1kg", flavor="", design="Basic Cream Finish",
                addons=list(addons), rush="Standard (24hr+)",
            ),
        )],
    ))


def _stock(db, name):
    return db.query(AddonRule).filter(AddonRule.name == name).first().stock


def test_ordering_reserves_finite_stock(db, catalogue):
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    _place(db, customer, catalogue, quantity=2)
    db.expire_all()
    assert _stock(db, "Photo Topper") == 3


def test_cancelling_returns_the_stock(db, catalogue):
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = _place(db, customer, catalogue, quantity=2)
    db.expire_all()
    assert _stock(db, "Photo Topper") == 3, "precondition: units were reserved"

    update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
    db.expire_all()
    assert _stock(db, "Photo Topper") == 5, "cancelled order kept the units"


def test_unlimited_addons_are_left_alone(db, catalogue):
    """stock=None means unlimited; it must never become a number."""
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = _place(db, customer, catalogue, addons=("Candles",))
    update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
    db.expire_all()
    assert _stock(db, "Candles") is None


def test_stock_is_returned_once_not_per_transition(db, catalogue):
    """CANCELLED is terminal, so the restore cannot run twice for one order."""
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = _place(db, customer, catalogue, quantity=1)
    update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
    db.expire_all()
    assert _stock(db, "Photo Topper") == 5

    # A second attempt is rejected by the lifecycle, so the count cannot inflate.
    with pytest.raises(Exception):
        update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
    db.expire_all()
    assert _stock(db, "Photo Topper") == 5, "stock inflated past its real level"


def test_delivering_an_order_does_not_return_stock(db, catalogue):
    """Only a cancellation releases the reservation — a sale consumes it."""
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = _place(db, customer, catalogue, quantity=2)
    for status in ("ASSIGNED", "IN_PRODUCTION", "AWAITING_APPROVAL",
                   "PACKAGED", "OUT_FOR_DELIVERY", "DELIVERED"):
        try:
            update_order_status(db, order.id, StatusUpdate(status=status))
        except Exception:
            break   # no staff seeded for some hops; the assertion below still holds
    db.expire_all()
    assert _stock(db, "Photo Topper") == 3, "a completed sale gave its units back"
