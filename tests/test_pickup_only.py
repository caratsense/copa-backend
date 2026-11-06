"""
Pickup-only mode: the delivery_enabled site setting.

"false" means the shop is not delivering for a while - the client's "for a
few days I only want to do pickup". The checkout greys Delivery out, and the
website's POST /orders refuses a delivery order so a page loaded before the
switch flipped cannot place one.

The gate lives on the ROUTE, not in create_order: the WhatsApp flow builds
its orders through the service and says its refusals inside the conversation,
so the service staying permissive is load-bearing, and these tests pin it.
"""

import pytest

from app.models.product import Product
from app.models.site_settings import SiteSettings
from app.schemas import OrderCreate, OrderItemCreate, ItemCustomization
from app.services.order_service import create_order

from tests.conftest import auth


@pytest.fixture
def cake(db):
    product = Product(name="Plain Cake", category="cakes",
                      base_price=1000.0, is_available=True)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _set_delivery(db, value: str | None):
    row = db.query(SiteSettings).filter(SiteSettings.key == "delivery_enabled").first()
    if value is None:
        if row:
            db.delete(row)
    elif row:
        row.value = value
    else:
        db.add(SiteSettings(key="delivery_enabled", value=value))
    db.commit()


def _order(client, customer, cake, fulfilment):
    body = {
        "items": [{"product_id": cake.id, "quantity": 1,
                   "customization": {"size": "", "flavor": "", "design": "",
                                     "addons": [], "rush": ""}}],
        "fulfilment": fulfilment,
    }
    if fulfilment == "DELIVERY":
        body["delivery_address"] = "12 Hazratganj, Lucknow"
    return client.post("/orders", headers=auth(customer), json=body)


# ─── THE GATE ─────────────────────────────────────────

def test_delivery_is_refused_while_pickup_only(client, db, customer, cake):
    _set_delivery(db, "false")
    res = _order(client, customer, cake, "DELIVERY")
    assert res.status_code == 409
    assert "pickup" in res.json()["detail"].lower()


def test_pickup_still_goes_through(client, db, customer, cake):
    _set_delivery(db, "false")
    res = _order(client, customer, cake, "PICKUP")
    assert res.status_code == 201
    assert res.json()["fulfilment"] == "PICKUP"


def test_missing_setting_means_delivering(client, db, customer, cake):
    # Every deployment predates this switch; none of them may stop delivering
    # because of an upgrade.
    _set_delivery(db, None)
    assert _order(client, customer, cake, "DELIVERY").status_code == 201


def test_true_means_delivering(client, db, customer, cake):
    _set_delivery(db, "true")
    assert _order(client, customer, cake, "DELIVERY").status_code == 201


def test_flipping_back_on_needs_no_restart(client, db, customer, cake):
    # The setting is read per request, not cached at startup - the admin
    # toggle has to take effect immediately in both directions.
    _set_delivery(db, "false")
    assert _order(client, customer, cake, "DELIVERY").status_code == 409
    _set_delivery(db, "true")
    assert _order(client, customer, cake, "DELIVERY").status_code == 201


# ─── WHAT THE GATE MUST NOT TOUCH ─────────────────────

def test_the_service_itself_stays_permissive(db, customer, cake):
    # The WhatsApp flow calls create_order directly and handles pickup-only
    # inside the conversation. If this ever starts failing, the gate has
    # slipped from the route into the service and broken the bot mid-chat.
    _set_delivery(db, "false")
    order = create_order(db, OrderCreate(
        user_id=customer.id,
        items=[OrderItemCreate(product_id=cake.id, quantity=1,
                               customization=ItemCustomization())],
        fulfilment="DELIVERY",
        delivery_address="12 Hazratganj, Lucknow",
    ))
    assert order.fulfilment == "DELIVERY"


# ─── WHAT THE CHECKOUT READS ──────────────────────────

def test_the_setting_is_publicly_readable(client, db):
    _set_delivery(db, "false")
    res = client.get("/settings/public/delivery_enabled")
    assert res.status_code == 200
    assert res.json()["value"] == "false"
