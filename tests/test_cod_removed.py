"""
Cash on delivery is withdrawn, and nothing tells a customer otherwise.

COD was made unorderable earlier: POST /payments/create-order refuses any
method but ONLINE, and OrderCreate has no payment_method field at all. What
survived was copy - a 503 that suggested paying cash when the card path was
down, and a WhatsApp prompt that listed COD among the payment options. Both
promised a route the API would then reject, which is worse than not offering it
at all.

What deliberately survives is everything to do with orders placed WHILE COD
existed: the COD_PENDING enum value (PostgreSQL cannot drop one, and a row
holding it must still load), and is_payable()'s grandfather clause, without
which an unpaid legacy COD order could never reach the kitchen.
"""

from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.user import UserRole
from app.services.order_service import is_payable

from tests.conftest import auth, make_order, make_user

import pytest


COD_WORDS = ("cash on delivery", "cash-on-delivery", "cod")


def _mentions_cod(text: str) -> bool:
    """Case-insensitive, and 'cod' only as a whole word - 'code' is not COD."""
    import re
    lowered = (text or "").lower()
    return any(
        re.search(rf"\b{re.escape(word)}\b", lowered) for word in COD_WORDS
    )


def test_the_matcher_would_actually_catch_cod_wording():
    """Guard the guard: a test that cannot fail proves nothing."""
    assert _mentions_cod("Please choose Cash on Delivery or try again shortly.")
    assert _mentions_cod("Payment: Online (UPI/Card) or Cash on Delivery")
    assert _mentions_cod("payment_method=COD")
    assert not _mentions_cod("Please try again shortly or contact us on WhatsApp.")
    assert not _mentions_cod("Enter your discount code")


# ─── 1. THE PAYU-UNCONFIGURED RESPONSE ───────────────

def test_payu_unconfigured_response_does_not_suggest_cod(client, db, monkeypatch):
    """
    With PayU unset the API fails closed - correctly - but it used to tell the
    customer to pay cash instead, twelve lines after refusing exactly that.
    """
    from app.api.routes import payments as payments_routes

    monkeypatch.setattr(payments_routes.settings, "PAYU_KEY", "")
    monkeypatch.setattr(payments_routes.settings, "PAYU_SALT", "")
    monkeypatch.setattr(payments_routes.settings, "PAYU_ALLOW_DEMO_PAYMENTS", False)

    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PENDING
    db.commit()

    res = client.post("/payments/create-order", headers=auth(customer),
                      json={"order_id": order.id})

    assert res.status_code == 503
    detail = res.json()["detail"]
    assert not _mentions_cod(detail), f"still offers COD: {detail!r}"
    assert "try again" in detail.lower()


def test_payu_unconfigured_still_fails_closed(client, db, monkeypatch):
    """Removing the COD wording must not turn the refusal into a free cake."""
    from app.api.routes import payments as payments_routes

    monkeypatch.setattr(payments_routes.settings, "PAYU_KEY", "")
    monkeypatch.setattr(payments_routes.settings, "PAYU_SALT", "")
    monkeypatch.setattr(payments_routes.settings, "PAYU_ALLOW_DEMO_PAYMENTS", False)

    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    order.payment_status = PaymentStatus.PENDING
    db.commit()

    client.post("/payments/create-order", headers=auth(customer),
                json={"order_id": order.id})

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING, "an unpaid order was marked paid"


# ─── 2. THE WHATSAPP BUSINESS CONTEXT ────────────────

def test_whatsapp_business_info_offers_online_payment_only():
    from app.services.gemini_parser import business_info

    info = business_info()
    assert not _mentions_cod(info), f"the assistant still offers COD: {info!r}"
    assert "Online (UPI/Card)" in info


def test_the_assembled_whatsapp_prompt_contains_no_cod(monkeypatch):
    """
    The rendered prompt, not just the helper - so moving the wording back into
    the f-string would still fail this.
    """
    import app.services.gemini_parser as parser

    captured = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"action":"WELCOME"}'}}]}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["system"] = json["messages"][0]["content"]
        return _Resp()

    monkeypatch.setattr(parser.settings, "GROQ_API_KEY", "test-key")
    monkeypatch.setattr(parser.httpx, "post", _fake_post)

    parser._groq_parse("hello", "customer", {"step": "IDLE", "products": []})

    assert captured, "the prompt was never assembled"
    assert not _mentions_cod(captured["system"]), "COD reached the assistant's prompt"
    assert "Payment: Online (UPI/Card), in advance" in captured["system"]


# ─── 3/4. WHAT MUST NOT CHANGE ───────────────────────

def test_a_new_cod_payment_attempt_is_still_rejected(client, db):
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    db.commit()

    res = client.post("/payments/create-order", headers=auth(customer),
                      json={"order_id": order.id, "payment_method": "COD"})

    assert res.status_code == 400
    assert "no longer available" in res.json()["detail"].lower()


def test_a_new_order_defaults_to_online(db):
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = Order(user_id=customer.id, subtotal=100.0, total_price=100.0)
    db.add(order)
    db.commit()
    db.refresh(order)

    assert order.payment_method == "ONLINE"


def test_cod_pending_remains_in_the_enum():
    """PostgreSQL cannot drop an enum value, and a legacy row must still load."""
    assert PaymentStatus.COD_PENDING.value == "COD_PENDING"


def test_a_legacy_cod_order_remains_readable_and_payable(db):
    """
    is_payable()'s grandfather clause. Without it an unpaid legacy COD order
    could never reach a baker, stranding work the bakery already committed to.
    """
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    order.payment_method = "COD"
    order.payment_status = PaymentStatus.COD_PENDING
    db.commit()
    db.refresh(order)

    assert order.payment_method == "COD"
    assert order.payment_status == PaymentStatus.COD_PENDING
    assert is_payable(order) is True


def test_an_unpaid_online_order_is_still_not_payable(db):
    """The grandfather clause must not have widened into a free pass."""
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PENDING
    db.commit()

    assert is_payable(order) is False
