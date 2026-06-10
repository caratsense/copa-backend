"""
Pricing Engine — calculates the full price breakdown for any item + customization.

To add a new pricing dimension:
1. Create a new rule model in app/models/pricing.py
2. Add a lookup step in calculate_item_price() below
3. Add the cost to the breakdown
4. Register the admin CRUD route in app/api/routes/admin.py
"""

from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.product import Product
from app.models.pricing import SizeRule, FlavorRule, DesignRule, AddonRule, RushRule
from app.models.delivery import DeliveryZone
from app.schemas import ItemCustomization, PriceBreakdown, PricingRequest, PricingResponse


def _lookup_or_zero(db: Session, model, name_field: str, name: str, cost_field: str):
    """Generic helper: look up a rule by name and return its cost, or 0 if not found."""
    row = db.query(model).filter(
        getattr(model, name_field) == name,
        model.is_active == True,
    ).first()
    if row is None:
        return 0.0
    return getattr(row, cost_field)


def calculate_item_price(
    db: Session,
    product: Product,
    customization: ItemCustomization,
    quantity: int = 1,
    delivery_zone_name: str | None = None,
) -> PriceBreakdown:
    """Calculate the full price breakdown for a single order line."""

    base_price = product.base_price

    # ── Size ──
    size_row = db.query(SizeRule).filter(
        SizeRule.name == customization.size, SizeRule.is_active == True
    ).first()
    size_multiplier = size_row.multiplier if size_row else 1.0
    size_adjusted = round(base_price * size_multiplier, 2)

    # ── Flavor ──
    flavor_cost = _lookup_or_zero(db, FlavorRule, "name", customization.flavor, "extra_cost")

    # ── Design ──
    design_cost = _lookup_or_zero(db, DesignRule, "name", customization.design, "cost")

    # ── Addons (multiple) ──
    addon_details: dict[str, float] = {}
    addon_total = 0.0
    for addon_name in customization.addons:
        cost = _lookup_or_zero(db, AddonRule, "name", addon_name, "cost")
        addon_details[addon_name] = cost
        addon_total += cost

    # ── Rush ──
    rush_cost = _lookup_or_zero(db, RushRule, "name", customization.rush, "cost")

    # ── Delivery ──
    delivery_charge = 0.0
    if delivery_zone_name:
        zone = db.query(DeliveryZone).filter(
            DeliveryZone.area_name == delivery_zone_name, DeliveryZone.is_active == True
        ).first()
        if zone:
            delivery_charge = zone.charge

    # ── Totals ──
    item_total = round(size_adjusted + flavor_cost + design_cost + addon_total + rush_cost + delivery_charge, 2)
    line_total = round(item_total * quantity, 2)

    return PriceBreakdown(
        base_price=base_price,
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
