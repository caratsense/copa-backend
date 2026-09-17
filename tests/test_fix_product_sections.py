"""
Restoring two products that were moved by hand in production.

Chocolate Base Cake (id 3) ended up under Signature Collection and Chocolate
Premium Cake (id 4) under Vanilla Cakes, leaving both chocolate sections empty.
setup_menu_sections renames Vanilla Cakes to "Vanilla Celebration Cakes", so
left alone a chocolate cake goes live in the vanilla section - and that script
refuses to run at all until the baseline matches, which blocks the catalogue
deployment behind it.

The fixture below reproduces production's exact state as of 17/09, so these
tests fail if the fix stops producing the arrangement setup_menu_sections
demands.
"""

import pytest

from scripts import fix_product_sections as fix
from scripts.setup_menu_sections import BASELINE

from app.models.menu_section import MenuSection
from app.models.product import Product


SECTION_NAMES = {
    1: "Signature Collection",
    2: "Vanilla Cakes",
    3: "Chocolate Cakes",
    4: "Premium Belgian",
    5: "Specialty",
}

PRODUCT_NAMES = {
    1: "Vanilla Base Cake",
    2: "Vanilla Premium Cake",
    3: "Chocolate Base Cake",
    4: "Chocolate Premium Cake",
    5: "Baileys Coffee Mousse Cake",
}

# Where production actually had them on 17/09, read off the live API.
BROKEN = {1: 2, 2: 2, 3: 1, 4: 2, 5: 1}

# Where they belong, per BASELINE.
CORRECT = {1: 2, 2: 2, 3: 3, 4: 4, 5: 1}


def _build(db, placement):
    for section_id, name in SECTION_NAMES.items():
        db.add(MenuSection(id=section_id, name=name, sort_order=section_id,
                           is_active=True))
    db.commit()
    for product_id, name in PRODUCT_NAMES.items():
        db.add(Product(id=product_id, name=name, category="seeded",
                       base_price=2000.0 + product_id, pricing_unit="kg",
                       is_available=True, tags=[],
                       section_id=placement[product_id]))
    db.commit()


@pytest.fixture
def broken(db):
    """Production as it actually stands."""
    _build(db, BROKEN)


@pytest.fixture
def already_correct(db):
    _build(db, CORRECT)


def _run(db, apply):
    """Drive main() against the test database."""
    import sys
    argv = ["fix_product_sections"] + (["--apply"] if apply else [])
    old = sys.argv
    sys.argv = argv
    try:
        return fix.main()
    finally:
        sys.argv = old
        db.expire_all()


def _placement(db):
    return {p.id: p.section_id for p in db.query(Product).order_by(Product.id).all()}


# ─── THE FIX ─────────────────────────────────────────

def test_the_broken_state_really_does_fail_the_baseline(db, broken):
    """
    Guards the fixture itself. If this passes, the rest of the file is
    testing a fix for a problem that no longer reproduces.
    """
    assert fix._verify(db) != []


def test_applying_moves_both_products_back(db, broken):
    assert _run(db, apply=True) == 0
    assert _placement(db) == CORRECT


def test_the_result_satisfies_the_baseline_setup_menu_sections_demands(db, broken):
    _run(db, apply=True)

    assert fix._verify(db) == []
    for section_id, expect in BASELINE.items():
        found = [p.id for p in db.query(Product)
                 .filter(Product.section_id == section_id)
                 .order_by(Product.id).all()]
        assert found == expect["products"], f"section {section_id}"


def test_the_chocolate_sections_are_no_longer_empty(db, broken):
    """The visible symptom: two chocolate cakes, neither in a chocolate section."""
    _run(db, apply=True)
    placement = _placement(db)

    assert placement[3] == 3, "Chocolate Base Cake belongs in Chocolate Cakes"
    assert placement[4] == 4, "Chocolate Premium Cake belongs in Premium Belgian"


# ─── WHAT IT DOES NOT TOUCH ──────────────────────────

def test_the_other_three_products_are_not_moved(db, broken):
    _run(db, apply=True)
    placement = _placement(db)

    for product_id in (1, 2, 5):
        assert placement[product_id] == BROKEN[product_id]


def test_nothing_but_section_id_is_written(db, broken):
    before = {p.id: (p.name, p.base_price, p.is_available, list(p.tags or []))
              for p in db.query(Product).all()}

    _run(db, apply=True)

    after = {p.id: (p.name, p.base_price, p.is_available, list(p.tags or []))
             for p in db.query(Product).all()}
    assert after == before


# ─── DRY RUN AND IDEMPOTENCE ─────────────────────────

def test_without_apply_nothing_is_written(db, broken):
    assert _run(db, apply=False) == 0
    assert _placement(db) == BROKEN


def test_running_twice_is_safe(db, broken):
    _run(db, apply=True)
    assert _run(db, apply=True) == 0
    assert _placement(db) == CORRECT


def test_an_already_correct_database_reports_success(db, already_correct):
    assert _run(db, apply=True) == 0
    assert _placement(db) == CORRECT


# ─── IT REFUSES WHAT IT DOES NOT RECOGNISE ───────────

def test_an_unfamiliar_placement_is_refused(db):
    """
    A product somewhere the script has no story for means a person should
    look, not that two rows should be moved and success reported.
    """
    odd = dict(CORRECT)
    odd[3] = 5
    _build(db, odd)

    assert _run(db, apply=True) == 2
    assert _placement(db)[3] == 5, "it wrote something despite aborting"
