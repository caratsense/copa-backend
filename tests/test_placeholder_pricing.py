"""
Products the client has not priced yet, and how they stop being that.

The catalogue has to be demonstrable and editable in the admin before every
price is in, so an unpriced product is created at a placeholder price of Rs 1
and tagged. Rs 1 is not a price - it is the absence of one, written down - and
the tag, not the number, is what says so: a cake may one day genuinely cost a
rupee, and a rule that reads the number could not tell the two apart.

What keeps Rs 1 from ever being charged is that a tagged product cannot be put
on sale. That is a refusal in the API rather than a convention, because one
click on the availability toggle is otherwise all it would take. Setting a real
price clears the tag in the same request, so the normal admin flow - fill in
the price, publish - just works.
"""

from app.api.routes.products import PLACEHOLDER_PRICE, PLACEHOLDER_PRICE_TAG
from app.models.product import Product

from tests.conftest import auth

import pytest


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """Exercise the guards, not the global 100/minute throttle."""
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


def _placeholder(db, name="Unpriced Cake", **kwargs):
    """A product as the catalogue script creates one before pricing."""
    product = Product(
        name=name, category="test", base_price=PLACEHOLDER_PRICE,
        pricing_unit=kwargs.pop("pricing_unit", "fixed"),
        is_customizable=False, is_available=False,
        tags=[PLACEHOLDER_PRICE_TAG], **kwargs,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _confirmed(db, name="Priced Cake", price=750.0):
    product = Product(name=name, category="test", base_price=price,
                      pricing_unit="fixed", is_customizable=False,
                      is_available=False, tags=[])
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


# ─── PLACEHOLDER IS DISTINGUISHABLE FROM A PRICE ─────

def test_a_placeholder_is_identified_by_its_tag_not_its_number(db):
    """A product that genuinely costs Rs 1 is not a placeholder."""
    from app.api.routes.products import _is_placeholder_priced

    placeholder = _placeholder(db)
    genuinely_one_rupee = _confirmed(db, name="Sample Bite", price=1.0)

    assert _is_placeholder_priced(placeholder) is True
    assert _is_placeholder_priced(genuinely_one_rupee) is False


def test_the_placeholder_is_visible_to_the_admin_listing(client, admin, db):
    """The whole point: the catalogue can be seen and managed before pricing."""
    _placeholder(db)

    listed = client.get("/products?available_only=false").json()

    assert [p["name"] for p in listed] == ["Unpriced Cake"]
    assert listed[0]["base_price"] == PLACEHOLDER_PRICE
    assert PLACEHOLDER_PRICE_TAG in listed[0]["tags"], \
        "the admin cannot tell this is provisional"


def test_a_placeholder_is_not_on_the_customer_menu(client, db):
    _placeholder(db)

    assert client.get("/products").json() == []
    assert all(s["products"] == [] for s in client.get("/menu/sections").json())


# ─── RS 1 CANNOT BE SOLD ─────────────────────────────

def test_a_placeholder_cannot_be_published_by_the_toggle(client, admin, db):
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}/toggle-availability", headers=auth(admin))

    assert res.status_code == 409
    assert "placeholder price" in res.json()["detail"]
    db.refresh(product)
    assert product.is_available is False


def test_a_placeholder_cannot_be_published_by_a_patch(client, admin, db):
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"is_available": True})

    assert res.status_code == 409
    db.refresh(product)
    assert product.is_available is False


def test_a_placeholder_cannot_be_ordered(db):
    """Belt and braces: unavailable already blocks create_order."""
    from app.models.user import UserRole
    from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate
    from app.services.order_service import create_order
    from tests.conftest import make_user
    from fastapi import HTTPException

    product = _placeholder(db)
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")

    with pytest.raises(HTTPException) as exc:
        create_order(db, OrderCreate(user_id=user.id, items=[
            OrderItemCreate(product_id=product.id, quantity=1,
                            customization=ItemCustomization())]))
    assert exc.value.status_code == 400


def test_taking_a_product_off_sale_is_never_refused(client, admin, db):
    """The guard is about publishing, not about unpublishing."""
    product = _placeholder(db)
    product.is_available = True          # however it got there
    db.commit()

    res = client.patch(f"/products/{product.id}/toggle-availability", headers=auth(admin))

    assert res.status_code == 200
    assert res.json()["is_available"] is False


# ─── THE NORMAL ADMIN FLOW STILL WORKS ───────────────

def test_setting_a_real_price_clears_the_placeholder(client, admin, db):
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"base_price": 850.0})

    assert res.status_code == 200
    assert res.json()["base_price"] == 850.0
    assert PLACEHOLDER_PRICE_TAG not in res.json()["tags"]


def test_pricing_and_publishing_in_one_save_works(client, admin, db):
    """What an admin filling in a price actually does."""
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"base_price": 850.0, "is_available": True})

    assert res.status_code == 200, res.text
    assert res.json()["is_available"] is True
    assert res.json()["base_price"] == 850.0


def test_after_pricing_the_product_publishes_normally(client, admin, db):
    product = _placeholder(db)
    client.patch(f"/products/{product.id}", headers=auth(admin),
                 json={"base_price": 850.0})

    res = client.patch(f"/products/{product.id}/toggle-availability", headers=auth(admin))

    assert res.status_code == 200
    assert res.json()["is_available"] is True


def test_an_admin_can_declare_a_genuine_one_rupee_price(client, admin, db):
    """Clearing the tag by hand is a legitimate way to say "this Rs 1 is real"."""
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"tags": [], "is_available": True})

    assert res.status_code == 200
    assert res.json()["is_available"] is True


def test_editing_other_fields_leaves_the_placeholder_alone(client, admin, db):
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"description": "Coming soon"})

    assert res.status_code == 200
    assert PLACEHOLDER_PRICE_TAG in res.json()["tags"], "the marker was lost"
    assert res.json()["base_price"] == PLACEHOLDER_PRICE


def test_saving_the_placeholder_price_again_does_not_clear_the_marker(client, admin, db):
    """Re-saving Rs 1 is not the same as confirming Rs 1."""
    product = _placeholder(db)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"base_price": PLACEHOLDER_PRICE})

    assert PLACEHOLDER_PRICE_TAG in res.json()["tags"]


# ─── CONFIRMED PRODUCTS ARE UNAFFECTED ───────────────

def test_a_confirmed_product_publishes_without_obstruction(client, admin, db):
    product = _confirmed(db)

    res = client.patch(f"/products/{product.id}/toggle-availability", headers=auth(admin))

    assert res.status_code == 200
    assert res.json()["is_available"] is True


def test_a_confirmed_product_can_be_repriced_normally(client, admin, db):
    product = _confirmed(db, price=750.0)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"base_price": 800.0})

    assert res.status_code == 200
    assert res.json()["base_price"] == 800.0


# ─── PRICING MACHINERY IS UNTOUCHED ──────────────────

def test_a_placeholder_prices_through_the_engine_like_any_product(db):
    """pricing_unit, options and surcharges must behave normally."""
    from app.models.pricing import SizeRule
    from app.schemas import ItemCustomization
    from app.services.pricing_engine import calculate_item_price

    db.add(SizeRule(name="1kg", multiplier=1.0, is_active=True))
    db.commit()
    kg_product = _placeholder(db, name="Unpriced Cake", pricing_unit="kg")

    breakdown = calculate_item_price(
        db=db, product=kg_product,
        customization=ItemCustomization(size="1kg"), quantity=2)

    assert breakdown.line_total == 2.0, "the pricing engine treated it specially"


def test_images_are_left_to_the_existing_fallback(db):
    """
    No placeholder is written into image_url. The frontend already falls back
    to a bundled photograph and then to its own placeholder block, and setting
    image_url would win over both - hiding the real photo the client supplied.
    """
    product = _placeholder(db)

    assert product.image_url is None
