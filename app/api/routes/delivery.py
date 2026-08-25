"""
Delivery Tracking REST Routes
===============================
REST endpoints for delivery tracking management.
WebSocket handles real-time GPS — these handle setup and queries.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.db import get_db
from app.models.user import User
from app.models.order import Order, OrderStatus
from app.core.auth import can_observe_delivery, get_current_user, require_admin, require_role
from app.models.user import UserRole
from app.schemas import FleetSnapshot
from app.services.delivery_tracking import (
    start_delivery_tracking,
    get_rider_location,
    stop_delivery_tracking,
    redis_available,
)
from app.services.fleet_tracking import build_snapshot

router = APIRouter(prefix="/delivery", tags=["Delivery Tracking"])


# ─── SCHEMAS ──────────────────────────────────────────

class StartTrackingRequest(BaseModel):
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float

class LocationResponse(BaseModel):
    order_id: int
    rider_lat: Optional[float] = None
    rider_lng: Optional[float] = None
    updated_at: Optional[str] = None
    dropoff_lat: Optional[float] = None
    dropoff_lng: Optional[float] = None
    pickup_lat: Optional[float] = None
    pickup_lng: Optional[float] = None
    eta_minutes: Optional[float] = None
    status: str = "unknown"
    # Age of the fix, so a caller can tell "live" from "last seen 4 minutes ago"
    # without having to parse and compare timestamps itself.
    seconds_since_update: Optional[float] = None


# ─── ROUTES ───────────────────────────────────────────
# NOTE: /admin/active is declared before the /{order_id}/... routes on purpose.
# FastAPI matches in declaration order, and "admin" would otherwise be captured
# by the int path parameter and rejected as a 422.

@router.get("/admin/active", response_model=FleetSnapshot)
def admin_active_deliveries(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Initial state for the admin live-delivery view: every delivery currently in
    a rider's hands, plus the rider roster.

    Active orders come from PostgreSQL, live positions from a single pipelined
    Redis read. Pair this with `WS /ws/delivery/admin` for updates — do not poll
    it on a timer.

    Redis being down degrades this rather than failing it: the orders still
    list, `live_tracking_available` reports false, and no delivery is given a
    fabricated position.
    """
    snapshot = build_snapshot(db)
    return FleetSnapshot(**snapshot, live_tracking_available=redis_available())


@router.post("/{order_id}/start-tracking", response_model=dict)
def start_tracking(
    order_id: int,
    data: StartTrackingRequest,
    user: User = Depends(require_role(UserRole.RIDER, UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Start GPS tracking for a delivery — stores pickup/dropoff so ETA can be computed.
    Call this when the order status changes to OUT_FOR_DELIVERY.

    The assigned rider can call this for their own delivery (they're the one with
    the coordinates); admins can call it for any order.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if user.role != UserRole.ADMIN and order.assigned_rider_id != user.id:
        raise HTTPException(status_code=403, detail="This delivery is not assigned to you")

    if order.status != OrderStatus.OUT_FOR_DELIVERY:
        raise HTTPException(
            status_code=400,
            detail=f"Order must be OUT_FOR_DELIVERY to start tracking. Current: {order.status.value}"
        )

    result = start_delivery_tracking(
        order_id=order_id,
        rider_id=order.assigned_rider_id or 0,
        pickup_lat=data.pickup_lat,
        pickup_lng=data.pickup_lng,
        dropoff_lat=data.dropoff_lat,
        dropoff_lng=data.dropoff_lng,
    )

    return {
        "message": "Tracking started",
        "order_id": order_id,
        **result,
    }


@router.get("/{order_id}/location", response_model=LocationResponse)
def get_location(
    order_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get current rider location for an order.

    REST alternative to the WebSocket, answering to the same authorisation
    rule: the customer who placed the order, the rider carrying it, or an admin.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # This used to reject only mismatched *customers*, so any baker and any
    # rider could read any order's live position and the customer's drop-off
    # coordinates just by walking the order id.
    if not can_observe_delivery(user, order):
        raise HTTPException(status_code=403, detail="You can only track your own orders")

    location = get_rider_location(order_id)
    if not location:
        return LocationResponse(
            order_id=order_id,
            status="not_started",
        )

    return LocationResponse(**location)


@router.post("/{order_id}/stop-tracking")
def stop_tracking(
    order_id: int,
    admin: User = Depends(require_admin),
):
    """Stop tracking for a delivery. Called when order is DELIVERED."""
    stop_delivery_tracking(order_id)
    return {"message": "Tracking stopped", "order_id": order_id}
