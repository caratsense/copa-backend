"""
Assignment Engine — Smart baker & rider assignment.

BAKER ASSIGNMENT:
- Auto-triggered when order status → CONFIRMED
- Picks baker with fewest active orders (ASSIGNED + IN_PRODUCTION + QC)
- Only considers on-duty, active bakers
- Ties broken by who got their last assignment earliest

RIDER ASSIGNMENT:
- Same logic as baker but for riders
- Considers PACKAGED + OUT_FOR_DELIVERY as active

TRANSFERS:
- Baker/admin can transfer an order to another on-duty baker
- Full event trail maintained
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi import HTTPException

from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus
from app.services.event_service import emit_event

logger = logging.getLogger(__name__)

# Sort sentinel meaning "never assigned before". Timezone-aware so it compares
# cleanly against created_at values coming back from Postgres.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _get_active_order_count(db: Session, user_id: int, statuses: list[OrderStatus], baker: bool = True) -> int:
    """Count active orders for a baker or rider."""
    field = Order.assigned_baker_id if baker else Order.assigned_rider_id
    return db.query(func.count(Order.id)).filter(
        field == user_id,
        Order.status.in_(statuses),
    ).scalar() or 0


def auto_assign_baker(db: Session, order_id: int, force: bool = False) -> Order:
    """
    Auto-assign the least-loaded baker to an order.
    force=True: admin-triggered, ignores on_duty status and store hours
    force=False: automatic, only assigns on-duty bakers during store hours
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.assigned_baker_id:
        return order  # already assigned

    # Get available bakers
    query = db.query(User).filter(
        User.role == UserRole.BAKER,
        User.is_active == True,
    )
    if not force:
        query = query.filter(User.on_duty == True)
    bakers = query.all()

    if not bakers:
        raise HTTPException(status_code=400, detail="No bakers available." + (" All are off-duty or inactive." if not force else " No active bakers found."))

    active_statuses = [OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.QC]

    # Build workload list: (baker, active_count, last_assignment_time)
    workloads = []
    for baker in bakers:
        count = _get_active_order_count(db, baker.id, active_statuses, baker=True)

        # Get last assignment time for tiebreaking
        last_order = (
            db.query(Order.created_at)
            .filter(Order.assigned_baker_id == baker.id)
            .order_by(Order.created_at.desc())
            .first()
        )
        last_time = last_order[0] if last_order else None

        workloads.append((baker, count, last_time))

    # Sort: lowest count first, then earliest last assignment (None = never assigned = top priority)
    # Never-assigned staff sort first. The previous key mixed datetime and int,
    # which raised TypeError as soon as one had a prior order and another did
    # not - the common case immediately after the first assignment.
    workloads.sort(key=lambda x: (x[1], x[2] is not None, x[2] or _EPOCH))

    chosen_baker = workloads[0][0]

    order.assigned_baker_id = chosen_baker.id

    emit_event(db, order.id, "BAKER_ASSIGNED", {
        "baker_id": chosen_baker.id,
        "baker_name": chosen_baker.name,
        "method": "auto",
        "workload": workloads[0][1],
    })

    db.commit()
    db.refresh(order)

    # The status change goes through the order service rather than being written
    # here. This used to set order.status directly, which meant CONFIRMED ->
    # ASSIGNED never emitted STATUS_CHANGED, never broadcast over the WebSocket,
    # and never called dispatch_notifications — so the baker's WhatsApp
    # assignment message existed in code but could never fire.
    return apply_baker_assignment_status(db, order)


def apply_baker_assignment_status(db: Session, order: Order) -> Order:
    """
    Move a freshly-assigned order to ASSIGNED through the central service.

    Only CONFIRMED orders transition — reassigning a baker to an order that is
    already in production must not drag its status backwards. In that case the
    baker still needs telling, so the assignment notification is sent directly.
    """
    from app.schemas import StatusUpdate
    from app.services.order_service import update_order_status

    if order.status == OrderStatus.CONFIRMED:
        return update_order_status(db, order.id, StatusUpdate(status="ASSIGNED"))

    # Mid-flight reassignment: no status change, but the new baker is owed the
    # same message they would have received on a fresh assignment.
    try:
        from app.services.wa_notifications import notify_baker_assigned
        notify_baker_assigned(db, order)
    except Exception as e:
        logger.error("[Assign] baker notification failed for order %s: %s", order.id, e)
    return order


def notify_rider_if_already_packaged(db: Session, order: Order) -> None:
    """
    Tell a rider about a delivery assigned to an order that is ALREADY packaged.

    The PACKAGED transition notifies the rider itself, so this must only fire
    when no such transition is coming — otherwise the rider gets the same
    delivery twice. Assigning a rider after packaging previously notified
    nobody at all.
    """
    if order.status != OrderStatus.PACKAGED or not order.assigned_rider_id:
        return
    try:
        from app.services.wa_notifications import notify_rider_assigned
        notify_rider_assigned(db, order)
    except Exception as e:
        logger.error("[Assign] rider notification failed for order %s: %s", order.id, e)


def admin_assign_baker(db: Session, order_id: int, baker_id: int) -> Order:
    """Admin manually assigns a specific baker."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    baker = db.query(User).filter(
        User.id == baker_id,
        User.role == UserRole.BAKER,
    ).first()
    if not baker:
        raise HTTPException(status_code=404, detail="Baker not found")

    old_baker_id = order.assigned_baker_id
    order.assigned_baker_id = baker.id

    emit_event(db, order.id, "BAKER_ASSIGNED", {
        "baker_id": baker.id,
        "baker_name": baker.name,
        "method": "admin_manual",
        "previous_baker_id": old_baker_id,
    })

    db.commit()
    db.refresh(order)

    # Same path as auto-assignment: transition through the order service so the
    # baker is actually notified, instead of being assigned silently.
    return apply_baker_assignment_status(db, order)


def transfer_order_baker(db: Session, order_id: int, from_baker_id: int, to_baker_id: int) -> Order:
    """Transfer an order from one baker to another."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.assigned_baker_id != from_baker_id:
        raise HTTPException(status_code=403, detail="This order is not assigned to you")

    to_baker = db.query(User).filter(
        User.id == to_baker_id,
        User.role == UserRole.BAKER,
        User.is_active == True,
        User.on_duty == True,
    ).first()
    if not to_baker:
        raise HTTPException(status_code=400, detail="Target baker not found, inactive, or off-duty")

    from_baker = db.query(User).filter(User.id == from_baker_id).first()

    order.assigned_baker_id = to_baker.id

    emit_event(db, order.id, "BAKER_TRANSFERRED", {
        "from_baker_id": from_baker_id,
        "from_baker_name": from_baker.name if from_baker else "Unknown",
        "to_baker_id": to_baker.id,
        "to_baker_name": to_baker.name,
    })

    db.commit()
    db.refresh(order)
    return order


def auto_assign_rider(db: Session, order_id: int, force: bool = False) -> Order:
    """Auto-assign the least-loaded rider. force=True ignores on_duty."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.assigned_rider_id:
        return order

    query = db.query(User).filter(
        User.role == UserRole.RIDER,
        User.is_active == True,
    )
    if not force:
        query = query.filter(User.on_duty == True)
    riders = query.all()

    if not riders:
        raise HTTPException(status_code=400, detail="No riders available.")

    active_statuses = [OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY]

    workloads = []
    for rider in riders:
        count = _get_active_order_count(db, rider.id, active_statuses, baker=False)
        last_order = (
            db.query(Order.created_at)
            .filter(Order.assigned_rider_id == rider.id)
            .order_by(Order.created_at.desc())
            .first()
        )
        last_time = last_order[0] if last_order else None
        workloads.append((rider, count, last_time))

    # Never-assigned staff sort first. The previous key mixed datetime and int,
    # which raised TypeError as soon as one had a prior order and another did
    # not - the common case immediately after the first assignment.
    workloads.sort(key=lambda x: (x[1], x[2] is not None, x[2] or _EPOCH))
    chosen_rider = workloads[0][0]

    order.assigned_rider_id = chosen_rider.id

    emit_event(db, order.id, "RIDER_ASSIGNED", {
        "rider_id": chosen_rider.id,
        "rider_name": chosen_rider.name,
        "method": "auto",
    })

    db.commit()
    db.refresh(order)
    return order


def rider_self_accept(db: Session, order_id: int, rider_id: int) -> Order:
    """Rider self-assigns to an unassigned order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.assigned_rider_id:
        raise HTTPException(status_code=400, detail="Order already has a rider assigned")

    rider = db.query(User).filter(User.id == rider_id).first()

    order.assigned_rider_id = rider_id

    emit_event(db, order.id, "RIDER_ASSIGNED", {
        "rider_id": rider_id,
        "rider_name": rider.name if rider else "Unknown",
        "method": "self_assigned",
    })

    db.commit()
    db.refresh(order)
    return order
