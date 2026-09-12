"""
Products carry their menu section and their place in it through the API.

`products.section_id` and `products.sort_order` are real columns, but neither
was exposed on ProductCreate/ProductUpdate. Pydantic ignores unknown keys by
default, so the admin form's `section_id` was accepted with a 200 and silently
discarded, and every product created through the API landed at sort_order 0 -
which leaves the public menu with nothing to order a section's products by.

These matter before the real catalogue is populated: ~48 products have to land
in the right section, in the client's order, without a second round of calls
per product.
"""

from app.models.menu_section import MenuSection
from app.models.product import Product

from tests.conftest import auth

import pytest


@pytest.fixture
def section(db):
    s = MenuSection(name="Brownies", sort_order=5, is_active=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _payload(**overrides):
    body = {
        "name": "Chocolate Walnut Brownies",
        "category": "brownies",
        "base_price": 400.0,
        "pricing_unit": "fixed",
        "is_customizable": False,
    }
    body.update(overrides)
    return body


# ─── CREATE ──────────────────────────────────────────

def test_create_product_with_section_and_sort_order(client, admin, db, section):
    res = client.post("/products", headers=auth(admin),
                      json=_payload(section_id=section.id, sort_order=3))

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["section_id"] == section.id
    assert body["sort_order"] == 3

    # Read back from the database, not just the response: the response could
    # echo a value the insert never actually stored.
    row = db.query(Product).filter(Product.id == body["id"]).first()
    assert row.section_id == section.id
    assert row.sort_order == 3


def test_create_product_without_section_or_sort_order_still_works(client, admin, db):
    """Every existing caller omits both. They must keep working unchanged."""
    res = client.post("/products", headers=auth(admin), json=_payload())

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["section_id"] is None
    assert body["sort_order"] == 0

    row = db.query(Product).filter(Product.id == body["id"]).first()
    assert row.section_id is None
    # Not NULL. The public menu sorts on this column, and None is not
    # comparable to an int - one NULL row raises a TypeError for the whole menu.
    assert row.sort_order == 0


def test_created_product_appears_in_its_section_on_the_public_menu(client, admin, section):
    """The point of the field: one call puts a product on the menu."""
    client.post("/products", headers=auth(admin),
                json=_payload(section_id=section.id, sort_order=1))

    menu = client.get("/menu/sections").json()
    brownies = next(s for s in menu if s["name"] == "Brownies")
    assert [p["name"] for p in brownies["products"]] == ["Chocolate Walnut Brownies"]
    # Not in the trailing unassigned bucket.
    assert not any(s["id"] is None for s in menu)


# ─── UPDATE ──────────────────────────────────────────

def test_update_product_section_and_sort_order(client, admin, db, section):
    created = client.post("/products", headers=auth(admin), json=_payload()).json()
    other = MenuSection(name="Cookies", sort_order=8, is_active=True)
    db.add(other)
    db.commit()
    db.refresh(other)

    res = client.patch(f"/products/{created['id']}", headers=auth(admin),
                       json={"section_id": other.id, "sort_order": 7})

    assert res.status_code == 200, res.text
    assert res.json()["section_id"] == other.id
    assert res.json()["sort_order"] == 7

    db.expire_all()
    row = db.query(Product).filter(Product.id == created["id"]).first()
    assert row.section_id == other.id
    assert row.sort_order == 7


def test_update_can_unassign_a_product_from_its_section(client, admin, db, section):
    """Null means unassigned, matching /admin/sections/assign-product."""
    created = client.post("/products", headers=auth(admin),
                          json=_payload(section_id=section.id)).json()

    res = client.patch(f"/products/{created['id']}", headers=auth(admin),
                       json={"section_id": None})

    assert res.status_code == 200, res.text
    assert res.json()["section_id"] is None


def test_update_leaves_untouched_fields_alone(client, admin, db, section):
    """PATCH uses exclude_unset, so omitting a field must not reset it."""
    created = client.post("/products", headers=auth(admin),
                          json=_payload(section_id=section.id, sort_order=4)).json()

    res = client.patch(f"/products/{created['id']}", headers=auth(admin),
                       json={"base_price": 450.0})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["base_price"] == 450.0
    assert body["section_id"] == section.id, "an omitted section_id was cleared"
    assert body["sort_order"] == 4, "an omitted sort_order was reset"


# ─── READ ────────────────────────────────────────────

def test_product_read_exposes_section_and_sort_order(client, admin, section):
    created = client.post("/products", headers=auth(admin),
                          json=_payload(section_id=section.id, sort_order=2)).json()

    one = client.get(f"/products/{created['id']}").json()
    assert one["section_id"] == section.id
    assert one["sort_order"] == 2

    listed = client.get("/products").json()
    assert all("section_id" in p and "sort_order" in p for p in listed)


# ─── ORDERING ────────────────────────────────────────
# Neither query had an ORDER BY. The database was free to return products in
# any order and to change that order between identical requests, which with
# offset pagination can repeat a product on one page and skip it on the next.


def _create(client, admin, name, **overrides):
    res = client.post("/products", headers=auth(admin),
                      json=_payload(name=name, **overrides))
    assert res.status_code == 201, res.text
    return res.json()


def test_products_are_returned_in_sort_order_then_id(client, admin, section):
    """Insertion order is deliberately not the wanted order."""
    _create(client, admin, "Third", sort_order=30, section_id=section.id)
    _create(client, admin, "First", sort_order=10, section_id=section.id)
    _create(client, admin, "Second", sort_order=20, section_id=section.id)

    names = [p["name"] for p in client.get("/products").json()]
    assert names == ["First", "Second", "Third"]


def test_products_sharing_a_sort_order_fall_back_to_id(client, admin, section):
    """sort_order alone is not a total order; id makes it one."""
    a = _create(client, admin, "Alpha", sort_order=5, section_id=section.id)
    b = _create(client, admin, "Bravo", sort_order=5, section_id=section.id)
    c = _create(client, admin, "Charlie", sort_order=5, section_id=section.id)

    listed = client.get("/products").json()
    assert [p["id"] for p in listed] == sorted([a["id"], b["id"], c["id"]])


def test_products_without_an_explicit_sort_order_still_list_by_id(client, admin):
    """Every existing caller omits sort_order, so they all tie at 0."""
    first = _create(client, admin, "Older")
    second = _create(client, admin, "Newer")

    listed = client.get("/products").json()
    assert all(p["sort_order"] == 0 for p in listed)
    assert [p["name"] for p in listed] == ["Older", "Newer"]
    assert [p["id"] for p in listed] == [first["id"], second["id"]]


def test_pagination_does_not_repeat_or_skip_a_product(client, admin, section):
    """An unordered query with offset/limit can hand back the same row twice."""
    for i, name in enumerate(["A", "B", "C", "D"]):
        _create(client, admin, name, sort_order=i, section_id=section.id)

    page1 = client.get("/products?skip=0&limit=2").json()
    page2 = client.get("/products?skip=2&limit=2").json()

    ids = [p["id"] for p in page1 + page2]
    assert len(set(ids)) == 4, "a product appeared on more than one page"
    assert [p["name"] for p in page1 + page2] == ["A", "B", "C", "D"]


def test_menu_sections_orders_products_the_same_way(client, admin, section):
    _create(client, admin, "Third", sort_order=30, section_id=section.id)
    _create(client, admin, "First", sort_order=10, section_id=section.id)
    _create(client, admin, "Second", sort_order=20, section_id=section.id)

    menu = client.get("/menu/sections").json()
    brownies = next(s for s in menu if s["name"] == "Brownies")
    assert [p["name"] for p in brownies["products"]] == ["First", "Second", "Third"]


def test_menu_sections_breaks_ties_by_id_like_products_does(client, admin, section):
    """The two endpoints must not disagree about the order of the same rows."""
    for name in ["Alpha", "Bravo", "Charlie"]:
        _create(client, admin, name, sort_order=5, section_id=section.id)

    menu = client.get("/menu/sections").json()
    brownies = next(s for s in menu if s["name"] == "Brownies")
    in_section = [p["id"] for p in brownies["products"]]
    in_list = [p["id"] for p in client.get("/products").json()]

    assert in_section == sorted(in_section)
    assert in_section == in_list, "the menu and the product list disagree on order"
