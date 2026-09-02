"""
Order Service — create orders, update statuses, payments, coupons.

Enhanced with:
- Coupon/discount application
- Payment status management
- WebSocket broadcast on status changes
- Customer order history & tracking
"""

from datetime import datetime, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.core.broadcast import broadcast_sync
from app.models.product import Product
from app.models.order import Order, OrderStatus, PaymentStatus, VALID_TRANSITIONS
from app.models.order_item import OrderItem
from app.models.delivery import DeliveryZone
from app.models.coupon import Coupon
from app.models.extra import Extra
from app.models.pricing import AddonRule
from app.schemas import OrderCreate, StatusUpdate, PaymentUpdate
from app.services.pricing_engine import calculate_item_price, lookup_delivery_charge
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
    broadcast_sync({
        "type": event_type,
        "order_id": order_id,
        **data,
    })


def _lock_order(db: Session, order_id: int) -> Order | None:
    """SELECT ... FOR UPDATE where the database supports it."""
    q = db.query(Order).filter(Order.id == order_id)
    try:
        return q.with_for_update().first()
    except Exception:
        return q.first()


def is_payable(order: Order) -> bool:
    """
    Whether this order may consume ingredients and a baker's time.

    Cash on delivery has been withdrawn, so every new order must be paid for
    before it consumes ingredients. Orders placed while COD still existed are
    grandfathered - refusing them here would strand in-flight work that the
    bakery has already committed to.
    """
    method = (order.payment_method or "ONLINE").upper()
    if method == "COD":
        return True        # legacy rows only; COD orders can no longer be created
    return order.payment_status == PaymentStatus.PAID


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

        # Decrement stock for finite addons
        for addon_name in item_data.customization.addons:
            addon = db.query(AddonRule).filter(AddonRule.name == addon_name).first()
            if addon and addon.stock is not None:
                addon.stock = max(0, addon.stock - item_data.quantity)

    items_subtotal = round(subtotal, 2)

    # ── Price extras (balloons, candles, gift wrap…) ──
    # Priced from the DB by id; the client never sends amounts.
    extras_total = 0.0
    extras_snapshot: list[dict] = []
    if data.extras:
        rows = db.query(Extra).filter(
            Extra.id.in_(set(data.extras)),
            Extra.is_active == True,
        ).all()
        found = {e.id for e in rows}
        missing = set(data.extras) - found
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"These extras are unavailable: {sorted(missing)}",
            )
        for e in rows:
            price = float(e.price or 0.0)
            extras_total += price
            extras_snapshot.append({"id": e.id, "name": e.name, "price": price})

    # ── Delivery charge — once per order, not per line ──
    delivery_charge = lookup_delivery_charge(db, data.delivery_zone)

    order.extras = extras_snapshot
    order.extras_total = round(extras_total, 2)
    order.delivery_charge = round(delivery_charge, 2)
    order.subtotal = round(items_subtotal + extras_total + delivery_charge, 2)

    # ── Apply coupon ──
    # Discount is computed against the cake subtotal only — coupons don't
    # discount delivery or party extras.
    discount = 0.0
    if data.coupon_code:
        discount, error = _apply_coupon(db, data.coupon_code, items_subtotal)
        if error:
            raise HTTPException(status_code=400, detail=f"Coupon error: {error}")
        order.coupon_code = data.coupon_code.upper().strip()

    order.discount = round(discount, 2)
    order.total_price = round(order.subtotal - discount, 2)

    # ── Emit event ──
    emit_event(db, order.id, "ORDER_CREATED", {
        "user_id": data.user_id,
        "items_subtotal": items_subtotal,
        "extras_total": order.extras_total,
        "delivery_charge": order.delivery_charge,
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

    # create_order writes CONFIRMED directly rather than transitioning into it,
    # so dispatch_notifications never ran for a new order: no customer
    # confirmation and no admin new-order alert for anything placed on the web.
    #
    # Only for an order that is actually going ahead. An ONLINE order at this
    # point has not been paid — the customer has not even reached PayU yet —
    # and announcing "Order received" to them and to the admin for a checkout
    # that is then abandoned is worse than saying nothing: the customer thinks
    # they have bought a cake and the bakery thinks it has sold one. The
    # confirmation is sent from the payment callback instead, once the money is
    # actually in (see payments._release_for_production).
    #
    # Sent BEFORE auto-assignment on purpose. Assignment now transitions the
    # order to ASSIGNED through the service, which fires its own notification;
    # dispatching afterwards would read the new status and send the baker a
    # second copy.
    if is_payable(order):
        try:
            from app.services.wa_notifications import dispatch_notifications
            dispatch_notifications(db, order, OrderStatus.CONFIRMED.value)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"[WA] Confirmation dispatch failed for order {order.id}: {e}")

    # ── Auto-assign baker if store is open AND the order is payable ──
    # Off-hours orders stay in CONFIRMED queue — the scheduler assigns them at
    # opening. Unpaid ONLINE orders also stay put: assignment happens when the
    # payment settles (see payments._release_for_production), so an abandoned
    # checkout is never baked.
    if not schedule.get("is_off_hours", False) and is_payable(order):
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


def update_order_status(db: Session, order_id: int, data: StatusUpdate, rework: bool = False) -> Order:
    """
    Update order status with lifecycle validation + auto-assignment + WebSocket broadcast.

    `rework=True` marks an IN_PRODUCTION transition as a quality-check
    rejection rather than a baker starting work. Both reach the same status,
    but only one of them should tell the baker to redo the cake.
    """
    # Lock the row for the duration of the transition. Without this two admins
    # (or a dashboard click racing a WhatsApp reply) both read the same current
    # status, both pass the transition check, and both commit — advancing the
    # order twice and sending duplicate notifications. Postgres honours this;
    # SQLite ignores it harmlessly.
    order = _lock_order(db, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        new_status = OrderStatus(data.status)
    except ValueError:
        valid = [s.value for s in OrderStatus]
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {valid}")

    current = order.status
    # An unpaid ONLINE order must not enter production by any route — admin
    # button, WhatsApp command or auto-assignment.
    if new_status in (OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION) and not is_payable(order):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Order #{order.id} is not paid ({order.payment_status.value}). "
                "It cannot enter production until payment is received."
            ),
        )

    allowed = VALID_TRANSITIONS.get(current, [])
    if new_status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from {current.value} to {new_status.value}. "
                   f"Allowed: {[s.value for s in allowed]}"
        )

    # Nothing can go out for delivery without someone carrying it. This lived
    # only in /retry-delivery, so the fleet board enforced it and a plain status
    # PATCH from the admin orders table did not -- and _begin_delivery_tracking
    # falls back to `assigned_rider_id or 0`, so the bypass produced a tracked
    # delivery belonging to a rider 0 who does not exist.
    if new_status == OrderStatus.OUT_FOR_DELIVERY and not order.assigned_rider_id:
        raise HTTPException(
            status_code=400,
            detail="Assign a rider before sending this order out.",
        )

    # A failed delivery is only useful if it says why. The dedicated endpoint
    # requires it, but a plain status PATCH reaches the same transition, so the
    # rule belongs here where every caller passes rather than in one route.
    if new_status == OrderStatus.DELIVERY_FAILED and not (
        getattr(data, "reason", None) or ""
    ).strip():
        raise HTTPException(
            status_code=422,
            detail="Say why the delivery could not be completed.",
        )

    old_status = current.value
    order.status = new_status

    # Give the add-ons back. create_order reserves finite stock by decrementing
    # it, and nothing ever put it back: every cancelled order permanently ate
    # its toppers. When stock reaches 0 the public list route filters the addon
    # out of the customer builder entirely, so it silently disappears from the
    # menu with no warning to anyone.
    if new_status == OrderStatus.CANCELLED:
        _restore_addon_stock(db, order)

    event_payload = {"from": old_status, "to": new_status.value}
    reason = (getattr(data, "reason", None) or "").strip()
    if reason:
        event_payload["reason"] = reason[:300]
    emit_event(db, order.id, "STATUS_CHANGED", event_payload)

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

    # ── Delivery tracking lifecycle ──
    # Owned by the backend rather than by whichever client happened to change the
    # status. It previously depended on a `POST /delivery/{id}/start-tracking`
    # call that nothing ever made, which is why ETA was always null: the metadata
    # holding the destination was never written.
    if new_status == OrderStatus.OUT_FOR_DELIVERY:
        _begin_delivery_tracking(db, order)
    elif new_status == OrderStatus.DELIVERED:
        _end_delivery_tracking(order)
    elif new_status == OrderStatus.DELIVERY_FAILED:
        # The rider is no longer carrying it, so it should not sit on the fleet
        # map showing a live position. Sending it out again starts tracking
        # afresh via the OUT_FOR_DELIVERY branch above.
        _end_delivery_tracking(order)
    elif new_status == OrderStatus.PACKAGED:
        # The order joins the fleet view as "assigned, not collected". It has no
        # position by definition, so there is nothing to stream — the dashboard
        # just needs to know its list is out of date.
        from app.core.broadcast import fleet_broadcast_sync
        fleet_broadcast_sync({"type": "fleet_changed", "order_id": order.id})

    # ── WhatsApp notifications ──
    try:
        from app.services.wa_notifications import dispatch_notifications
        dispatch_notifications(db, order, new_status.value, rework=rework)
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


def _begin_delivery_tracking(db: Session, order: Order):
    """
    Stamp the delivery's origin/destination in Redis and put it on the admin
    fleet stream. Best effort — the status change is already committed, and a
    Redis outage must not roll it back or 500 the caller.
    """
    from app.core.broadcast import delivery_started_sync
    from app.services.delivery_tracking import start_delivery_tracking
    from app.services.fleet_tracking import resolve_order_coordinates

    try:
        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng = resolve_order_coordinates(db, order)
        start_delivery_tracking(
            order_id=order.id,
            rider_id=order.assigned_rider_id or 0,
            pickup_lat=pickup_lat,
            pickup_lng=pickup_lng,
            dropoff_lat=dropoff_lat,
            dropoff_lng=dropoff_lng,
        )
        delivery_started_sync(order.id, {
            "order_id": order.id,
            "rider_id": order.assigned_rider_id,
            "rider_name": order.rider.name if order.rider else None,
            "customer_name": order.user.name if order.user else None,
            "delivery_address": order.delivery_address,
            "order_status": order.status.value,
            # No GPS has arrived yet — the dashboard shows the delivery without
            # a map position rather than inventing one.
            "tracking_state": "awaiting_gps",
            "dropoff_lat": dropoff_lat,
            "dropoff_lng": dropoff_lng,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[Tracking] start failed for order {order.id}: {e}")


def _restore_addon_stock(db: Session, order: Order) -> None:
    """
    Return the finite add-on units an order was holding.

    Only touches rules with a finite stock (NULL means unlimited), and never
    raises: releasing inventory must not be able to block a cancellation.
    """
    from app.models.pricing import AddonRule

    try:
        for item in order.items or []:
            names = (item.customization or {}).get("addons") or []
            for name in names:
                addon = db.query(AddonRule).filter(AddonRule.name == name).first()
                if addon is not None and addon.stock is not None:
                    addon.stock = addon.stock + (item.quantity or 1)
    except Exception as e:                       # noqa: BLE001 — best effort
        import logging
        logging.getLogger(__name__).error(
            "[STOCK] Could not restore add-ons for cancelled order %s: %s", order.id, e
        )


def _end_delivery_tracking(order: Order):
    """
    Clean up Redis and wind down every socket for a completed delivery.

    Lives here rather than in the rider route so the admin's
    `PATCH /orders/{id}/status` path cleans up too — it previously did not, and
    left tracking keys alive for the full 24h TTL.
    """
    from app.core.broadcast import delivery_completed_sync
    from app.services.delivery_tracking import stop_delivery_tracking

    try:
        stop_delivery_tracking(order.id)
        delivery_completed_sync(order.id, {
            "order_id": order.id,
            "rider_id": order.assigned_rider_id,
        })
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[Tracking] stop failed for order {order.id}: {e}")


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
