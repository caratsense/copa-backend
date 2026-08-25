"""
Admin CRUD routes for pricing rules, delivery zones, etc.

Each rule type gets: Create, List, Update (toggle active), Delete.
All write operations require admin role (via X-User-Id header).
List (GET) is open to everyone so the pricing engine and frontend can read rules.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.pricing import SizeRule, FlavorRule, DesignRule, AddonRule, RushRule
from app.models.delivery import DeliveryZone
from app.core.auth import require_admin
from app.schemas import (
    SizeRuleCreate, SizeRuleRead,
    FlavorRuleCreate, FlavorRuleRead,
    DesignRuleCreate, DesignRuleRead,
    AddonRuleCreate, AddonRuleRead,
    RushRuleCreate, RushRuleRead,
    DeliveryZoneCreate, DeliveryZoneRead,
)

router = APIRouter(prefix="/admin", tags=["Admin — Pricing Rules"])


# ─── GENERIC CRUD FACTORY ─────────────────────────────
# Reduces repetition — each rule type uses the same pattern.

def _build_crud(prefix: str, model, create_schema, read_schema):
    """Register standard CRUD endpoints for a pricing rule model."""

    @router.post(
        f"/{prefix}", response_model=read_schema, status_code=201, name=f"create_{prefix}",
    )
    def create(data: create_schema, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
        obj = model(**data.model_dump())
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    @router.get(f"/{prefix}", response_model=list[read_schema], name=f"list_{prefix}")
    def list_all(active_only: bool = True, db: Session = Depends(get_db)):
        q = db.query(model)
        if active_only:
            q = q.filter(model.is_active == True)
            if hasattr(model, "stock"):
                from sqlalchemy import or_
                q = q.filter(or_(model.stock.is_(None), model.stock > 0))
        return q.all()

    @router.patch(
        f"/{prefix}/{{item_id}}/toggle", response_model=read_schema, name=f"toggle_{prefix}",
    )
    def toggle_active(item_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
        obj = db.query(model).filter(model.id == item_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail=f"{prefix} rule not found")
        obj.is_active = not obj.is_active
        db.commit()
        db.refresh(obj)
        return obj

    @router.delete(
        f"/{prefix}/{{item_id}}", status_code=204, name=f"delete_{prefix}",
    )
    def delete(item_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
        obj = db.query(model).filter(model.id == item_id).first()
        if not obj:
            raise HTTPException(status_code=404, detail=f"{prefix} rule not found")
        db.delete(obj)
        db.commit()


# Register all rule types
_build_crud("sizes", SizeRule, SizeRuleCreate, SizeRuleRead)
_build_crud("flavors", FlavorRule, FlavorRuleCreate, FlavorRuleRead)
_build_crud("designs", DesignRule, DesignRuleCreate, DesignRuleRead)
_build_crud("addons", AddonRule, AddonRuleCreate, AddonRuleRead)
_build_crud("rush", RushRule, RushRuleCreate, RushRuleRead)
_build_crud("delivery-zones", DeliveryZone, DeliveryZoneCreate, DeliveryZoneRead)


# ─── ADDON STOCK UPDATE ──────────────────────────────

class AddonStockUpdate(PydanticBaseModel):
    stock: int | None


@router.patch("/addons/{item_id}/stock", response_model=AddonRuleRead)
def set_addon_stock(
    item_id: int,
    data: AddonStockUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin sets exact stock count for an addon. Pass null for unlimited."""
    addon = db.query(AddonRule).filter(AddonRule.id == item_id).first()
    if not addon:
        raise HTTPException(status_code=404, detail="Addon not found")
    addon.stock = data.stock
    db.commit()
    db.refresh(addon)
    return addon


# ─── MANUAL ASSIGNMENT ────────────────────────────────

from app.models.order import Order, OrderStatus
from app.services.assignment_engine import auto_assign_baker, auto_assign_rider
from pydantic import BaseModel as PydanticBaseModel


class ManualAssign(PydanticBaseModel):
    staff_id: int


@router.post("/orders/{order_id}/assign-baker")
def admin_assign_baker(
    order_id: int,
    data: ManualAssign = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin manually assigns a baker. If staff_id provided, assigns that specific baker. Otherwise auto-assigns (force=True, ignores duty status)."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if data and data.staff_id:
        from app.models.user import UserRole
        baker = db.query(User).filter(User.id == data.staff_id, User.role == UserRole.BAKER, User.is_active == True).first()
        if not baker:
            raise HTTPException(status_code=400, detail="Baker not found or inactive")
        order.assigned_baker_id = baker.id
        if order.status == OrderStatus.CONFIRMED:
            order.status = OrderStatus.ASSIGNED
        db.commit()
        db.refresh(order)
        return {"message": f"Baker {baker.name} assigned to order #{order.id}", "order_id": order.id, "baker_id": baker.id}
    else:
        result = auto_assign_baker(db, order_id, force=True)
        return {"message": f"Baker auto-assigned to order #{order.id}", "order_id": order.id, "baker_id": result.assigned_baker_id}


@router.post("/orders/{order_id}/assign-rider")
def admin_assign_rider(
    order_id: int,
    data: ManualAssign = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin manually assigns a rider. force=True, ignores duty status."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if data and data.staff_id:
        from app.models.user import UserRole
        rider = db.query(User).filter(User.id == data.staff_id, User.role == UserRole.RIDER, User.is_active == True).first()
        if not rider:
            raise HTTPException(status_code=400, detail="Rider not found or inactive")
        previous_rider_id = order.assigned_rider_id
        order.assigned_rider_id = rider.id
        db.commit()
        db.refresh(order)

        # Reassigning a delivery that is already on the road: repoint the live
        # tracking state so the fleet view stops crediting the previous rider's
        # position to this order.
        if previous_rider_id != rider.id:
            from app.core.broadcast import fleet_broadcast_sync

            if order.status == OrderStatus.OUT_FOR_DELIVERY:
                from app.core.broadcast import rider_reassigned_sync
                from app.services.delivery_tracking import set_tracking_rider

                set_tracking_rider(order.id, rider.id)
                # Also disconnects the previous rider's GPS socket, so their
                # next ping cannot rewrite the position just cleared.
                rider_reassigned_sync(order.id, {
                    "order_id": order.id,
                    "rider_id": rider.id,
                    "rider_name": rider.name,
                    "previous_rider_id": previous_rider_id,
                    # The new rider has not sent a fix yet — the old rider's last
                    # position is not theirs to inherit.
                    "tracking_state": "awaiting_gps",
                })
            elif order.status == OrderStatus.PACKAGED:
                # Not on the road yet, so there is no tracking state to move —
                # the fleet list just needs to re-read who is carrying it.
                fleet_broadcast_sync({"type": "fleet_changed", "order_id": order.id})

        return {"message": f"Rider {rider.name} assigned to order #{order.id}", "order_id": order.id, "rider_id": rider.id}
    else:
        result = auto_assign_rider(db, order_id, force=True)
        return {"message": f"Rider auto-assigned to order #{order.id}", "order_id": order.id, "rider_id": result.assigned_rider_id}

