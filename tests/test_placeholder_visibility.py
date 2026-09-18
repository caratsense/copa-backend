"""
Showing an unpriced product without making it buyable.

The catalogue has to be reviewable before every price is in, but a product the
client has not priced must never be sold at its stand-in price. Those two pull
in opposite directions, and the resolution is that they are separate questions:

  is_placeholder  - "base_price is a stand-in"   -> a DISPLAY fact
  is_available    - "this can be ordered"        -> the ORDER rule

Visibility is opt-in via `include_placeholders`, and it never touches
is_available. So nothing that decides whether an order may happen was relaxed
to make the menu look complete: create_order still refuses, the publish guard
still refuses, and a caller that does not ask for placeholders sees exactly
what it saw before.
"""

from app.models.product import PLACEHOLDER_PRICE_TAG, Product
from app.models.menu_section import MenuSection
from app.models.user import UserRole
from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate
from app.services.order_service import create_order

from tests.conftest import auth, make_user

from fastapi import HTTPException

import pytest


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


@pytest.fixture
def section(db):
    s = MenuSection(name="Cookies", sort_order=1, is_active=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _placeholder(db, section, name="Unpriced Cookie"):
    p = Product(name=name, category="cookies", base_price=1.0, pricing_unit="fixed",
                is_customizable=False, is_available=False,
                tags=[PLACEHOLDER_PRICE_TAG], section_id=section.id, sort_order=2)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _confirmed(db, section, name="Priced Cookie", price=450.0, available=True):
    p = Product(name=name, category="cookies", base_price=price, pricing_unit="fixed",
                is_customizable=False, is_available=available,
                tags=[], section_id=section.id, sort_order=1)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _paused(db, section):
    """Unavailable but NOT a placeholder - the owner took it off sale."""
    return _confirmed(db, section, name="Paused Cookie", price=500.0, available=False)


# ─── 1. PLACEHOLDERS ARE RETURNED FOR DISPLAY ────────

def test_products_returns_placeholders_when_asked(client, db, section):
    _confirmed(db, section)
    _placeholder(db, section)

    listed = client.get("/products?include_placeholders=true").json()

    assert sorted(p["name"] for p in listed) == ["Priced Cookie", "Unpriced Cookie"]


def test_the_menu_returns_placeholders_when_asked(client, db, section):
    _confirmed(db, section)
    _placeholder(db, section)

    menu = client.get("/menu/sections?include_placeholders=true").json()
    cookies = next(s for s in menu if s["name"] == "Cookies")

    assert sorted(p["name"] for p in cookies["products"]) == \
        ["Priced Cookie", "Unpriced Cookie"]


def test_a_placeholder_is_flagged_as_one_in_both_responses(client, db, section):
    _placeholder(db, section)

    listed = client.get("/products?include_placeholders=true").json()
    menu = client.get("/menu/sections?include_placeholders=true").json()
    from_menu = next(s for s in menu if s["name"] == "Cookies")["products"][0]

    for row in (listed[0], from_menu):
        assert row["is_placeholder"] is True, "a client cannot tell this is unpriced"
        assert row["is_available"] is False, "shown as orderable"


def test_an_unassigned_placeholder_shows_in_the_other_cakes_bucket(client, db, section):
    orphan = _placeholder(db, section, name="Unassigned Unpriced")
    orphan.section_id = None
    db.commit()

    menu = client.get("/menu/sections?include_placeholders=true").json()
    bucket = next((s for s in menu if s["id"] is None), None)

    assert bucket is not None
    assert [p["name"] for p in bucket["products"]] == ["Unassigned Unpriced"]


# ─── 2. PLACEHOLDERS CANNOT BE ORDERED ───────────────

def test_a_placeholder_cannot_be_ordered(db, section):
    product = _placeholder(db, section)
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")

    with pytest.raises(HTTPException) as exc:
        create_order(db, OrderCreate(user_id=user.id, items=[
            OrderItemCreate(product_id=product.id, quantity=1,
                            customization=ItemCustomization())]))

    assert exc.value.status_code == 400
    assert "unavailable" in exc.value.detail.lower()


def test_being_visible_does_not_make_it_orderable(client, db, section):
    """The display flag and the order rule are independent."""
    product = _placeholder(db, section)
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")

    shown = client.get("/products?include_placeholders=true").json()
    assert shown[0]["is_placeholder"] is True

    with pytest.raises(HTTPException):
        create_order(db, OrderCreate(user_id=user.id, items=[
            OrderItemCreate(product_id=product.id, quantity=1,
                            customization=ItemCustomization())]))


def test_the_publish_guard_still_refuses(client, admin, db, section):
    product = _placeholder(db, section)

    res = client.patch(f"/products/{product.id}/toggle-availability", headers=auth(admin))

    assert res.status_code == 409
    db.refresh(product)
    assert product.is_available is False


# ─── 3. EXISTING BEHAVIOUR IS UNTOUCHED ──────────────

def test_the_default_still_hides_placeholders(client, db, section):
    _confirmed(db, section)
    _placeholder(db, section)

    assert [p["name"] for p in client.get("/products").json()] == ["Priced Cookie"]
    menu = client.get("/menu/sections").json()
    cookies = next(s for s in menu if s["name"] == "Cookies")
    assert [p["name"] for p in cookies["products"]] == ["Priced Cookie"]


def test_a_confirmed_product_is_never_flagged_as_a_placeholder(client, db, section):
    _confirmed(db, section)

    row = client.get("/products").json()[0]

    assert row["is_placeholder"] is False
    assert row["is_available"] is True
    assert row["base_price"] == 450.0


def test_a_paused_product_is_not_revealed_by_the_flag(client, db, section):
    """
    Only placeholders come back - not every unavailable product. A product the
    owner took off sale stays off the menu.
    """
    _paused(db, section)
    _placeholder(db, section)

    listed = client.get("/products?include_placeholders=true").json()

    assert [p["name"] for p in listed] == ["Unpriced Cookie"]
    assert "Paused Cookie" not in [p["name"] for p in listed]


def test_available_only_false_still_returns_everything(client, db, section):
    """The admin listing is unaffected."""
    _confirmed(db, section)
    _placeholder(db, section)
    _paused(db, section)

    assert len(client.get("/products?available_only=false").json()) == 3


def test_paging_still_works_with_placeholders_included(client, db, section):
    for i in range(6):
        _placeholder(db, section, name=f"Unpriced {i}")

    page1 = client.get("/products?include_placeholders=true&skip=0&limit=3").json()
    page2 = client.get("/products?include_placeholders=true&skip=3&limit=3").json()

    ids = [p["id"] for p in page1 + page2]
    assert len(ids) == 6
    assert len(set(ids)) == 6, "a product appeared on two pages"


# ─── 4. THE REAL PRICE ARRIVES ───────────────────────

def test_a_real_price_clears_the_placeholder_state(client, admin, db, section):
    product = _placeholder(db, section)

    res = client.patch(f"/products/{product.id}", headers=auth(admin),
                       json={"base_price": 480.0})

    assert res.status_code == 200, res.text
    assert res.json()["base_price"] == 480.0
    assert res.json()["is_placeholder"] is False
    assert PLACEHOLDER_PRICE_TAG not in res.json()["tags"]


def test_after_pricing_and_publishing_it_behaves_like_any_product(client, admin, db, section):
    product = _placeholder(db, section)

    client.patch(f"/products/{product.id}", headers=auth(admin),
                 json={"base_price": 480.0, "is_available": True})

    # On the default catalogue now, with no placeholder flag.
    listed = client.get("/products").json()
    assert [p["name"] for p in listed] == ["Unpriced Cookie"]
    assert listed[0]["is_placeholder"] is False
    assert listed[0]["base_price"] == 480.0

    # And orderable.
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = create_order(db, OrderCreate(user_id=user.id, items=[
        OrderItemCreate(product_id=product.id, quantity=1,
                        customization=ItemCustomization())]))
    assert order.subtotal == 480.0


def test_a_priced_product_is_no_longer_duplicated_by_the_flag(client, admin, db, section):
    """Once real, it appears once - not as both a product and a placeholder."""
    product = _placeholder(db, section)
    client.patch(f"/products/{product.id}", headers=auth(admin),
                 json={"base_price": 480.0, "is_available": True})

    listed = client.get("/products?include_placeholders=true").json()

    assert len(listed) == 1
