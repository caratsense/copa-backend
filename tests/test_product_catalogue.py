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


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """
    Exercise pagination, not throttling.

    The app carries a global 100/minute limit and these tests create sixty-odd
    products apiece, so without this the later cases come back 429 and look
    like truncation bugs - the very thing they exist to detect. Scoped to this
    file; the limits are real behaviour and stay on everywhere else.
    """
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


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


# ─── PAGINATION: THE DEFAULT MUST NOT TRUNCATE ───────
# `limit` defaulted to 50. Three callers fetch /products with no parameters -
# the homepage, the admin product list and the cake builder - so once the
# catalogue passed fifty products they each silently showed a prefix of it.
# Nothing about a page of fifty looks wrong, which is what made it dangerous.


def _bulk(client, admin, count, section=None, available=True):
    """Create `count` products, ordered by sort_order so the set is checkable."""
    made = []
    for i in range(count):
        res = client.post("/products", headers=auth(admin), json=_payload(
            name=f"Catalogue Item {i:03d}", sort_order=i,
            is_available=available,
            section_id=section.id if section else None,
        ))
        assert res.status_code == 201, res.text
        made.append(res.json())
    return made


def test_a_catalogue_larger_than_fifty_is_returned_whole(client, admin, db):
    _bulk(client, admin, 63)

    listed = client.get("/products").json()

    assert len(listed) == 63, f"truncated to {len(listed)}"
    assert [p["name"] for p in listed] == [f"Catalogue Item {i:03d}" for i in range(63)]


def test_the_old_fifty_boundary_is_gone(client, admin, db):
    """51 is the smallest catalogue the old default would have clipped."""
    _bulk(client, admin, 51)

    assert len(client.get("/products").json()) == 51


def test_unavailable_products_are_included_when_asked_and_not_truncated(client, admin, db):
    """The admin list fetches available_only=false with no limit."""
    _bulk(client, admin, 30, available=True)
    _bulk(client, admin, 30, available=False)

    assert len(client.get("/products?available_only=false").json()) == 60
    assert len(client.get("/products").json()) == 30


def test_a_category_filter_is_not_truncated_either(client, admin, db):
    for i in range(55):
        client.post("/products", headers=auth(admin),
                    json=_payload(name=f"Brownie {i:03d}", category="brownies"))

    assert len(client.get("/products?category=brownies").json()) == 55


# ─── EXPLICIT PAGING IS UNCHANGED ────────────────────

def test_an_explicit_limit_is_still_honoured(client, admin, db):
    _bulk(client, admin, 60)

    assert len(client.get("/products?limit=50").json()) == 50
    assert len(client.get("/products?limit=10").json()) == 10
    assert len(client.get("/products?limit=100").json()) == 60, "asked for more than exists"


def test_skip_and_limit_still_page_without_repeating_or_dropping(client, admin, db):
    _bulk(client, admin, 55)

    seen = []
    for skip in range(0, 55, 10):
        seen += [p["id"] for p in client.get(f"/products?skip={skip}&limit=10").json()]

    assert len(seen) == 55
    assert len(set(seen)) == 55, "a product appeared on two pages"
    assert seen == sorted(seen), "pages were not in a stable order"


def test_skip_without_a_limit_returns_the_rest(client, admin, db):
    _bulk(client, admin, 55)

    rest = client.get("/products?skip=50").json()

    assert len(rest) == 5, "skip alone should return everything after the offset"


def test_limit_zero_still_returns_nothing(client, admin, db):
    """Preserved deliberately: it is what LIMIT 0 did before."""
    _bulk(client, admin, 3)

    assert client.get("/products?limit=0").json() == []


@pytest.mark.parametrize("bad", ["-1", "-50"])
def test_negative_paging_is_rejected_rather_than_erroring(client, admin, db, bad):
    """A negative offset used to reach the database and surface as a 500."""
    assert client.get(f"/products?skip={bad}").status_code == 422
    assert client.get(f"/products?limit={bad}").status_code == 422


def test_an_empty_catalogue_is_still_an_empty_list(client, db):
    assert client.get("/products").json() == []
