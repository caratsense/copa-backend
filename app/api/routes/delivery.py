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
from app.core.auth import get_current_user, require_admin, require_role
from app.models.user import UserRole
from app.services.delivery_tracking import (
    start_delivery_tracking,
    get_rider_location,
    stop_delivery_tracking,
)

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


# ─── ROUTES ───────────────────────────────────────────

@router.post("/{order_id}/start-tracking", response_model=dict)
def start_tracking(
    order_id: int,
    data: StartTrackingRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Start GPS tracking for a delivery.
    Call this when the order status changes to OUT_FOR_DELIVERY.
    Requires pickup and dropoff coordinates.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != OrderStatus.OUT_FOR_DELIVERY:
        raise HTTPException(
            status_code=400,
            detail=f"Order must be OUT_FOR_DELIVERY to start tracking. Current: {order.status.value}"
        )

    rider_id = 0
    if order.assigned_rider:
        # Try to extract rider ID from the assigned_rider string
        # In a full system, this would be a foreign key
        rider_id = hash(order.assigned_rider) % 10000

    result = start_delivery_tracking(
        order_id=order_id,
        rider_id=rider_id,
        pickup_lat=data.pickup_lat,
        pickup_lng=data.pickup_lng,
        dropoff_lat=data.dropoff_lat,
        dropoff_lng=data.dropoff_lng,
    )

    return {
        "message": "Tracking started",
        "order_id": order_id,
        "websocket_rider": f"ws://localhost:8000/ws/rider/{order_id}?token=<rider_jwt>",
        "websocket_customer": f"ws://localhost:8000/ws/track/{order_id}?token=<customer_jwt>",
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
    Customers can only check their own orders.
    Works as a REST alternative to the WebSocket.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Customers can only track their own orders
    if user.role == UserRole.CUSTOMER and order.user_id != user.id:
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
