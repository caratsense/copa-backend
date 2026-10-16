"""
Keeping a price out of the catalogue without taking it away from the builder.

"Vanilla Base Cake" and "Chocolate Base Cake" are prices Build a Cake quotes
from. A customer browsing cakes should never be offered one — it headed its
category like a stray first item — but they cannot simply be deleted or paused:
/build-a-cake/options looks them up BY NAME and refuses to serve the builder at
all if either is missing or unavailable.

So the two questions are kept apart, the same way placeholders keep display and
ordering apart:

  is_pricing_row  - "this is a price, not a cake"  -> a CATALOGUE fact
  is_available    - "the builder can quote it"     -> untouched

These tests hold both ends: the rows are gone from the menu and the catalogue,
and the builder still prices a cake from them.
"""

from app.models.menu_section import MenuSection
from app.models.product import PLACEHOLDER_PRICE_TAG, PRICING_ROW_TAG, Product

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
    s = MenuSection(name="Chocolate Celebration Cakes", sort_order=1, is_active=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@pytest.fixture
def catalogue(db, section):
    """A pricing row and a real cake, in one section."""
    base = Product(name="Chocolate Base Cake", category="cakes", base_price=2200.0,
                   is_available=True, tags=["chocolate", PRICING_ROW_TAG],
                   section_id=section.id, sort_order=1)
    cake = Product(name="Tiramisu Cake", category="cakes", base_price=2500.0,
                   is_available=True, tags=["coffee"],
                   section_id=section.id, sort_order=2)
    db.add_all([base, cake])
    db.commit()
    db.refresh(base)
    db.refresh(cake)
    return base, cake


def _names(payload):
    return [p["name"] for s in payload for p in s["products"]]


def test_the_menu_does_not_offer_a_pricing_row(client, catalogue):
    body = client.get("/menu/sections").json()

    assert "Tiramisu Cake" in _names(body)
    assert "Chocolate Base Cake" not in _names(body)


def test_reviewing_unpriced_products_does_not_bring_pricing_rows_back(client, catalogue):
    """include_placeholders is about prices the client owes us, not these."""
    body = client.get("/menu/sections?include_placeholders=true").json()

    assert "Chocolate Base Cake" not in _names(body)


def test_the_catalogue_leaves_them_out_by_default(client, catalogue):
    names = [p["name"] for p in client.get("/products").json()]

    assert "Tiramisu Cake" in names
    assert "Chocolate Base Cake" not in names


def test_an_admin_can_still_ask_for_them(client, catalogue):
    """They have prices to manage, so a screen that manages prices can see them."""
    rows = client.get("/products?include_pricing_rows=true").json()

    row = next(p for p in rows if p["name"] == "Chocolate Base Cake")
    assert row["is_pricing_row"] is True
    assert row["base_price"] == 2200.0


def test_hiding_one_does_not_make_it_unavailable(client, catalogue):
    """
    The builder refuses to serve if a base row is unavailable, so this is the
    line between "not in the catalogue" and "gone".
    """
    base, _ = catalogue

    assert base.is_available is True
    assert client.get(f"/products/{base.id}").json()["is_available"] is True


def test_a_pricing_row_is_still_orderable_by_id(client, catalogue):
    """
    Build a Cake adds the base product to the cart by id once a size is chosen.
    Hiding it from lists must not break that.
    """
    base, _ = catalogue

    got = client.get(f"/products/{base.id}").json()

    assert got["name"] == "Chocolate Base Cake"
    assert got["is_pricing_row"] is True


def test_a_real_cake_is_never_mistaken_for_one(client, catalogue):
    _, cake = catalogue

    assert client.get(f"/products/{cake.id}").json()["is_pricing_row"] is False


def test_paging_is_not_short_because_rows_were_hidden(client, db, section):
    """
    The hidden rows are removed before the page is cut, not after, or asking
    for two products would hand back one.
    """
    db.add_all([
        Product(name="Base A", category="cakes", base_price=2000.0, is_available=True,
                tags=[PRICING_ROW_TAG], section_id=section.id, sort_order=1),
        Product(name="Cake A", category="cakes", base_price=2400.0, is_available=True,
                section_id=section.id, sort_order=2),
        Product(name="Cake B", category="cakes", base_price=2450.0, is_available=True,
                section_id=section.id, sort_order=3),
    ])
    db.commit()

    page = client.get("/products?limit=2").json()

    assert [p["name"] for p in page] == ["Cake A", "Cake B"]


def test_the_two_hidden_kinds_are_independent(client, db, section):
    """A placeholder is not a pricing row and the reverse, whatever is asked."""
    db.add(Product(name="Unpriced Cake", category="cakes", base_price=1.0,
                   is_available=False, tags=[PLACEHOLDER_PRICE_TAG],
                   section_id=section.id, sort_order=4))
    db.add(Product(name="Vanilla Base Cake", category="cakes", base_price=2000.0,
                   is_available=True, tags=[PRICING_ROW_TAG],
                   section_id=section.id, sort_order=1))
    db.commit()

    both = client.get("/products?include_placeholders=true&include_pricing_rows=true").json()
    neither = client.get("/products").json()
    placeholders_only = client.get("/products?include_placeholders=true").json()

    assert {"Unpriced Cake", "Vanilla Base Cake"} <= {p["name"] for p in both}
    assert not ({"Unpriced Cake", "Vanilla Base Cake"} & {p["name"] for p in neither})
    assert "Unpriced Cake" in {p["name"] for p in placeholders_only}
    assert "Vanilla Base Cake" not in {p["name"] for p in placeholders_only}
