"""
Order Routes — complete with auth, payments, tracking, rider self-service.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.uploads import save_image
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
from app.services.event_service import emit_event

router = APIRouter(prefix="/orders", tags=["Orders"])


# ─── CUSTOMER ─────────────────────────────────────────

@router.post("", response_model=OrderRead, status_code=201)
def create(data: OrderCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a new order — prices calculated automatically, coupons applied."""
    data.user_id = user.id
    return create_order(db, data)


@router.get("/my-orders", response_model=list[OrderTrackingRead])
def my_orders(skip: int = 0, limit: int = 20, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Customer order history."""
    return get_user_orders(db, user.id, skip, limit)


@router.get("/track/{order_id}", response_model=OrderTrackingRead)
def track_order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Customer order tracking."""
    order = get_order(db, order_id)
    # The customer, an admin, or the baker/rider actually working on it. Any
    # baker or rider used to be let through, which by walking order ids gave
    # every member of staff every customer's delivery address.
    allowed = (
        order.user_id == user.id
        or user.role == UserRole.ADMIN
        or (user.role == UserRole.BAKER and order.assigned_baker_id == user.id)
        or (user.role == UserRole.RIDER and order.assigned_rider_id == user.id)
    )
    if not allowed:
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
    """Update order status - enforces lifecycle. Auto-assigns baker on CONFIRMED."""
    return update_order_status(db, order_id, data, rework=data.rework)


@router.patch("/{order_id}/payment", response_model=OrderRead)
def update_payment(order_id: int, data: PaymentUpdate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return update_payment_status(db, order_id, data)


@router.post("/{order_id}/assign-rider", response_model=OrderRead)
def do_assign_rider(order_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Auto-assign least-loaded rider."""
    order = auto_assign_rider(db, order_id)
    # Same reason as the manual path: no PACKAGED transition is coming for an
    # order that is already packaged, so notify here or the rider hears nothing.
    from app.services.assignment_engine import notify_rider_if_already_packaged
    notify_rider_if_already_packaged(db, order)
    return order


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
            Order.status.in_([
                OrderStatus.PACKAGED,
                OrderStatus.OUT_FOR_DELIVERY,
                # Kept visible after a failed attempt so it does not simply
                # disappear from the rider's queue when they report it.
                OrderStatus.DELIVERY_FAILED,
            ])
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

    # Tracking teardown is part of the DELIVERED transition itself now, so every
    # path that delivers an order (rider app, admin status update, WhatsApp)
    # cleans up. Calling it here as well would just duplicate the work.
    return update_order_status(db, order_id, StatusUpdate(status="DELIVERED"))


class DeliveryFailure(BaseModel):
    """Why a delivery could not be completed."""
    reason: str = Field(min_length=1, max_length=300)


@router.post("/{order_id}/delivery-failed", response_model=OrderRead)
def rider_delivery_failed(
    order_id: int,
    data: DeliveryFailure,
    user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Record a delivery that was attempted and could not be completed.

    Before this existed the only way out of OUT_FOR_DELIVERY was DELIVERED, so
    a rider who found nobody home had to mark the order delivered - which sent
    the customer a message saying their cake had arrived. The order now moves
    to DELIVERY_FAILED with the reason on its timeline, and no arrival message
    is sent. It can be sent out again or cancelled from there.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.assigned_rider_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if order.status != OrderStatus.OUT_FOR_DELIVERY:
        raise HTTPException(
            status_code=400,
            detail=f"Must be OUT_FOR_DELIVERY. Current: {order.status.value}",
        )

    return update_order_status(
        db, order_id,
        StatusUpdate(status="DELIVERY_FAILED", reason=data.reason),
    )


@router.post("/{order_id}/retry-delivery", response_model=OrderRead)
def retry_delivery(
    order_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Send a failed delivery back out. Tracking restarts with the transition."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.DELIVERY_FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Only a failed delivery can be sent out again. Current: {order.status.value}",
        )
    if not order.assigned_rider_id:
        raise HTTPException(status_code=400, detail="Assign a rider before sending it out again")

    return update_order_status(db, order_id, StatusUpdate(status="OUT_FOR_DELIVERY"))




# ─── PROOF OF WORK ────────────────────────────────────
#
# "Baked" and "delivered" used to be buttons. Anyone holding the role could
# advance an order by pressing one, and the shop's only record that a cake had
# been made, checked, or handed to anybody was that somebody had pressed it.
#
# Three photographs close that gap, and the gates that require them live in
# update_order_status so every caller - website, admin table, WhatsApp - meets
# the same rule:
#
#   proof_image      the baker's shot of the finished cake. Required before
#                    the cake can be offered for approval.
#   proof_verified   Shriya looking at that shot and saying yes. Required
#                    before the cake can be packed.
#   handover_image   the doorstep. Required before a delivery can be closed,
#                    because nobody at the shop sees it happen. A collection
#                    is handed over the counter in person and needs no photo.


@router.post("/{order_id}/proof", response_model=OrderRead)
def upload_bake_proof(
    order_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """
    The baker photographs the finished cake.

    Re-uploading is allowed and deliberately clears any previous approval: if
    the photo changes, the thing Shriya approved is not the thing in the box,
    and the cake has to be looked at again.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.assigned_baker_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if order.status in (OrderStatus.DELIVERED, OrderStatus.COLLECTED, OrderStatus.CANCELLED):
        raise HTTPException(status_code=400, detail="This order is closed")

    order.proof_image = save_image(file, prefix=f"bake_{order_id}")
    order.proof_verified_at = None
    order.proof_verified_by = None
    emit_event(db, order.id, "BAKE_PROOF_UPLOADED", {
        "by": user.name or "Baker",
        "by_id": user.id,
        "note": f"{user.name or 'Baker'} photographed the finished cake",
    })
    db.commit()
    db.refresh(order)

    # Tell the shop there is something to look at. Best effort: the photo is
    # saved either way, and a messaging outage must not lose it.
    try:
        from app.services.wa_notifications import notify_admin_proof_ready
        notify_admin_proof_ready(db, order)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[WA] proof-ready notify failed for {order_id}: {e}")

    _enrich_order(order)
    return order


@router.post("/{order_id}/verify-proof", response_model=OrderRead)
def verify_bake_proof(
    order_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Shriya has looked at the photo and is happy. The cake may now be packed."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not order.proof_image:
        raise HTTPException(status_code=400, detail="There is no photo to check yet")

    order.proof_verified_at = datetime.now(timezone.utc)
    order.proof_verified_by = user.id
    emit_event(db, order.id, "BAKE_PROOF_VERIFIED", {
        "by": user.name or "Admin",
        "by_id": user.id,
        "note": f"{user.name or 'Admin'} checked the photo and approved the cake",
    })
    db.commit()
    db.refresh(order)
    _enrich_order(order)
    return order


class ProofRejection(BaseModel):
    """What was wrong with the cake, in the baker's words rather than a code."""
    reason: str = Field(min_length=1, max_length=300)


@router.post("/{order_id}/reject-proof", response_model=OrderRead)
def reject_bake_proof(
    order_id: int,
    data: ProofRejection,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Shriya is not happy with the cake. It goes back to the kitchen.

    The photo is cleared along with the approval, because the next thing the
    baker does is make it right and photograph it again - and leaving the old
    picture in place would let the cake through on a shot of the rejected one.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.proof_image = None
    order.proof_verified_at = None
    order.proof_verified_by = None
    emit_event(db, order.id, "BAKE_PROOF_REJECTED", {
        "by": user.name or "Admin",
        "by_id": user.id,
        "reason": data.reason,
        "note": f"Sent back by {user.name or 'Admin'}: {data.reason}",
    })
    db.commit()

    # Back to the bench if it had already been offered for approval. If it is
    # still in production there is nothing to move - the baker simply has no
    # approved photo to advance with.
    if order.status == OrderStatus.AWAITING_APPROVAL:
        return update_order_status(
            db, order_id, StatusUpdate(status="IN_PRODUCTION", reason=data.reason), rework=True
        )
    db.refresh(order)
    _enrich_order(order)
    return order


@router.post("/{order_id}/handover", response_model=OrderRead)
def upload_handover_proof(
    order_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_role(UserRole.RIDER, UserRole.BAKER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """
    The handover shot - the doorstep. Required before a delivery can be closed.

    A rider may only do this for their own delivery. A collection does not need
    one, but the shop may still record a counter handover if it wants to, which
    is why a baker and an admin can post here too.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if (not order.is_pickup
            and user.role == UserRole.RIDER
            and order.assigned_rider_id != user.id):
        raise HTTPException(status_code=403, detail="Not assigned to you")
    if order.status in (OrderStatus.DELIVERED, OrderStatus.COLLECTED, OrderStatus.CANCELLED):
        raise HTTPException(status_code=400, detail="This order is closed")

    order.handover_image = save_image(file, prefix=f"handover_{order_id}")
    emit_event(db, order.id, "HANDOVER_PHOTOGRAPHED", {
        "by": user.name or "Staff",
        "by_id": user.id,
        "at": "counter" if order.is_pickup else "door",
        "note": ("Photographed at the counter" if order.is_pickup
                 else f"Photographed at the door by {user.name or 'the rider'}"),
    })
    db.commit()
    db.refresh(order)
    _enrich_order(order)
    return order


# ─── CUSTOMER CANCELLATION ────────────────────────────

class CancelReason(BaseModel):
    """Optional, and free text. Most people will not give one."""
    reason: Optional[str] = Field(default=None, max_length=300)


@router.post("/{order_id}/cancel", response_model=OrderRead)
def cancel_own_order(
    order_id: int,
    data: CancelReason | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    The customer calls off their own order.

    There was no way to do this. CANCELLED existed as a status and the admin
    table could set it, but a customer looking at an order they no longer
    wanted had one button on the page and it said Pay. The only way out was to
    not pay and leave it sitting there, which is indistinguishable from a
    payment that failed - so the shop could not tell an abandoned order from a
    cancelled one, and neither could the customer.

    Two limits, both deliberate:

    - Unpaid only. Cancelling a paid order is a refund, and a refund is the
      shop's decision and PayU's API, not a button. A paid customer is pointed
      at the shop, and an admin can still cancel from the admin table.
    - Before the kitchen commits. Once a baker has been assigned, ingredients
      are out and the cake is someone's afternoon. RECEIVED and CONFIRMED are
      the two states where nothing has been started yet.

    Routed through update_order_status rather than setting the column here, so
    the cancellation emits its event, broadcasts, and notifies the same way
    every other transition does.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # An admin cancelling on a customer's behalf goes through the admin table,
    # which has no such limits. This endpoint is the customer's own.
    if order.user_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="This is not your order")

    if order.status == OrderStatus.CANCELLED:
        # Already done. Saying so beats a 400 on a second tap or a stale page.
        _enrich_order(order)
        return order

    if order.payment_status == PaymentStatus.PAID:
        raise HTTPException(
            status_code=400,
            detail=("This order is already paid for, so cancelling it means a refund. "
                    "Please contact the shop and we'll sort it out for you."),
        )

    if order.status not in (OrderStatus.RECEIVED, OrderStatus.CONFIRMED):
        raise HTTPException(
            status_code=400,
            detail=("We've already started making this one, so it can't be cancelled "
                    "here. Please contact the shop."),
        )

    reason = (data.reason if data else None) or "Cancelled by the customer"
    return update_order_status(db, order.id, StatusUpdate(status="CANCELLED", reason=reason))
