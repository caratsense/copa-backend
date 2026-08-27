"""
WhatsApp Notification Dispatcher
==================================
Called after every order status change. This is the SINGLE owner of outbound
business-initiated WhatsApp messages — the event worker deliberately no longer
sends any (two pipelines used to double-message customers).

Every recipient is checked against their recorded opt-in first; see
app/services/wa_consent.py. Every send goes through the outbox
(app/services/wa_outbox.py) so a Meta failure leaves a retryable record rather
than a log line nobody reads.

Template names are not written here — they resolve through
app/services/wa_templates.py so the Meta-side names stay configurable.
"""

import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services import wa_outbox
from app.services.order_service import _enrich_order
from app.services.wa_consent import may_message
from app.services.store_hours import to_ist

settings = get_settings()
logger = logging.getLogger(__name__)


def _consenting_admins(db: Session) -> list[User]:
    """Admins we're allowed to message."""
    return [
        a for a in db.query(User).filter(User.role == UserRole.ADMIN).all()
        if may_message(a)
    ]


def _items_str(order: Order) -> str:
    """Build a human-readable items string."""
    parts = []
    for item in (order.items or []):
        c = item.customization or {}
        parts.append(
            f"{c.get('size', '1kg')} {c.get('flavor', '')} "
            f"{item.product.name if item.product else 'Cake'} x{item.quantity}"
        )
    return ", ".join(parts) or "Cake"


def _delivery_str(order: Order) -> str:
    """
    Delivery time, rendered in IST.

    delivery_time is a timezone-aware column and Postgres hands it back in UTC,
    so formatting it directly printed the wrong clock time: a 4:00 PM IST slot
    came out as "10:30 AM" (16:00 minus the 5:30 offset) in the message the
    customer actually reads.
    """
    if not order.delivery_time:
        return "ASAP"
    return to_ist(order.delivery_time).strftime("%d %b, %I:%M %p")


def _maps_link(address: str) -> str:
    """Google Maps link. URL-encoded — a raw space swap breaks on & and #."""
    if not address or address == "Self Pickup":
        return ""
    from urllib.parse import quote_plus
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def _tracking_link(order: Order) -> str:
    base = settings.WHATSAPP_TRACKING_BASE_URL or ""
    return f"{base}?id={order.id}" if base else ""


def _money(value) -> str:
    """
    Template amounts as rupees-and-paise.

    These used to be int()-truncated, so a customer whose order came to 1499.50
    was quoted 1499 in the very message that confirms what they will pay.
    """
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _send(db, order, template_key, recipient, params, event_type, role, allowed):
    """One notification: skipped-with-reason, or attempted and recorded."""
    # Order matters: `allowed` already folds in "has a phone number", so
    # checking it first would report a missing number as a consent problem and
    # send whoever reads the outbox looking in the wrong place.
    reason = None
    if not settings.WHATSAPP_ENABLED:
        reason = "WHATSAPP_ENABLED is false"
    elif not recipient:
        reason = "no phone number on record"
    elif not allowed:
        reason = "no opt-in on record"

    wa_outbox.queue_and_send(
        db,
        template_key=template_key,
        recipient=recipient or "",
        params=params,
        order_id=order.id if order else None,
        recipient_role=role,
        event_type=event_type,
        skip_reason=reason,
    )


def notify_baker_assigned(db: Session, order: Order) -> None:
    """
    Tell a baker an order is theirs.

    Split out so a mid-flight reassignment can notify the new baker without a
    status change (see assignment_engine.apply_baker_assignment_status).
    """
    _enrich_order(order)
    baker = order.baker
    _send(
        db, order, "baker_new_order",
        baker.phone if baker else "",
        [str(order.id), _items_str(order), order.notes or "None", _delivery_str(order)],
        OrderStatus.ASSIGNED.value, "baker",
        may_message(baker) and bool(baker and baker.phone),
    )


def notify_rider_assigned(db: Session, order: Order) -> None:
    """
    Tell a rider a delivery is theirs.

    Also split out: a rider assigned to an already-PACKAGED order used to be
    told nothing, because the only trigger was the PACKAGED transition itself.
    """
    _enrich_order(order)
    rider = order.rider
    address = order.delivery_address or "Self Pickup"
    _send(
        db, order, "rider_new_delivery",
        rider.phone if rider else "",
        [
            str(order.id), order.customer_name or "Customer",
            order.user.phone if order.user else "",
            address, _maps_link(address), _money(order.total_price),
        ],
        OrderStatus.PACKAGED.value, "rider",
        may_message(rider) and bool(rider and rider.phone),
    )


def dispatch_notifications(db: Session, order: Order, new_status: str, rework: bool = False):
    """
    Send WhatsApp notifications for a status change.

    `rework` distinguishes the two ways an order reaches IN_PRODUCTION: a baker
    starting work, and an admin rejecting it at quality check. Both used to send
    the rework template, so a baker was told to redo a cake the instant they
    started it.
    """
    _enrich_order(order)
    items = _items_str(order)
    delivery = _delivery_str(order)

    customer = order.user
    baker = order.baker
    rider = order.rider
    customer_ok = may_message(customer) and bool(customer and customer.phone)
    baker_ok = may_message(baker) and bool(baker and baker.phone)
    rider_ok = may_message(rider) and bool(rider and rider.phone)
    customer_phone = customer.phone if customer else ""

    try:
        # ─── CONFIRMED → customer + admin ─────────────
        if new_status == OrderStatus.CONFIRMED.value:
            _send(db, order, "order_confirmation", customer_phone,
                  [order.customer_name or "Customer", str(order.id), items,
                   _money(order.total_price), delivery],
                  new_status, "customer", customer_ok)
            for admin in _consenting_admins(db):
                _send(db, order, "admin_new_order", admin.phone,
                      [str(order.id), f"{order.customer_name or 'Customer'} ({customer_phone})",
                       items, _money(order.total_price), delivery],
                      new_status, "admin", True)

        # ─── ASSIGNED → baker ────────────────────────
        elif new_status == OrderStatus.ASSIGNED.value:
            notify_baker_assigned(db, order)

        # ─── IN_PRODUCTION → baker, only on rework ───
        elif new_status == OrderStatus.IN_PRODUCTION.value:
            if rework:
                _send(db, order, "order_rework",
                      baker.phone if baker else "", [str(order.id)],
                      new_status, "baker", baker_ok)
            # A baker starting their own order needs no message; they just acted.

        # ─── AWAITING_APPROVAL → admin (quality check) ─
        elif new_status == OrderStatus.AWAITING_APPROVAL.value:
            for admin in _consenting_admins(db):
                _send(db, order, "admin_approval_needed", admin.phone,
                      [str(order.id), items, _money(order.total_price)],
                      new_status, "admin", True)

        # ─── PACKAGED → rider, if one is already assigned ─
        elif new_status == OrderStatus.PACKAGED.value:
            if order.assigned_rider_id:
                notify_rider_assigned(db, order)

        # ─── OUT_FOR_DELIVERY → customer ─────────────
        # The proposal promised this one and the dispatcher never had a branch
        # for it, so customers were never told their cake had left the bakery.
        elif new_status == OrderStatus.OUT_FOR_DELIVERY.value:
            _send(db, order, "order_out_for_delivery", customer_phone,
                  [order.customer_name or "Customer", str(order.id),
                   order.rider_name or "Your rider", _tracking_link(order)],
                  new_status, "customer", customer_ok)

        # ─── DELIVERED → customer + admin ────────────
        elif new_status == OrderStatus.DELIVERED.value:
            _send(db, order, "order_delivered", customer_phone,
                  [order.customer_name or "Customer", str(order.id)],
                  new_status, "customer", customer_ok)
            for admin in _consenting_admins(db):
                _send(db, order, "order_delivered_admin", admin.phone,
                      [str(order.id), order.customer_name or "Customer"],
                      new_status, "admin", True)

        # ─── CANCELLED → customer + assigned staff ───
        elif new_status == OrderStatus.CANCELLED.value:
            _send(db, order, "order_cancelled", customer_phone,
                  [order.customer_name or "Customer", str(order.id)],
                  new_status, "customer", customer_ok)
            if order.assigned_baker_id:
                _send(db, order, "order_cancelled_staff",
                      baker.phone if baker else "", [str(order.id)],
                      new_status, "baker", baker_ok)
            if order.assigned_rider_id:
                _send(db, order, "order_cancelled_staff",
                      rider.phone if rider else "", [str(order.id)],
                      new_status, "rider", rider_ok)

    except Exception as e:
        # The order transition is already committed; notification trouble must
        # never surface to whoever moved the order.
        logger.error(f"[WA DISPATCH] Error for order #{order.id}: {e}")
