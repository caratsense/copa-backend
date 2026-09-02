"""
Payment and pricing invariants.

The rule these all serve: money and cake only move when the system can prove a
genuine, correctly-attributed payment happened — or the order is COD.
"""

import hashlib

import pytest

from app.config import get_settings
from app.models.delivery import DeliveryZone
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.pricing import FlavorRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole

from tests.conftest import auth, make_order, make_user

settings = get_settings()


@pytest.fixture
def catalogue(db):
    """A premium cake, real size/flavour rules and one real delivery zone."""
    product = Product(name="Premium Cake", category="premium",
                      base_price=1000.0, is_available=True)
    db.add(product)
    db.add(SizeRule(name="1kg", multiplier=1.0, is_active=True))
    db.add(SizeRule(name="5kg", multiplier=5.0, is_active=True))
    db.add(FlavorRule(name="Belgian Chocolate", extra_cost=2000.0, is_active=True))
    db.add(DeliveryZone(area_name="Gomti Nagar", charge=150.0,
                        estimated_time=60, is_active=True))
    db.commit()
    db.refresh(product)
    return product


@pytest.fixture
def payu(monkeypatch):
    """PayU configured with known credentials so callbacks can be signed."""
    monkeypatch.setattr(settings, "PAYU_KEY", "testkey")
    monkeypatch.setattr(settings, "PAYU_SALT", "testsalt")
    monkeypatch.setattr(settings, "PAYU_ALLOW_DEMO_PAYMENTS", False)
    return settings


def payu_hash(status, email, firstname, productinfo, amount, txnid,
              salt="testsalt", key="testkey"):
    return hashlib.sha512("|".join([
        salt, status, "", "", "", "", "", "", "", "", "", "",
        email, firstname, productinfo, amount, txnid, key,
    ]).encode()).hexdigest()


def callback(client, order, txnid, amount, status="success", **over):
    payload = {
        "status": status, "txnid": txnid, "amount": amount,
        "email": "a@b.c", "firstname": "A", "productinfo": "p",
    }
    payload.update(over)
    payload.setdefault("hash", payu_hash(status, payload["email"], payload["firstname"],
                                         payload["productinfo"], amount, txnid))
    return client.post("/payments/payu-callback", data=payload, follow_redirects=False)


def online_order(db, customer, total=5000.0, **kw):
    o = make_order(db, customer, OrderStatus.CONFIRMED, **kw)
    o.payment_method = "ONLINE"
    o.payment_status = PaymentStatus.PENDING
    o.total_price = total
    db.commit()
    db.refresh(o)
    return o


# ─── FAIL CLOSED WITHOUT PAYU ────────────────────────

def test_online_payment_fails_closed_when_payu_is_unconfigured(client, db, customer, monkeypatch):
    """
    The shipped default marked every ONLINE order PAID for free. Refusing is
    the only safe behaviour when we cannot actually take money.
    """
    monkeypatch.setattr(settings, "PAYU_KEY", "")
    monkeypatch.setattr(settings, "PAYU_SALT", "")
    monkeypatch.setattr(settings, "PAYU_ALLOW_DEMO_PAYMENTS", False)

    order = online_order(db, customer)
    res = client.post("/payments/create-order", headers=auth(customer),
                      json={"order_id": order.id, "payment_method": "ONLINE"})

    assert res.status_code == 503
    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


def test_demo_payment_requires_an_explicit_opt_in(client, db, customer, monkeypatch):
    monkeypatch.setattr(settings, "PAYU_KEY", "")
    monkeypatch.setattr(settings, "PAYU_SALT", "")
    monkeypatch.setattr(settings, "PAYU_ALLOW_DEMO_PAYMENTS", True)

    order = online_order(db, customer)
    res = client.post("/payments/create-order", headers=auth(customer),
                      json={"order_id": order.id, "payment_method": "ONLINE"})

    assert res.status_code == 200
    assert res.json()["status"] == "demo_paid"


def test_callback_refuses_to_settle_while_payu_is_unconfigured(client, db, customer, monkeypatch):
    """An empty salt makes the reverse hash forgeable by anyone."""
    monkeypatch.setattr(settings, "PAYU_KEY", "")
    monkeypatch.setattr(settings, "PAYU_SALT", "")

    order = online_order(db, customer)
    txnid = f"CO{order.id}-x"
    forged = payu_hash("success", "a@b.c", "A", "p", "5000.00", txnid, salt="", key="")
    callback(client, order, txnid, "5000.00", hash=forged)

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


# ─── CALLBACK VERIFICATION ───────────────────────────

def test_forged_callback_with_bad_hash_is_rejected(client, db, customer, payu):
    order = online_order(db, customer)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    callback(client, order, order.payment_id, "5000.00", hash="0" * 128)

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


def test_callback_with_wrong_amount_is_rejected(client, db, customer, payu):
    """Rs 1 must not settle a Rs 5000 order."""
    order = online_order(db, customer, total=5000.0)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    callback(client, order, order.payment_id, "1.00")

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


def test_callback_with_someone_elses_txnid_is_rejected(client, db, customer, payu):
    order = online_order(db, customer)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    callback(client, order, f"CO{order.id}-attacker", "5000.00")

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


def test_genuine_callback_settles_the_order(client, db, customer, payu, catalogue):
    order = online_order(db, customer, total=5000.0)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    res = callback(client, order, order.payment_id, "5000.00")

    assert res.status_code == 303
    db.refresh(order)
    assert order.payment_status == PaymentStatus.PAID


def test_duplicate_callback_is_idempotent(client, db, customer, payu, catalogue):
    """PayU redelivers; the customer also refreshes the return page."""
    order = online_order(db, customer, total=5000.0)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    for _ in range(3):
        res = callback(client, order, order.payment_id, "5000.00")
        assert res.status_code == 303

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PAID


def test_failed_status_callback_does_not_settle(client, db, customer, payu):
    order = online_order(db, customer)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    callback(client, order, order.payment_id, "5000.00", status="failure")

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


# ─── PRODUCTION GATE ─────────────────────────────────

def test_unpaid_online_order_cannot_be_assigned_to_a_baker(client, db, admin, customer):
    baker = make_user(db, "Baker", UserRole.BAKER)
    order = online_order(db, customer)

    res = client.post(f"/admin/orders/{order.id}/assign-baker",
                      headers=auth(admin), json={"staff_id": baker.id})

    db.refresh(order)
    assert order.status == OrderStatus.CONFIRMED, "an unpaid order entered production"
    assert res.status_code >= 400


def test_cod_order_may_enter_production_unpaid(client, db, admin, customer):
    """COD is paid on delivery — it must still be baked."""
    baker = make_user(db, "Baker", UserRole.BAKER)
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    order.payment_method = "COD"
    order.payment_status = PaymentStatus.COD_PENDING
    db.commit()

    client.post(f"/admin/orders/{order.id}/assign-baker",
                headers=auth(admin), json={"staff_id": baker.id})

    db.refresh(order)
    assert order.status == OrderStatus.ASSIGNED


def test_paid_online_order_is_released_to_a_baker(client, db, customer, payu):
    make_user(db, "Baker", UserRole.BAKER)
    order = online_order(db, customer, total=5000.0)
    order.payment_id = f"CO{order.id}-genuine"
    db.commit()

    callback(client, order, order.payment_id, "5000.00")

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PAID
    assert order.assigned_baker_id is not None, "payment did not release the order"
    assert order.status == OrderStatus.ASSIGNED


# ─── PRICING INTEGRITY ───────────────────────────────

def test_mis_cased_customization_no_longer_discounts(client, db, customer, catalogue):
    """'belgian chocolate' + '5kg ' used to turn a Rs 7000 cake into Rs 1000."""
    exact = client.post("/pricing/calculate", json={
        "product_id": catalogue.id, "quantity": 1,
        "customization": {"size": "5kg", "flavor": "Belgian Chocolate",
                          "design": "", "addons": [], "rush": ""}})
    mangled = client.post("/pricing/calculate", json={
        "product_id": catalogue.id, "quantity": 1,
        "customization": {"size": "5kg ", "flavor": "belgian chocolate",
                          "design": "", "addons": [], "rush": ""}})

    assert exact.status_code == 200
    assert mangled.status_code == 200
    assert mangled.json()["total"] == exact.json()["total"] == 7000.0


def test_unknown_customization_is_rejected_not_priced_at_zero(client, db, catalogue):
    res = client.post("/pricing/calculate", json={
        "product_id": catalogue.id, "quantity": 1,
        "customization": {"size": "1kg", "flavor": "Belgian Chocolat",
                          "design": "", "addons": [], "rush": ""}})
    assert res.status_code == 400
    assert "flavour" in res.json()["detail"].lower()


def test_blank_customization_is_free_and_allowed(client, db, catalogue):
    res = client.post("/pricing/calculate", json={
        "product_id": catalogue.id, "quantity": 1,
        "customization": {"size": "", "flavor": "", "design": "", "addons": [], "rush": ""}})
    assert res.status_code == 200
    assert res.json()["total"] == 1000.0


def test_unknown_delivery_zone_is_rejected(client, db, customer, catalogue):
    res = client.post("/orders", headers=auth(customer), json={
        "items": [{"product_id": catalogue.id, "quantity": 1,
                   "customization": {"size": "1kg", "flavor": "Belgian Chocolate",
                                     "design": "", "addons": [], "rush": ""}}],
        "delivery_address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar Typo",
    })
    assert res.status_code == 400
    assert "deliver" in res.json()["detail"].lower()


def test_known_delivery_zone_is_charged(client, db, customer, catalogue):
    res = client.post("/orders", headers=auth(customer), json={
        "items": [{"product_id": catalogue.id, "quantity": 1,
                   "customization": {"size": "1kg", "flavor": "Belgian Chocolate",
                                     "design": "", "addons": [], "rush": ""}}],
        "delivery_address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
    })
    assert res.status_code == 201
    assert res.json()["delivery_charge"] == 150.0


@pytest.mark.parametrize("quantity", [0, -5])
def test_invalid_quantity_is_rejected(client, db, customer, catalogue, quantity):
    res = client.post("/orders", headers=auth(customer), json={
        "items": [{"product_id": catalogue.id, "quantity": quantity,
                   "customization": {"size": "1kg", "flavor": "Belgian Chocolate",
                                     "design": "", "addons": [], "rush": ""}}],
        "delivery_address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
    })
    assert res.status_code == 422


def test_order_with_no_items_is_rejected(client, db, customer):
    res = client.post("/orders", headers=auth(customer), json={
        "items": [], "delivery_address": "Somewhere",
    })
    assert res.status_code == 422


def test_checkout_preview_and_order_total_agree(client, db, customer, catalogue):
    """The quote and the charge must come from the same authority."""
    body = {"size": "5kg", "flavor": "Belgian Chocolate",
            "design": "", "addons": [], "rush": ""}

    preview = client.post("/pricing/calculate", json={
        "product_id": catalogue.id, "quantity": 2,
        "customization": body, "delivery_zone": "Gomti Nagar"}).json()

    created = client.post("/orders", headers=auth(customer), json={
        "items": [{"product_id": catalogue.id, "quantity": 2, "customization": body}],
        "delivery_address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
    }).json()

    assert created["subtotal"] == preview["total"] + 150.0
    assert created["total_price"] == created["subtotal"]


# ─── OWNERSHIP ───────────────────────────────────────

def test_customer_cannot_pay_for_someone_elses_order(client, db, customer, other_customer):
    order = online_order(db, customer)
    res = client.post("/payments/create-order", headers=auth(other_customer),
                      json={"order_id": order.id, "payment_method": "COD"})
    assert res.status_code == 403


# ─── TWO CHECKOUT TABS ───────────────────────────────
# A customer opening checkout twice used to lose their payment: every call to
# /payments/create-order minted a fresh txnid and overwrote the column, so the
# first tab's genuine, correctly signed callback no longer matched the order and
# was refused. Money taken, order left PENDING.

def test_a_second_checkout_tab_reuses_the_pending_txnid(client, db, customer, payu, catalogue):
    order = online_order(db, customer)

    first = client.post("/payments/create-order", headers=auth(customer),
                        json={"order_id": order.id, "payment_method": "ONLINE"})
    assert first.status_code == 200
    txnid_1 = first.json()["params"]["txnid"]

    second = client.post("/payments/create-order", headers=auth(customer),
                         json={"order_id": order.id, "payment_method": "ONLINE"})
    assert second.status_code == 200
    txnid_2 = second.json()["params"]["txnid"]

    assert txnid_1 == txnid_2, "a second tab minted a new txnid and orphaned the first"


def test_paying_in_the_first_tab_still_settles_the_order(client, db, customer, payu, catalogue):
    """The exact sequence that used to lose a real payment."""
    order = online_order(db, customer)

    opened_first = client.post("/payments/create-order", headers=auth(customer),
                               json={"order_id": order.id, "payment_method": "ONLINE"})
    txnid = opened_first.json()["params"]["txnid"]

    # ...customer opens checkout again in another tab...
    client.post("/payments/create-order", headers=auth(customer),
                json={"order_id": order.id, "payment_method": "ONLINE"})

    # ...then completes the payment back in the FIRST tab.
    callback(client, order, txnid, f"{order.total_price:.2f}")

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PAID, \
        "a genuine payment from the first tab was refused"


def test_a_foreign_txnid_still_cannot_settle_this_order(client, db, customer, payu, catalogue):
    """The identity check must survive the reuse change."""
    order = online_order(db, customer)
    client.post("/payments/create-order", headers=auth(customer),
                json={"order_id": order.id, "payment_method": "ONLINE"})

    callback(client, order, f"CO{order.id}-forged", f"{order.total_price:.2f}")

    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING
