"""
Per-product sizes: the weights and shapes one particular product is sold in.

The global SizeRule table is shared by every per-kg product, so it can only say
"these six weights, for everything". The client's menu needs more than that: a
cake that starts at 700g, another that is not sold under 1kg, a 1.3kg chiffon
at a price that is not per-kg, and tea cakes sold as a Rs 800 loaf or a
Rs 1,700 round - two prices that are not a scaling of one another.

A product with options is priced only from them; the global sizes are not
consulted for it at all. That is what makes a size unselectable, and it holds
for every product without the pricing engine naming any of them.
"""

from app.models.pricing import FlavorRule, SizeRule
from app.models.product import Product
from app.models.product_option import ProductOption
from app.models.user import UserRole
from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate
from app.services.order_service import create_order
from app.services.pricing_engine import calculate_item_price

from tests.conftest import make_user

from fastapi import HTTPException

import pytest


@pytest.fixture
def sizes(db):
    """The global size table, exactly as seeded in production."""
    db.add_all([
        SizeRule(name="500g", multiplier=0.5, is_active=True),
        SizeRule(name="1kg", multiplier=1.0, is_active=True),
        SizeRule(name="1.5kg", multiplier=1.5, is_active=True),
        SizeRule(name="2kg", multiplier=2.0, is_active=True),
    ])
    db.commit()


def _product(db, name, price, unit="kg", options=()):
    p = Product(name=name, category="test", base_price=price, pricing_unit=unit,
                is_customizable=False, is_available=True)
    db.add(p)
    db.commit()
    db.refresh(p)
    for i, (label, kwargs) in enumerate(options, start=1):
        db.add(ProductOption(product_id=p.id, label=label, sort_order=i, **kwargs))
    db.commit()
    db.refresh(p)
    return p


def _price(db, product, size="", **kw):
    return calculate_item_price(
        db=db, product=product,
        customization=ItemCustomization(size=size, **kw),
        quantity=kw.pop("quantity", 1) if "quantity" in kw else 1,
    )


# ─── EXISTING BEHAVIOUR MUST NOT MOVE ────────────────

def test_per_kg_product_without_options_still_uses_the_global_sizes(db, sizes):
    cake = _product(db, "Belgian Chocolate Cake", 2400.0)

    assert _price(db, cake, "1kg").line_total == 2400.0
    assert _price(db, cake, "2kg").line_total == 4800.0
    assert _price(db, cake, "500g").line_total == 1200.0
    # Blank still means "not selected" and costs the base price.
    assert _price(db, cake, "").line_total == 2400.0


def test_fixed_product_without_options_still_ignores_size(db, sizes):
    brownie = _product(db, "Chocolate Walnut Brownies", 750.0, unit="fixed")

    assert _price(db, brownie, "").line_total == 750.0
    assert _price(db, brownie, "2kg").line_total == 750.0, "a size multiplied a fixed product"


def test_fixed_product_with_no_options_is_unaffected_by_the_feature(db, sizes):
    """Whole Wheat Dates & Walnut Tea Cake: Rs 1,700, no selectable size."""
    cake = _product(db, "Whole Wheat Dates & Walnut Tea Cake", 1700.0, unit="fixed")

    assert cake.options == []
    assert _price(db, cake, "").line_total == 1700.0


def test_dog_cakes_remain_two_fixed_products(db, sizes):
    small = _product(db, "Dog Cake - 500gm", 950.0, unit="fixed")
    large = _product(db, "Dog Cake - 1kg", 1900.0, unit="fixed")

    assert _price(db, small, "").line_total == 950.0
    assert _price(db, large, "").line_total == 1900.0


# ─── RESTRICTED AND NON-STANDARD WEIGHTS ─────────────

@pytest.fixture
def blueberry(db):
    """Rs 2,300/kg, sold from 700g. 700g is not a global SizeRule."""
    return _product(db, "Blueberry Lemon Curd Cake", 2300.0, options=[
        ("700g", {"multiplier": 0.7}),
        ("1kg", {"multiplier": 1.0}),
        ("1.5kg", {"multiplier": 1.5}),
        ("2kg", {"multiplier": 2.0}),
    ])


@pytest.fixture
def coffee_cake(db):
    """Rs 2,500/kg, not sold under 1kg."""
    return _product(db, "Belgian Chocolate Coffee Cake With Cinnamon Roll", 2500.0, options=[
        ("1kg", {"multiplier": 1.0}),
        ("1.5kg", {"multiplier": 1.5}),
        ("2kg", {"multiplier": 2.0}),
    ])


def test_blueberry_allows_700g(db, sizes, blueberry):
    assert _price(db, blueberry, "700g").line_total == 1610.0     # 2300 x 0.7
    assert _price(db, blueberry, "1kg").line_total == 2300.0
    assert _price(db, blueberry, "1.5kg").line_total == 3450.0


def test_blueberry_rejects_500g_even_though_it_is_a_global_size(db, sizes, blueberry):
    """500g exists in SizeRule, so only the product's own options can refuse it."""
    with pytest.raises(HTTPException) as exc:
        _price(db, blueberry, "500g")
    assert exc.value.status_code == 400
    assert "700g" in exc.value.detail, "the error should list what IS available"


def test_coffee_cake_allows_1kg_and_rejects_500g(db, sizes, coffee_cake):
    assert _price(db, coffee_cake, "1kg").line_total == 2500.0
    assert _price(db, coffee_cake, "2kg").line_total == 5000.0

    with pytest.raises(HTTPException) as exc:
        _price(db, coffee_cake, "500g")
    assert exc.value.status_code == 400


def test_a_multiplier_option_does_not_duplicate_the_kg_price(db, sizes, blueberry):
    """Correcting the per-kg price must correct every size."""
    blueberry.base_price = 2400.0
    db.commit()

    assert _price(db, blueberry, "700g").line_total == 1680.0     # 2400 x 0.7
    assert _price(db, blueberry, "1kg").line_total == 2400.0


# ─── PRICES THAT ARE NOT PER KG ──────────────────────

def test_chiffon_is_exactly_2400_for_its_1_3kg_option(db, sizes):
    """
    Rs 2,400 for a 1.3kg cake - not Rs 2,400/kg. base_price is irrelevant to an
    outright-priced option, so a wrong one cannot leak into the charge.
    """
    chiffon = _product(db, "Chiffon Fresh Fruit Milk Cake", 9999.0, options=[
        ("1.3kg", {"price": 2400.0}),
    ])

    breakdown = _price(db, chiffon, "1.3kg")
    assert breakdown.line_total == 2400.0
    assert breakdown.size_adjusted == 2400.0
    assert breakdown.option_label == "1.3kg"


@pytest.mark.parametrize("name,loaf,round_", [
    ("Orange Cardamom Crumble", 800.0, 1700.0),
    ("Banana Chocolate Walnut", 800.0, 1700.0),
    ("Vanilla Chocolate Pineapple", 800.0, 1700.0),
    ("Almond Tea Cake - With Egg", 880.0, 1850.0),
    ("Almond Tea Cake - Without Egg", 850.0, 1820.0),
])
def test_every_tea_cake_shape_and_price(db, sizes, name, loaf, round_):
    cake = _product(db, name, loaf, unit="fixed", options=[
        ("500g loaf", {"price": loaf}),
        ("1kg round", {"price": round_}),
    ])

    assert _price(db, cake, "500g loaf").line_total == loaf
    assert _price(db, cake, "1kg round").line_total == round_


def test_almond_with_and_without_egg_are_priced_separately(db, sizes):
    """The client confirmed these differ; they are two products, not one."""
    with_egg = _product(db, "Almond Tea Cake - With Egg", 880.0, unit="fixed", options=[
        ("500g loaf", {"price": 880.0}), ("1kg round", {"price": 1850.0})])
    without = _product(db, "Almond Tea Cake - Without Egg", 850.0, unit="fixed", options=[
        ("500g loaf", {"price": 850.0}), ("1kg round", {"price": 1820.0})])

    assert _price(db, with_egg, "500g loaf").line_total == 880.0
    assert _price(db, without, "500g loaf").line_total == 850.0
    assert _price(db, with_egg, "1kg round").line_total == 1850.0
    assert _price(db, without, "1kg round").line_total == 1820.0


def test_a_tea_cake_shape_is_not_scaled_by_a_size_multiplier(db, sizes):
    """An outright-priced option must never be multiplied by anything."""
    cake = _product(db, "Orange Cardamom Crumble", 800.0, unit="fixed", options=[
        ("500g loaf", {"price": 800.0}), ("1kg round", {"price": 1700.0})])

    assert _price(db, cake, "1kg round").size_multiplier == 1.0
    assert _price(db, cake, "1kg round").line_total == 1700.0


# ─── REJECTION ───────────────────────────────────────

def test_an_unknown_option_is_rejected(db, sizes, blueberry):
    with pytest.raises(HTTPException) as exc:
        _price(db, blueberry, "3kg")
    assert exc.value.status_code == 400


def test_a_blank_choice_is_rejected_when_the_product_has_options(db, sizes):
    """
    Not defaulted. A tea cake is Rs 800 as a loaf and Rs 1,700 as a round, so
    guessing which the customer meant is guessing what to charge them.
    """
    cake = _product(db, "Orange Cardamom Crumble", 800.0, unit="fixed", options=[
        ("500g loaf", {"price": 800.0}), ("1kg round", {"price": 1700.0})])

    with pytest.raises(HTTPException) as exc:
        _price(db, cake, "")
    assert exc.value.status_code == 400
    assert "500g loaf" in exc.value.detail


def test_an_inactive_option_cannot_be_ordered(db, sizes, blueberry):
    option = next(o for o in blueberry.options if o.label == "700g")
    option.is_active = False
    db.commit()
    db.refresh(blueberry)

    with pytest.raises(HTTPException):
        _price(db, blueberry, "700g")
    assert _price(db, blueberry, "1kg").line_total == 2300.0


def test_option_matching_tolerates_case_and_spacing(db, sizes):
    cake = _product(db, "Orange Cardamom Crumble", 800.0, unit="fixed", options=[
        ("500g loaf", {"price": 800.0})])

    assert _price(db, cake, "  500G   LOAF ").line_total == 800.0


# ─── ORDER CREATION CANNOT BYPASS VALIDATION ─────────

def _order(db, product, size):
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    return create_order(db, OrderCreate(
        user_id=user.id,
        items=[OrderItemCreate(product_id=product.id, quantity=1,
                               customization=ItemCustomization(size=size))],
    ))


def test_order_creation_rejects_a_size_the_product_does_not_sell(db, sizes, coffee_cake):
    with pytest.raises(HTTPException) as exc:
        _order(db, coffee_cake, "500g")
    assert exc.value.status_code == 400


def test_order_creation_rejects_a_blank_size_on_an_option_product(db, sizes, blueberry):
    with pytest.raises(HTTPException) as exc:
        _order(db, blueberry, "")
    assert exc.value.status_code == 400


def test_order_creation_charges_the_option_price(db, sizes):
    cake = _product(db, "Orange Cardamom Crumble", 800.0, unit="fixed", options=[
        ("500g loaf", {"price": 800.0}), ("1kg round", {"price": 1700.0})])

    order = _order(db, cake, "1kg round")

    assert order.subtotal == 1700.0
    # The shape is snapshotted, so the order history stays readable after edits.
    assert order.items[0].price_breakdown["option_label"] == "1kg round"


def test_quantity_multiplies_an_option_price(db, sizes):
    cake = _product(db, "Orange Cardamom Crumble", 800.0, unit="fixed", options=[
        ("500g loaf", {"price": 800.0})])

    breakdown = calculate_item_price(
        db=db, product=cake,
        customization=ItemCustomization(size="500g loaf"), quantity=3)
    assert breakdown.line_total == 2400.0


def test_surcharges_still_apply_on_top_of_an_option(db, sizes):
    db.add(FlavorRule(name="Belgian Chocolate", extra_cost=200.0, is_active=True))
    db.commit()
    cake = _product(db, "Blueberry Lemon Curd Cake", 2300.0, options=[
        ("700g", {"multiplier": 0.7})])

    breakdown = calculate_item_price(
        db=db, product=cake,
        customization=ItemCustomization(size="700g", flavor="Belgian Chocolate"))
    assert breakdown.line_total == 1810.0     # 2300 x 0.7 + 200
