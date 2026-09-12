"""
The public menu: every active section, only its available products.

An active section stays in the response even with nothing to show, so the menu
page can render its own "coming soon" state for a category the client has
announced but not yet stocked - Cheesecakes, Gifting Collection and Wedding are
all in exactly that position, and the 48 catalogue products currently sit in
their sections as unavailable drafts.

The section must survive two different kinds of empty: having no products at
all, and having only products the customer may not order.
"""

from app.models.menu_section import MenuSection
from app.models.product import Product

import pytest


def _section(db, name, sort_order, is_active=True):
    s = MenuSection(name=name, sort_order=sort_order, is_active=is_active)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _product(db, name, section=None, sort_order=1, is_available=True):
    p = Product(
        name=name, category="test", base_price=100.0, pricing_unit="fixed",
        is_customizable=False, is_available=is_available,
        section_id=section.id if section else None, sort_order=sort_order,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _named(menu, name):
    return next(s for s in menu if s["name"] == name)


# ─── WHAT IS RETURNED ────────────────────────────────

def test_active_section_returns_its_available_products(client, db):
    section = _section(db, "Brownies", 1)
    _product(db, "Chocolate Walnut Brownies", section)

    menu = client.get("/menu/sections").json()

    assert [p["name"] for p in _named(menu, "Brownies")["products"]] == \
        ["Chocolate Walnut Brownies"]


def test_active_section_with_no_products_is_still_returned(client, db):
    """Cheesecakes, Gifting Collection and Wedding are all in this state."""
    _section(db, "Cheesecakes", 1)

    menu = client.get("/menu/sections").json()

    section = _named(menu, "Cheesecakes")
    assert section["products"] == []
    assert section["id"] is not None


def test_section_whose_products_are_all_unavailable_is_still_returned(client, db):
    """
    The live case: 48 catalogue products exist as unavailable drafts. Filtering
    their section out because nothing in it can be bought would hide a category
    the client has announced.
    """
    section = _section(db, "Dog Cakes", 1)
    _product(db, "Dog Cake - 500gm", section, sort_order=1, is_available=False)
    _product(db, "Dog Cake - 1kg", section, sort_order=2, is_available=False)

    menu = client.get("/menu/sections").json()

    assert _named(menu, "Dog Cakes")["products"] == []


def test_unavailable_products_never_appear_in_the_menu(client, db):
    section = _section(db, "Cookies", 1)
    _product(db, "On Sale", section, sort_order=1, is_available=True)
    _product(db, "Draft", section, sort_order=2, is_available=False)

    menu = client.get("/menu/sections").json()

    names = [p["name"] for s in menu for p in s["products"]]
    assert "On Sale" in names
    assert "Draft" not in names


def test_inactive_sections_are_not_returned(client, db):
    _section(db, "Retired", 1, is_active=False)
    _section(db, "Live", 2)

    menu = client.get("/menu/sections").json()

    assert [s["name"] for s in menu] == ["Live"]


# ─── ORDERING ────────────────────────────────────────

def test_sections_are_ordered_by_sort_order_then_id(client, db):
    # Created out of order, and two sharing a sort_order to force the tie-break.
    third = _section(db, "Third", 3)
    first = _section(db, "First", 1)
    tie_a = _section(db, "Tie A", 2)
    tie_b = _section(db, "Tie B", 2)

    menu = client.get("/menu/sections").json()

    assert [s["name"] for s in menu] == ["First", "Tie A", "Tie B", "Third"]
    assert tie_a.id < tie_b.id and first.id > third.id


def test_products_within_a_section_are_ordered_by_sort_order_then_id(client, db):
    section = _section(db, "Breads", 1)
    _product(db, "Third", section, sort_order=3)
    _product(db, "First", section, sort_order=1)
    tie_a = _product(db, "Tie A", section, sort_order=2)
    tie_b = _product(db, "Tie B", section, sort_order=2)

    menu = client.get("/menu/sections").json()

    products = _named(menu, "Breads")["products"]
    assert [p["name"] for p in products] == ["First", "Tie A", "Tie B", "Third"]
    assert tie_a.id < tie_b.id


# ─── THE ORPHAN BUCKET ───────────────────────────────

def test_orphan_available_products_appear_in_other_cakes(client, db):
    _section(db, "Brownies", 1)
    _product(db, "Unassigned Cake", section=None)

    menu = client.get("/menu/sections").json()

    bucket = _named(menu, "Other Cakes")
    assert bucket["id"] is None
    assert bucket["sort_order"] == 999
    assert [p["name"] for p in bucket["products"]] == ["Unassigned Cake"]
    assert menu[-1] is bucket or menu[-1]["name"] == "Other Cakes"


def test_no_other_cakes_bucket_when_nothing_is_unassigned(client, db):
    section = _section(db, "Brownies", 1)
    _product(db, "Assigned", section)

    menu = client.get("/menu/sections").json()

    assert not any(s["id"] is None for s in menu)
    assert "Other Cakes" not in [s["name"] for s in menu]


def test_unavailable_orphans_do_not_create_an_other_cakes_bucket(client, db):
    """An unassigned draft must not conjure a bucket that shows nothing."""
    _section(db, "Brownies", 1)
    _product(db, "Unassigned Draft", section=None, is_available=False)

    menu = client.get("/menu/sections").json()

    assert "Other Cakes" not in [s["name"] for s in menu]
