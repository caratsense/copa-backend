"""
WhatsApp orders must be charged for delivery like any other order.

The conversation collected a free-text address and never established a delivery
zone, so every order it created was written with delivery_zone=None. A blank
zone means "self pickup" to the pricing engine, so every cake ordered over
WhatsApp shipped free — the charge was simply never applied.

An earlier fix passed state["delivery_zone"] through to create_order, which
looked correct but changed nothing: no step in the flow ever set that key.
These tests pin the actual behaviour rather than the wiring.
"""

from app.models.delivery import DeliveryZone
from app.models.order import Order
from app.models.pricing import DesignRule, FlavorRule, RushRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.services import wa_customer_flow as wcf
from app.services.wa_customer_flow import handle_customer_message

from tests.conftest import make_user

import pytest

PHONE = "919222222222"


@pytest.fixture
def catalogue(db):
    product = Product(name="Premium Cake", category="premium",
                      base_price=1000.0, is_available=True)
    db.add_all([
        product,
        SizeRule(name="1kg", multiplier=1.0, is_active=True),
        FlavorRule(name="Belgian Chocolate", extra_cost=200.0, is_active=True),
        DesignRule(name="Basic Cream Finish", cost=0.0, is_active=True),
        RushRule(name="Standard (24hr+)", cost=0.0, is_active=True),
        DeliveryZone(area_name="Gomti Nagar", charge=150.0,
                     estimated_time=45, is_active=True),
        DeliveryZone(area_name="Hazratganj", charge=90.0,
                     estimated_time=30, is_active=True),
    ])
    db.commit()
    db.refresh(product)
    return product


@pytest.fixture
def customer(db):
    u = make_user(db, "Priya", UserRole.CUSTOMER, phone="+91" + PHONE[2:])
    u.whatsapp_opt_in = True
    db.commit()
    return u


def _at_address_step(product):
    """Drop the conversation straight onto the address question."""
    wcf._set_state(PHONE, {
        "step": "DELIVERY_ADDRESS",
        "product": {"id": product.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 1200,
    })


def test_address_step_asks_which_area(db, catalogue, customer):
    """It used to jump straight to the date, never establishing a zone."""
    _at_address_step(catalogue)
    reply = handle_customer_message(PHONE, "Flat 1, near the park", customer)

    assert "area" in reply.lower(), f"never asked for an area: {reply!r}"
    assert "Gomti Nagar" in reply and "Hazratganj" in reply, "did not list the zones"
    assert "150" in reply, "did not show what delivery costs"


def test_choosing_an_area_records_it(db, catalogue, customer):
    _at_address_step(catalogue)
    handle_customer_message(PHONE, "Flat 1, near the park", customer)
    handle_customer_message(PHONE, "1", customer)

    assert wcf._get_state(PHONE).get("delivery_zone") == "Gomti Nagar"


def test_the_area_name_is_accepted_too(db, catalogue, customer):
    """People reply with the name as often as the number."""
    _at_address_step(catalogue)
    handle_customer_message(PHONE, "Flat 1, near the park", customer)
    handle_customer_message(PHONE, "Hazratganj", customer)

    assert wcf._get_state(PHONE).get("delivery_zone") == "Hazratganj"


def test_a_nonsense_answer_re_asks_rather_than_proceeding(db, catalogue, customer):
    _at_address_step(catalogue)
    handle_customer_message(PHONE, "Flat 1, near the park", customer)
    reply = handle_customer_message(PHONE, "somewhere over there", customer)

    assert "area" in reply.lower() or "pick" in reply.lower()
    assert wcf._get_state(PHONE).get("step") == "SELECT_AREA", "moved on without a zone"


def test_the_order_is_actually_charged_for_delivery(db, catalogue, customer, wa_secret):
    """The point of the whole exercise."""
    _at_address_step(catalogue)
    handle_customer_message(PHONE, "Flat 1, near the park", customer)
    handle_customer_message(PHONE, "1", customer)                  # Gomti Nagar

    state = wcf._get_state(PHONE)
    wcf._set_state(PHONE, {**state, "step": "CONFIRM",
                           "delivery_date": "2026-12-25", "time_hours": 14})
    handle_customer_message(PHONE, "CONFIRM", customer)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order is not None, "no order was created"
    assert order.delivery_charge == 150.0, \
        f"WhatsApp order shipped for {order.delivery_charge} instead of 150"
    # 1000 base + 200 flavour + 150 delivery
    assert order.total_price == 1350.0


def test_self_pickup_is_still_free(db, catalogue, customer, wa_secret):
    """Pickup must skip the area question entirely, not be charged for it."""
    _at_address_step(catalogue)
    reply = handle_customer_message(PHONE, "pickup", customer)

    assert "area" not in reply.lower(), "asked a pickup customer for a delivery area"
    assert wcf._get_state(PHONE).get("step") == "SELECT_DATE"

    state = wcf._get_state(PHONE)
    wcf._set_state(PHONE, {**state, "step": "CONFIRM",
                           "delivery_date": "2026-12-25", "time_hours": 14})
    handle_customer_message(PHONE, "CONFIRM", customer)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.delivery_charge == 0.0
