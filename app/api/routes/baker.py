"""
Baker Routes
=============
- Baker queue (my orders)
- Start baking / mark done
- Toggle duty status
- Transfer order to another baker
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus
from app.core.auth import require_role, get_current_user
from app.schemas import OrderRead, StatusUpdate, DutyToggle, TransferRequest
from app.services.order_service import update_order_status, _enrich_order
from app.services.event_service import emit_event
from app.services.assignment_engine import transfer_order_baker

router = APIRouter(prefix="/baker", tags=["Baker"])


@router.get("/my-queue", response_model=list[OrderRead])
def baker_queue(
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """Get all active orders assigned to this baker, sorted by delivery time."""
    orders = (
        db.query(Order)
        .filter(
            Order.assigned_baker_id == user.id,
            Order.status.in_([
                OrderStatus.ASSIGNED,
                OrderStatus.IN_PRODUCTION,
                OrderStatus.QC,
            ])
        )
        .order_by(Order.delivery_time.asc().nullslast(), Order.created_at.asc())
        .all()
    )
    for o in orders:
        _enrich_order(o)
    return orders


@router.get("/completed", response_model=list[OrderRead])
def baker_completed(
    skip: int = 0,
    limit: int = 20,
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """Get baker's completed orders (for their history)."""
    return (
        db.query(Order)
        .filter(
            Order.assigned_baker_id == user.id,
            Order.status.in_([OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED])
        )
        .order_by(Order.updated_at.desc())
        .offset(skip).limit(limit)
        .all()
    )


@router.post("/orders/{order_id}/start-baking", response_model=OrderRead)
def start_baking(
    order_id: int,
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """Baker starts working on an order: ASSIGNED → IN_PRODUCTION."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.assigned_baker_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="This order is not assigned to you")

    if order.status != OrderStatus.ASSIGNED:
        raise HTTPException(
            status_code=400,
            detail=f"Can only start baking from ASSIGNED status. Current: {order.status.value}"
        )

    return update_order_status(db, order_id, StatusUpdate(status="IN_PRODUCTION"))


@router.post("/orders/{order_id}/baking-done", response_model=OrderRead)
def baking_done(
    order_id: int,
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """Baker marks baking as complete: IN_PRODUCTION → AWAITING_APPROVAL (Shriya must approve before delivery)."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.assigned_baker_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="This order is not assigned to you")

    if order.status != OrderStatus.IN_PRODUCTION:
        raise HTTPException(
            status_code=400,
            detail=f"Can only mark done from IN_PRODUCTION. Current: {order.status.value}"
        )

    return update_order_status(db, order_id, StatusUpdate(status="AWAITING_APPROVAL"))


@router.patch("/duty-status", response_model=dict)
def toggle_duty(
    data: DutyToggle,
    user: User = Depends(require_role(UserRole.BAKER, UserRole.RIDER)),
    db: Session = Depends(get_db),
):
    """Baker/rider toggles their own duty status."""
    user.on_duty = data.on_duty
    db.commit()
    return {
        "id": user.id,
        "name": user.name,
        "on_duty": user.on_duty,
        "message": f"You are now {'on duty' if user.on_duty else 'off duty'}",
    }


@router.post("/orders/{order_id}/transfer", response_model=OrderRead)
def transfer(
    order_id: int,
    data: TransferRequest,
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """Transfer an order to another baker."""
    return transfer_order_baker(db, order_id, user.id, data.to_baker_id)


@router.get("/available-bakers", response_model=list[dict])
def list_available_bakers(
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """List on-duty bakers (for transfer selection)."""
    bakers = db.query(User).filter(
        User.role == UserRole.BAKER,
        User.is_active == True,
        User.on_duty == True,
    ).all()

    result = []
    for b in bakers:
        active_count = (
            db.query(Order)
            .filter(
                Order.assigned_baker_id == b.id,
                Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.QC])
            )
            .count()
        )
        result.append({
            "id": b.id,
            "name": b.name,
            "active_orders": active_count,
        })

    return sorted(result, key=lambda x: x["active_orders"])
