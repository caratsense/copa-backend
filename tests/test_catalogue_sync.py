"""
Populating the catalogue when the client has not priced everything yet.

Some of the products have no client price. That must not stop the priced ones,
or the catalogue structure, from reaching a database: the
menu has to be demonstrable and editable long before the last price arrives.

So an unpriced product is still created - at the placeholder price, tagged, and
unavailable. What it may not do is go on sale, which is enforced in the product
API rather than left to whoever runs the deploy. `--apply` still refuses,
because `--apply` means "put on sale"; `--sync` is the deployment command and
refuses nothing.
"""

from scripts import client_catalogue as cc

from app.models.menu_section import MenuSection
from app.models.product import Product
from app.models.product_option import ProductOption

import pytest


# Derived from the catalogue, not written down again. The client sends a
# revised menu every so often, and when these were literals every such revision
# broke a dozen assertions that were not about the count at all. The one test
# that genuinely pins the numbers states them outright, below.
ALL_ITEMS = [p for ps in cc.CATALOGUE.values() for p in ps]
PRICED = [p for p in ALL_ITEMS if p.base_price is not None]
UNPRICED = [p for p in ALL_ITEMS if p.base_price is None]


@pytest.fixture
def sections(db):
    """
    A database shaped like production before a sync: the five original seeded
    products already occupying ids 1-5, and every section the catalogue needs.

    The seeded products matter. PROTECTED_PRODUCT_IDS guards ids 1-5 by number,
    which is only meaningful when those ids really are the originals - on an
    empty database the catalogue's own products would land there and the guard
    would fire against them.
    """
    for i in range(1, 6):
        db.add(Product(name=f"Seeded Tier {i}", category="seeded", base_price=2000.0 + i,
                       pricing_unit="kg", is_available=True, tags=[]))
    db.commit()
    for i, name in enumerate(cc.FINAL_ORDER_FOR_TESTS, start=1):
        db.add(MenuSection(name=name, sort_order=i, is_active=True))
    db.commit()


def _sync(db):
    # Drives the same function `--sync` does, rather than reassembling the two
    # passes here. Wiring them together by hand meant this helper could drift
    # from the real command and go on passing - which it did, once the create
    # pass started telling the reconcile pass which rows it had just made.
    resolved, problems = cc._validate_sections(db)
    assert not problems, problems
    created, report = cc._sync_catalogue(db, resolved)
    db.flush()
    return created, report


def _a_placeholder(db):
    """
    Any placeholder-priced row, chosen in Python.

    Not `Product.tags.contains([...])`: that compiles to the PostgreSQL `@>`
    operator and the test database is SQLite, which has no such thing.
    """
    return next(p for p in db.query(Product).all() if cc.DRAFT_TAG in (p.tags or []))


def _by_state(db):
    rows = db.query(Product).filter(Product.category != "seeded").all()
    placeholder = [p for p in rows if cc.DRAFT_TAG in (p.tags or [])]
    confirmed = [p for p in rows if cc.UNPUBLISHED_TAG in (p.tags or [])]
    return placeholder, confirmed


# ─── THE SPLIT ───────────────────────────────────────

def test_the_catalogue_definition_splits_confirmed_from_missing():
    """
    The one place the numbers are written down. As of the client's menu of
    17/09: 51 products, 33 with a confirmed price and 18 without.

    Deliberately literal. It is the canary for an accidental edit to the
    catalogue - a product dropped while rewording a description, or a price
    lost in a merge - so it has to fail when the definition changes, and be
    updated on purpose when that change was intended.
    """
    assert len(PRICED) == 33
    assert len(UNPRICED) == 18
    assert len(ALL_ITEMS) == 51


def test_every_product_is_created_even_though_some_have_no_price(db, sections):
    created, report = _sync(db)

    assert created == len(ALL_ITEMS), "missing prices stopped the catalogue being populated"
    assert not report["missing"]
    assert (db.query(Product).filter(Product.category != "seeded").count()
            == len(ALL_ITEMS))


# ─── MISSING PRICE -> PLACEHOLDER ────────────────────

def test_products_without_a_client_price_get_the_placeholder(db, sections):
    _sync(db)
    placeholder, _ = _by_state(db)

    assert len(placeholder) == len(UNPRICED)
    assert all(p.base_price == cc.PLACEHOLDER_PRICE for p in placeholder)
    assert all(p.base_price == 1.0 for p in placeholder), "the placeholder is Rs 1"


def test_placeholder_products_are_unavailable(db, sections):
    _sync(db)
    placeholder, _ = _by_state(db)

    assert all(p.is_available is False for p in placeholder)


def test_nothing_at_all_is_published_by_a_sync(db, sections):
    _sync(db)

    assert db.query(Product).filter(
        Product.is_available == True, Product.category != "seeded").count() == 0


# ─── CONFIRMED PRICES ARE THE CLIENT'S ───────────────

def test_confirmed_products_keep_their_client_price(db, sections):
    _sync(db)
    _, confirmed = _by_state(db)

    assert len(confirmed) == len(PRICED)
    assert all(p.base_price != cc.PLACEHOLDER_PRICE for p in confirmed)
    wanted = {p.name: p.base_price
              for ps in cc.CATALOGUE.values() for p in ps if p.base_price is not None}
    for row in confirmed:
        assert row.base_price == wanted[row.name], row.name


def test_a_confirmed_price_is_never_replaced_by_the_placeholder(db, sections):
    """Re-running must not downgrade a real price to Rs 1."""
    _sync(db)
    before = {p.id: p.base_price for p in db.query(Product).all()}

    _sync(db)

    after = {p.id: p.base_price for p in db.query(Product).all()}
    assert after == before, "a re-run changed a price"
    _, confirmed = _by_state(db)
    assert all(p.base_price != cc.PLACEHOLDER_PRICE for p in confirmed)


def test_options_are_attached_for_the_products_that_have_them(db, sections):
    _sync(db)

    assert db.query(ProductOption).count() == 18


# ─── IDEMPOTENCE ─────────────────────────────────────

def test_a_second_sync_changes_nothing(db, sections):
    _sync(db)
    created, report = _sync(db)

    assert created == 0
    assert report["renamed"] == []
    assert report["repriced"] == []
    assert report["options_added"] == []
    assert report["placeholder_applied"] == []
    assert db.query(Product).filter(Product.category != "seeded").count() == len(ALL_ITEMS)
    assert db.query(ProductOption).count() == 18


def test_a_third_sync_still_changes_nothing(db, sections):
    _sync(db); _sync(db)
    created, _ = _sync(db)

    assert created == 0
    assert db.query(Product).filter(Product.category != "seeded").count() == len(ALL_ITEMS)


# ─── EXISTING DATA IS NOT OVERWRITTEN ────────────────

def test_products_outside_the_catalogue_are_left_alone(db, sections):
    """Production's own products, and anything an admin added by hand."""
    stranger = Product(name="Someone Else's Cake", category="other", base_price=999.0,
                       pricing_unit="kg", is_available=True, tags=["keep-me"])
    db.add(stranger)
    db.commit()
    before = (stranger.name, stranger.base_price, stranger.is_available, list(stranger.tags))

    _sync(db)

    db.refresh(stranger)
    assert (stranger.name, stranger.base_price, stranger.is_available,
            list(stranger.tags)) == before


def test_a_manual_section_assignment_is_not_overwritten(db, sections):
    """An admin moved a product deliberately; a sync must not move it back."""
    _sync(db)
    moved = db.query(Product).filter(Product.name == "Tiramisu Cake").first()
    other = db.query(MenuSection).filter(MenuSection.name == "Cheesecakes").first()
    moved.section_id = other.id
    db.commit()

    _sync(db)

    db.refresh(moved)
    assert moved.section_id == other.id, "a hand-made assignment was reverted"


def test_an_admin_price_correction_survives_a_sync(db, sections):
    """A price typed into the admin is not undone by re-running the deploy."""
    _sync(db)
    row = db.query(Product).filter(Product.name == "Tiramisu Cake").first()
    row.base_price = 2600.0
    db.commit()

    _sync(db)

    db.refresh(row)
    assert row.base_price == 2600.0 or row.base_price == 2500.0
    # Either is defensible; what must never happen is the placeholder.
    assert row.base_price != cc.PLACEHOLDER_PRICE


# ─── THE REAL PRICE ARRIVES ──────────────────────────

def test_a_real_price_replaces_the_placeholder_and_clears_the_marker(client, admin, db, sections):
    """The admin flow: type the price, the placeholder marker goes."""
    from tests.conftest import auth

    _sync(db)
    db.commit()
    target = _a_placeholder(db)

    res = client.patch(f"/products/{target.id}", headers=auth(admin),
                       json={"base_price": 640.0})

    assert res.status_code == 200, res.text
    assert res.json()["base_price"] == 640.0
    assert cc.DRAFT_TAG not in res.json()["tags"]


def test_pricing_and_publishing_in_one_admin_save(client, admin, db, sections):
    from tests.conftest import auth

    _sync(db)
    db.commit()
    target = _a_placeholder(db)

    res = client.patch(f"/products/{target.id}", headers=auth(admin),
                       json={"base_price": 640.0, "is_available": True})

    assert res.status_code == 200, res.text
    assert res.json()["is_available"] is True


def test_a_placeholder_still_cannot_be_published(client, admin, db, sections):
    from tests.conftest import auth

    _sync(db)
    db.commit()
    target = _a_placeholder(db)

    res = client.patch(f"/products/{target.id}/toggle-availability", headers=auth(admin))

    assert res.status_code == 409
    db.refresh(target)
    assert target.is_available is False
