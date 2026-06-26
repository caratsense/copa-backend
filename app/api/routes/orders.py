"""
Order Routes — complete with auth, payments, tracking, rider self-service.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.event import OrderEvent
from app.core.auth import get_current_user, require_admin, require_role
from app.schemas import (
    OrderCreate, OrderRead, StatusUpdate, PaymentUpdate,
    EventRead, OrderTrackingRead,
)
from app.services.order_service import (
    create_order, update_order_status, update_payment_status,
    get_order, list_orders, get_user_orders, _enrich_order,
)
from app.services.assignment_engine import auto_assign_rider, rider_self_accept

router = APIRouter(prefix="/orders", tags=["Orders"])


# ─── CUSTOMER ─────────────────────────────────────────

@router.post("", response_model=OrderRead, status_code=201)
def create(data: OrderCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a new order — prices calculated automatically, coupons applied."""
    try:
        data.user_id = user.id
        return create_order(db, data)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(status_code=500, detail=f"Order creation failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")


@router.get("/my-orders", response_model=list[OrderTrackingRead])
def my_orders(skip: int = 0, limit: int = 20, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Customer order history."""
    return get_user_orders(db, user.id, skip, limit)


@router.get("/track/{order_id}", response_model=OrderTrackingRead)
def track_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Customer order tracking."""
    order = get_order(db, order_id)
    if order.user_id != user.id and user.role.value not in ("admin", "baker", "rider"):
        raise HTTPException(status_code=403, detail="You can only track your own orders")
    return order


# ─── ADMIN ────────────────────────────────────────────

@router.get("", response_model=list[OrderRead])
def list_all(skip: int = 0, limit: int = 50, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list_orders(db, skip, limit)


@router.get("/{order_id}", response_model=OrderRead)
def get(order_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return get_order(db, order_id)


@router.patch("/{order_id}/status", response_model=OrderRead)
def update_status(order_id: int, data: StatusUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Update order status — enforces lifecycle. Auto-assigns baker on CONFIRMED."""
    return update_order_status(db, order_id, data)


@router.patch("/{order_id}/payment", response_model=OrderRead)
def update_payment(order_id: int, data: PaymentUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return update_payment_status(db, order_id, data)


@router.post("/{order_id}/assign-rider", response_model=OrderRead)
def do_assign_rider(order_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Auto-assign least-loaded rider."""
    return auto_assign_rider(db, order_id)


@router.get("/{order_id}/events", response_model=list[EventRead])
def get_events(order_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return (
        db.query(OrderEvent)
        .filter(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.created_at.asc())
        .all()
    )


# ─── RIDER SELF-SERVICE ──────────────────────────────

@router.get("/rider/my-queue", response_model=list[OrderRead])
def rider_queue(user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)), db: Session = Depends(get_db)):
    """Rider's active delivery queue."""
    orders = (
        db.query(Order)
        .filter(
            Order.assigned_rider_id == user.id,
            Order.status.in_([OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY])
        )
        .order_by(Order.created_at.asc())
        .all()
    )
    for o in orders:
        _enrich_order(o)
    return orders


@router.post("/{order_id}/rider-accept", response_model=OrderRead)
def rider_accept(order_id: int, user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)), db: Session = Depends(get_db)):
    """Rider self-assigns to an unassigned order."""
    return rider_self_accept(db, order_id, user.id)


@router.post("/{order_id}/rider-pickup", response_model=OrderRead)
def rider_pickup(order_id: int, user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)), db: Session = Depends(get_db)):
    """Rider picks up order: PACKAGED → OUT_FOR_DELIVERY."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.assigned_rider_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if order.status != OrderStatus.PACKAGED:
        raise HTTPException(status_code=400, detail=f"Must be PACKAGED. Current: {order.status.value}")
    return update_order_status(db, order_id, StatusUpdate(status="OUT_FOR_DELIVERY"))


@router.post("/{order_id}/rider-delivered", response_model=OrderRead)
def rider_delivered(order_id: int, user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)), db: Session = Depends(get_db)):
    """Rider marks order as delivered."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.assigned_rider_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if order.status != OrderStatus.OUT_FOR_DELIVERY:
        raise HTTPException(status_code=400, detail=f"Must be OUT_FOR_DELIVERY. Current: {order.status.value}")

    from app.services.delivery_tracking import stop_delivery_tracking
    stop_delivery_tracking(order_id)

    return update_order_status(db, order_id, StatusUpdate(status="DELIVERED"))


@router.post("/{order_id}/collect-cod", response_model=OrderRead)
def rider_collect_cod(order_id: int, user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)), db: Session = Depends(get_db)):
    """Rider marks a COD order as paid after collecting cash on delivery."""
    from app.services.event_service import emit_event

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.assigned_rider_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if (order.payment_method or "ONLINE").upper() != "COD":
        raise HTTPException(status_code=400, detail="Only COD orders can be marked paid here")
    if order.payment_status == PaymentStatus.PAID:
        raise HTTPException(status_code=400, detail="Order is already paid")

    old_payment = order.payment_status.value if hasattr(order.payment_status, "value") else order.payment_status
    order.payment_status = PaymentStatus.PAID
    emit_event(db, order.id, "PAYMENT_UPDATED", {
        "from": old_payment,
        "to": PaymentStatus.PAID.value,
        "method": "COD",
        "collected_by_rider_id": user.id,
    })
    db.commit()
    db.refresh(order)
    _enrich_order(order)
    return order
