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

from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi import HTTPException

from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus
from app.services.event_service import emit_event


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
    workloads.sort(key=lambda x: (x[1], x[2] if x[2] else 0))

    chosen_baker = workloads[0][0]

    order.assigned_baker_id = chosen_baker.id
    order.status = OrderStatus.ASSIGNED

    emit_event(db, order.id, "BAKER_ASSIGNED", {
        "baker_id": chosen_baker.id,
        "baker_name": chosen_baker.name,
        "method": "auto",
        "workload": workloads[0][1],
    })

    emit_event(db, order.id, "STATUS_CHANGED", {
        "from": "CONFIRMED",
        "to": "ASSIGNED",
    })

    db.commit()
    db.refresh(order)
    return order


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
    return order


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

    workloads.sort(key=lambda x: (x[1], x[2] if x[2] else 0))
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
