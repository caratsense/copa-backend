"""
Pricing Engine — calculates the full price breakdown for any item + customization.

This module is the single authority on what an order costs. The checkout
preview, order creation and the WhatsApp flow all price through here, so a
quote and a charge cannot disagree.

Lookups are STRICT. A name that does not match an active rule is rejected
rather than silently priced at zero: `_lookup_or_zero` used to return 0.0 for
anything it could not find, so a client could drop a premium simply by
mis-typing it —

    exact:      size "5kg" + flavor "Belgian Chocolate"   -> Rs 7000
    mangled:    size "5kg " + flavor "belgian chocolate"  -> Rs 1000

Matching is case- and whitespace-insensitive so honest clients are unaffected;
an empty string still means "not selected" and legitimately costs nothing.

To add a new pricing dimension:
1. Create a new rule model in app/models/pricing.py
2. Add a lookup step in calculate_item_price() below
3. Add the cost to the breakdown
4. Register the admin CRUD route in app/api/routes/admin.py
"""

from sqlalchemy import func
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.product import Product
from app.models.product_option import ProductOption
from app.models.pricing import SizeRule, FlavorRule, DesignRule, AddonRule, RushRule
from app.models.delivery import DeliveryZone
from app.schemas import ItemCustomization, PriceBreakdown, PricingRequest, PricingResponse


def _normalise(name: str | None) -> str:
    """Collapse whitespace and case so 'belgian  chocolate ' matches its rule."""
    return " ".join((name or "").split()).strip().lower()


def _find_rule(db: Session, model, name: str, label: str):
    """
    Look up an active rule by name, tolerating case and spacing.

    Returns None when `name` is blank (the option was not chosen). Raises 400
    when a non-blank name matches nothing — pricing must never quietly discount.
    """
    wanted = _normalise(name)
    if not wanted:
        return None

    row = (
        db.query(model)
        .filter(
            func.lower(func.trim(model.name)) == wanted,
            model.is_active == True,
        )
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown {label}: '{name}'. Choose one of the available options.",
        )
    return row


def _resolve_option(db: Session, product: Product, chosen: str):
    """
    The product option the customer picked, or None if this product has none.

    A product with options is priced only from them - the global SizeRule table
    is not consulted for it - so a size that is not one of its options is not
    selectable, and saying so is the whole mechanism. No product is named here;
    the rule is the same for every one of them.

    A blank choice is rejected rather than defaulted. These options are not
    proportional to each other: a tea cake is Rs 800 as a loaf and Rs 1,700 as
    a round, so guessing which one the customer meant is guessing what to
    charge them.
    """
    options = [o for o in (product.options or []) if o.is_active]
    if not options:
        return None

    wanted = _normalise(chosen)
    if wanted:
        for option in options:
            if _normalise(option.label) == wanted:
                return option

    available = ", ".join(o.label for o in options)
    if not wanted:
        raise HTTPException(
            status_code=400,
            detail=f"Choose a size for '{product.name}': {available}.",
        )
    raise HTTPException(
        status_code=400,
        detail=(
            f"'{chosen}' is not available for '{product.name}'. "
            f"Choose one of: {available}."
        ),
    )


def _cost_of(db: Session, model, name: str, cost_field: str, label: str) -> float:
    row = _find_rule(db, model, name, label)
    return float(getattr(row, cost_field)) if row else 0.0


def lookup_delivery_charge(db: Session, delivery_zone_name: str | None) -> float:
    """
    Charge for a delivery zone. Blank means pickup (free); an unrecognised zone
    is rejected rather than treated as free delivery.
    """
    wanted = _normalise(delivery_zone_name)
    if not wanted:
        return 0.0

    zone = (
        db.query(DeliveryZone)
        .filter(
            func.lower(func.trim(DeliveryZone.area_name)) == wanted,
            DeliveryZone.is_active == True,
        )
        .first()
    )
    if zone is None:
        raise HTTPException(
            status_code=400,
            detail=f"We don't deliver to '{delivery_zone_name}'. Please choose a listed delivery area.",
        )
    return float(zone.charge)


def calculate_item_price(
    db: Session,
    product: Product,
    customization: ItemCustomization,
    quantity: int = 1,
    delivery_zone_name: str | None = None,
) -> PriceBreakdown:
    """Calculate the full price breakdown for a single order line."""

    if quantity < 1:
        # Defence in depth; the schema also constrains this. A zero or negative
        # quantity produced a free or negative line total.
        raise HTTPException(status_code=400, detail="Quantity must be at least 1")

    base_price = product.base_price

    # ── Size ──
    # Size is a pricing dimension only for per-kg products. For a fixed-price
    # item - a brownie, a loaf, a pack of six buns - base_price IS the price,
    # so a size cannot be allowed to multiply it: a Rs 400 brownie ordered at
    # "2kg" would have been billed Rs 800.
    #
    # A size sent for a fixed-price product is ignored rather than rejected.
    # Every existing client sends one unconditionally (the product page
    # hardcodes "1kg", the builder defaults to it, the WhatsApp flow always
    # asks), so rejecting would break checkout for these products the moment
    # the first one is created. Ignoring is safe by construction - the
    # multiplier is the literal 1.0 and no SizeRule is ever consulted - and
    # this can be tightened to a 400 once those callers stop sending a size.
    #
    # A product may instead carry its own options - the weights and shapes that
    # particular product is sold in. Those replace the global SizeRule table
    # for it entirely, which is how a cake that is not sold under 1kg stops
    # offering 500g without the engine knowing which cake it is.
    option = _resolve_option(db, product, customization.size)
    if option is not None:
        option_label = option.label
        size_adjusted = option.price_for(base_price)
        # Reported for the breakdown only. An option priced outright has no
        # multiplier to report, so it shows as 1.0 against its own price.
        size_multiplier = float(option.multiplier) if option.multiplier is not None else 1.0
    elif product.pricing_unit == "kg":
        option_label = _normalise(customization.size) and customization.size or ""
        size_row = _find_rule(db, SizeRule, customization.size, "size")
        size_multiplier = float(size_row.multiplier) if size_row else 1.0
        size_adjusted = round(base_price * size_multiplier, 2)
    else:
        option_label = ""
        size_multiplier = 1.0
        size_adjusted = round(base_price * size_multiplier, 2)

    # ── Flavor ──
    flavor_cost = _cost_of(db, FlavorRule, customization.flavor, "extra_cost", "flavour")

    # ── Design ──
    design_cost = _cost_of(db, DesignRule, customization.design, "cost", "design")

    # ── Addons (multiple) ──
    addon_details: dict[str, float] = {}
    addon_total = 0.0
    for addon_name in customization.addons:
        cost = _cost_of(db, AddonRule, addon_name, "cost", "addon")
        addon_details[addon_name] = cost
        addon_total += cost

    # ── Rush ──
    rush_cost = _cost_of(db, RushRule, customization.rush, "cost", "rush option")

    # ── Delivery ──
    # Reported here for display only. Delivery is charged ONCE per order (see
    # lookup_delivery_charge + create_order) — folding it into item_total would
    # bill it per line AND multiply it by quantity.
    delivery_charge = lookup_delivery_charge(db, delivery_zone_name)

    # ── Totals ──
    item_total = round(size_adjusted + flavor_cost + design_cost + addon_total + rush_cost, 2)
    line_total = round(item_total * quantity, 2)

    return PriceBreakdown(
        base_price=base_price,
        option_label=option_label,
        size_multiplier=size_multiplier,
        size_adjusted=size_adjusted,
        flavor_cost=flavor_cost,
        design_cost=design_cost,
        addon_cost=addon_total,
        addon_details=addon_details,
        rush_cost=rush_cost,
        delivery_charge=delivery_charge,
        item_total=item_total,
        quantity=quantity,
        line_total=line_total,
    )


def calculate_price(db: Session, request: PricingRequest) -> PricingResponse:
    """Public entry point — validates product exists and returns pricing."""
    product = db.query(Product).filter(Product.id == request.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail=f"Product {request.product_id} not found")

    breakdown = calculate_item_price(
        db=db,
        product=product,
        customization=request.customization,
        quantity=request.quantity,
        delivery_zone_name=request.delivery_zone,
    )
    return PricingResponse(breakdown=breakdown, total=breakdown.line_total)
