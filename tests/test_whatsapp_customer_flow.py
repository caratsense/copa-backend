"""
Customer-facing WhatsApp flow: ownership, honest pricing, honest payment state.

Scope is safety only — the conversational behaviour itself is unchanged.
"""

from app.models.delivery import DeliveryZone
from app.models.order import OrderStatus, PaymentStatus
from app.models.pricing import FlavorRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.services import wa_customer_flow as wcf
from app.services.wa_customer_flow import handle_customer_message

from tests.conftest import make_order, make_user

import pytest


def order_id(db):
    """The id of the most recently created order."""
    from app.models.order import Order
    return db.query(Order).order_by(Order.id.desc()).first().id


@pytest.fixture(autouse=True)
def clean_chat_state(fake_redis):
    """The flow keeps conversation state in Redis; start each test fresh."""
    yield


@pytest.fixture
def catalogue(db):
    product = Product(name="Premium Cake", category="premium",
                      base_price=1000.0, is_available=True)
    db.add(product)
    db.add(SizeRule(name="1kg", multiplier=1.0, is_active=True))
    db.add(FlavorRule(name="Belgian Chocolate", extra_cost=2000.0, is_active=True))
    db.add(DeliveryZone(area_name="Gomti Nagar", charge=150.0,
                        estimated_time=60, is_active=True))
    db.commit()
    db.refresh(product)
    return product


# ─── OWNERSHIP ───────────────────────────────────────

def test_customer_cannot_read_another_customers_order(db):
    """
    Order status used to be looked up by id alone, so any WhatsApp number could
    read a stranger's items, amount and status by guessing a number.
    """
    owner = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    rival = make_user(db, "Rival", UserRole.CUSTOMER, phone="+919777777777")
    order = make_order(db, owner, OrderStatus.OUT_FOR_DELIVERY)

    wcf._set_state("919777777777", {"step": "AWAITING_STATUS_ID"})
    reply = handle_customer_message("919777777777", str(order.id), rival)

    assert f"*Order #{order.id}*" not in reply
    assert "Amount" not in reply
    assert "couldn't find" in reply.lower()


def test_customer_can_read_their_own_order(db):
    owner = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, owner, OrderStatus.OUT_FOR_DELIVERY)

    wcf._set_state("919222222222", {"step": "AWAITING_STATUS_ID"})
    reply = handle_customer_message("919222222222", str(order.id), owner)

    assert f"*Order #{order.id}*" in reply
    assert "Amount" in reply


def test_missing_and_forbidden_orders_are_indistinguishable(db):
    """Otherwise the reply confirms which order numbers exist."""
    owner = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    rival = make_user(db, "Rival", UserRole.CUSTOMER, phone="+919777777777")
    real = make_order(db, owner, OrderStatus.CONFIRMED)

    wcf._set_state("919777777777", {"step": "AWAITING_STATUS_ID"})
    forbidden = handle_customer_message("919777777777", str(real.id), rival)

    wcf._set_state("919777777777", {"step": "AWAITING_STATUS_ID"})
    missing = handle_customer_message("919777777777", "999999", rival)

    assert forbidden.replace(str(real.id), "N") == missing.replace("999999", "N")


# ─── HONEST PAYMENT STATE ────────────────────────────

def test_whatsapp_order_is_not_presented_as_paid(db, catalogue, wa_secret):
    """
    The flow used to reply "Order #N — Confirmed" with an amount and no payment
    step at all, which reads as settled while nothing had been collected.
    """
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3000,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    reply = handle_customer_message("919222222222", "CONFIRM", user)

    assert "Payment: pending" in reply
    assert "Confirmed" not in reply
    # order_id is the parameter the orders page actually reads (it also accepts
    # `success`, which PayU uses on return). `order` was silently ignored, so
    # the customer landed on a bare list with nothing highlighted.
    assert f"/orders?order_id={order_id(db)}" in reply, "no handoff to the real payment flow"


def test_whatsapp_order_is_priced_by_the_pricing_engine(db, catalogue, wa_secret):
    """The quoted amount must be the order's real total, not the chat state's."""
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 1,                      # deliberately wrong chat-state total
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    reply = handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    # 1000 base + 2000 flavour + 150 delivery
    assert order.total_price == 3150.0
    assert "3,150.00" in reply, f"quoted the chat state instead of the order: {reply!r}"
    assert "Rs 1.00" not in reply


def test_whatsapp_order_charges_delivery(db, catalogue, wa_secret):
    """delivery_zone was never passed, so every WhatsApp order shipped free."""
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3000,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.delivery_charge == 150.0


def test_whatsapp_order_does_not_enter_production_unpaid(db, catalogue, wa_secret):
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    make_user(db, "Baker", UserRole.BAKER)
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3150,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.payment_status == PaymentStatus.PENDING
    assert order.status == OrderStatus.CONFIRMED
    assert order.assigned_baker_id is None, "unpaid WhatsApp order went to a baker"


def test_admin_without_opt_in_is_not_messaged_by_the_whatsapp_flow(db, catalogue, wa_secret):
    """The flow used to call notify_admin_new_order directly, skipping consent."""
    from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus

    admin = make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    admin.whatsapp_opt_in = False
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3150,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    admin_rows = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.recipient_role == "admin").all()
    assert all(m.status == WhatsAppMessageStatus.SKIPPED for m in admin_rows), \
        "messaged an admin who never opted in"
