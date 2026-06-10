"""
Order Service — create orders, update statuses, payments, coupons.

Enhanced with:
- Coupon/discount application
- Payment status management
- WebSocket broadcast on status changes
- Customer order history & tracking
"""

import asyncio
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.product import Product
from app.models.order import Order, OrderStatus, PaymentStatus, VALID_TRANSITIONS
from app.models.order_item import OrderItem
from app.models.delivery import DeliveryZone
from app.models.coupon import Coupon
from app.schemas import OrderCreate, StatusUpdate, PaymentUpdate
from app.services.pricing_engine import calculate_item_price
from app.services.event_service import emit_event


def _apply_coupon(db: Session, coupon_code: str, subtotal: float) -> tuple[float, str | None]:
    """Validate and apply a coupon. Returns (discount_amount, error_message)."""
    coupon = db.query(Coupon).filter(
        Coupon.code == coupon_code.upper().strip(),
        Coupon.is_active == True,
    ).first()

    if not coupon:
        return 0.0, "Invalid coupon code"

    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        return 0.0, "Coupon has expired"

    if coupon.max_uses and coupon.used_count >= coupon.max_uses:
        return 0.0, "Coupon usage limit reached"

    if subtotal < coupon.min_order_value:
        return 0.0, f"Minimum order value is ₹{coupon.min_order_value}"

    discount = coupon.calculate_discount(subtotal)
    coupon.used_count += 1
    return discount, None


def _try_broadcast(order_id: int, event_type: str, data: dict):
    """Best-effort WebSocket broadcast — doesn't fail the request."""
    try:
        from app.api.routes.websocket import manager
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(manager.broadcast({
                "type": event_type,
                "order_id": order_id,
                **data,
            }))
    except Exception:
        pass


def create_order(db: Session, data: OrderCreate) -> Order:
    """Full order creation with pricing, coupons, store hours scheduling, and events."""

    from app.services.store_hours import schedule_order_delivery

    # ── Resolve delivery zone ──
    delivery_zone_id = None
    if data.delivery_zone:
        zone = db.query(DeliveryZone).filter(
            DeliveryZone.area_name == data.delivery_zone,
            DeliveryZone.is_active == True,
        ).first()
        if zone:
            delivery_zone_id = zone.id

    # ── Schedule delivery based on store hours ──
    schedule = schedule_order_delivery(db, data.delivery_time)

    # ── Create order shell — always auto-confirmed ──
    order = Order(
        user_id=data.user_id,
        status=OrderStatus.CONFIRMED,    # auto-confirmed always
        total_price=0.0,
        subtotal=0.0,
        discount=0.0,
        delivery_address=data.delivery_address,
        delivery_time=schedule["delivery_time"],
        delivery_zone_id=delivery_zone_id,
        notes=data.notes,
    )
    db.add(order)
    db.flush()

    # ── Price each item ──
    subtotal = 0.0
    for item_data in data.items:
        product = db.query(Product).filter(Product.id == item_data.product_id).first()
        if not product:
            raise HTTPException(status_code=404, detail=f"Product {item_data.product_id} not found")
        if not product.is_available:
            raise HTTPException(status_code=400, detail=f"Product '{product.name}' is currently unavailable")

        breakdown = calculate_item_price(
            db=db,
            product=product,
            customization=item_data.customization,
            quantity=item_data.quantity,
            delivery_zone_name=data.delivery_zone,
        )

        order_item = OrderItem(
            order_id=order.id,
            product_id=item_data.product_id,
            quantity=item_data.quantity,
            customization=item_data.customization.model_dump(),
            price=breakdown.line_total,
            price_breakdown=breakdown.model_dump(),
        )
        db.add(order_item)
        subtotal += breakdown.line_total

    order.subtotal = round(subtotal, 2)

    # ── Apply coupon ──
    discount = 0.0
    if data.coupon_code:
        discount, error = _apply_coupon(db, data.coupon_code, subtotal)
        if error:
            raise HTTPException(status_code=400, detail=f"Coupon error: {error}")
        order.coupon_code = data.coupon_code.upper().strip()

    order.discount = round(discount, 2)
    order.total_price = round(subtotal - discount, 2)

    # ── Emit event ──
    emit_event(db, order.id, "ORDER_CREATED", {
        "user_id": data.user_id,
        "subtotal": order.subtotal,
        "discount": order.discount,
        "total_price": order.total_price,
        "item_count": len(data.items),
        "is_off_hours": schedule.get("is_off_hours", False),
        "is_scheduled": schedule.get("is_scheduled", False),
        "schedule_message": schedule.get("message", ""),
    })

    emit_event(db, order.id, "STATUS_CHANGED", {
        "from": "NEW",
        "to": "CONFIRMED",
    })

    db.commit()
    db.refresh(order)

    # ── Auto-assign baker if store is currently open ──
    # Off-hours orders stay in CONFIRMED queue — the scheduler assigns them at opening
    if not schedule.get("is_off_hours", False):
        try:
            from app.services.assignment_engine import auto_assign_baker
            order = auto_assign_baker(db, order.id)
        except Exception:
            pass  # no bakers available — stays in CONFIRMED queue

    _try_broadcast(order.id, "ORDER_CREATED", {
        "status": order.status.value,
        "total": order.total_price,
        "is_off_hours": schedule.get("is_off_hours", False),
        "message": schedule.get("message", ""),
    })
    return order


def update_order_status(db: Session, order_id: int, data: StatusUpdate) -> Order:
    """Update order status with lifecycle validation + auto-assignment + WebSocket broadcast."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        new_status = OrderStatus(data.status)
    except ValueError:
        valid = [s.value for s in OrderStatus]
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid}")

    current = order.status
    allowed = VALID_TRANSITIONS.get(current, [])
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from {current.value} to {new_status.value}. "
                   f"Allowed: {[s.value for s in allowed]}"
        )

    old_status = current.value
    order.status = new_status

    emit_event(db, order.id, "STATUS_CHANGED", {
        "from": old_status,
        "to": new_status.value,
    })

    db.commit()
    db.refresh(order)

    # ── Auto-assign baker when CONFIRMED ──
    if new_status == OrderStatus.CONFIRMED and not order.assigned_baker_id:
        try:
            from app.services.assignment_engine import auto_assign_baker
            order = auto_assign_baker(db, order.id)
        except HTTPException:
            pass  # no bakers available — admin can assign manually later

    # ── Auto-assign rider when PACKAGED ──
    if new_status == OrderStatus.PACKAGED and not order.assigned_rider_id:
        try:
            from app.services.assignment_engine import auto_assign_rider
            order = auto_assign_rider(db, order.id)
        except HTTPException:
            pass  # no riders available — admin can assign manually later

    _try_broadcast(order.id, "STATUS_CHANGED", {"from": old_status, "to": new_status.value})

    # ── WhatsApp notifications ──
    try:
        from app.services.wa_notifications import dispatch_notifications
        dispatch_notifications(db, order, new_status.value)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[WA] Notification dispatch failed: {e}")

    return order


def update_payment_status(db: Session, order_id: int, data: PaymentUpdate) -> Order:
    """Update payment status (admin action)."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        new_payment = PaymentStatus(data.payment_status)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payment status. Use: PENDING, PAID, REFUNDED")

    old_payment = order.payment_status.value
    order.payment_status = new_payment

    emit_event(db, order.id, "PAYMENT_UPDATED", {
        "from": old_payment,
        "to": new_payment.value,
    })

    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int) -> Order:
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    _enrich_order(order)
    return order


def list_orders(db: Session, skip: int = 0, limit: int = 50) -> list[Order]:
    orders = db.query(Order).order_by(Order.created_at.desc()).offset(skip).limit(limit).all()
    for o in orders:
        _enrich_order(o)
    return orders


def get_user_orders(db: Session, user_id: int, skip: int = 0, limit: int = 20) -> list[Order]:
    """Get order history for a specific customer."""
    orders = (
        db.query(Order)
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .offset(skip).limit(limit)
        .all()
    )
    for o in orders:
        _enrich_order(o)
    return orders


def _enrich_order(order: Order):
    """Add customer_name, customer_phone, baker_name, rider_name as dynamic attributes."""
    if order.user:
        order.customer_name = order.user.name
        order.customer_phone = order.user.phone
    else:
        order.customer_name = None
        order.customer_phone = None
    order.baker_name = order.baker.name if order.baker else None
    order.rider_name = order.rider.name if order.rider else None
