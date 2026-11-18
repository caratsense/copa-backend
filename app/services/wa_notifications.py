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


def customer_status_phrase(order: Order) -> str:
    """
    What to tell a customer their order is doing, in their words.

    Two things this fixes, both of which the customer read on WhatsApp.

    ASSIGNED said "Assigned to baker". There is no baker — the shop makes
    these itself — so it was telling the customer about a person who does not
    exist. "With our kitchen" is true either way, which is the point: the
    customer does not care who is holding it, only that somebody is.

    And the map covered eight statuses out of thirteen. The five missing ones
    included every collection state, so a customer who asked where their
    pickup order was got the raw enum back: "Status: READY_FOR_PICKUP".
    PACKAGED said "Ready for delivery" to someone who was coming to fetch it.
    """
    status = order.status.value if hasattr(order.status, "value") else str(order.status)
    pickup = bool(getattr(order, "is_pickup", False))
    return {
        "RECEIVED": "Received — being confirmed",
        "CONFIRMED": "Confirmed",
        "ASSIGNED": "With our kitchen",
        "IN_PRODUCTION": "Being made",
        "QC": "Being checked",
        "AWAITING_APPROVAL": "Being checked",
        "PACKAGED": "Packed and ready to collect" if pickup else "Packed, about to go out",
        "READY_FOR_PICKUP": "Ready to collect",
        "COLLECTED": "Collected — thank you!",
        "NOT_COLLECTED": "Waiting for you at the shop",
        "OUT_FOR_DELIVERY": "On its way to you",
        "DELIVERED": "Delivered",
        "DELIVERY_FAILED": "We could not deliver it — we will be in touch",
        "CANCELLED": "Cancelled",
    }.get(status, status.replace("_", " ").capitalize())


def items_summary(order: Order) -> str:
    """
    Build a human-readable items string.

    Assembled from the parts that are actually present. Size used to default to
    "1kg", which printed a weight onto a fixed-price item - a brownie, a pack of
    six buns - that is not sold by weight, so the baker and the customer both
    read a size nobody chose. A blank flavour left a double space for the same
    reason.
    """
    parts = []
    for item in (order.items or []):
        c = item.customization or {}
        name = item.product.name if item.product else "Cake"
        bits = [b for b in (c.get("size"), c.get("flavor"), name) if b]
        parts.append(f"{' '.join(bits)} x{item.quantity}")
    return ", ".join(parts) or "Cake"


# The old name, kept because it is what the tests and the rest of this module
# call. The public one exists so the WhatsApp bot can use the same assembly
# rather than its own, which read size and flavour only and printed a bare
# space for anything the customer did not choose a flavour for.
_items_str = items_summary


def scheduled_for(order: Order) -> str:
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


_delivery_str = scheduled_for


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


def _send(db, order, template_key, recipient, params, event_type, role, allowed,
          skip_reason: str | None = None):
    """
    One notification: skipped-with-reason, or attempted and recorded.

    `skip_reason` is for when the caller knows something this function cannot:
    that there is nobody in the role at all. "No phone number on record" reads
    like a broken staff account, and a shop with no baker would have collected
    one of those against every order it made itself.
    """
    # Order matters: `allowed` already folds in "has a phone number", so
    # checking it first would report a missing number as a consent problem and
    # send whoever reads the outbox looking in the wrong place.
    reason = skip_reason
    if reason:
        pass
    elif not settings.WHATSAPP_ENABLED:
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
        # Nothing has gone wrong when there is nobody to tell: the shop is
        # making this one itself, which is its normal way of working until it
        # hires. Said plainly so the outbox is not a column of red.
        skip_reason=None if order.assigned_baker_id
        else "no baker on this order — the shop is making it in-house",
    )


def notify_customer_picture(db: Session, order: Order, count: int) -> None:
    """
    Tell the shop a customer has sent a picture of the cake they want.

    Customers can attach pictures to their own order now. Before that they
    could not, and their reference reached the kitchen as a filename written
    into the text piped onto the cake, or not at all.

    What it did not come with was anybody being told. The picture lands in the
    order's brief and is seen only if somebody opens that order — which, for a
    shop that reads WhatsApp rather than a dashboard, means often not until
    after the cake is made.

    Admin only. The customer knows what they sent.
    """
    _enrich_order(order)
    for admin in _consenting_admins(db):
        _send(
            db, order, "admin_customer_sent_picture", admin.phone,
            [str(order.id), order.customer_name or "Customer", str(count)],
            "customer_picture", "admin", True,
        )


def notify_admin_proof_ready(db: Session, order: Order) -> None:
    """
    Tell the shop a finished cake is waiting to be looked at.

    Sent when the photo is uploaded rather than when the order reaches
    AWAITING_APPROVAL, because the photo is the thing being checked and it is
    now what gates the approval. The two usually happen seconds apart; when
    they do not, this is the one that matters.
    """
    _enrich_order(order)
    # proof_image is a /media path served by the API, not by the website, so
    # it is the backend's origin that makes it openable from a phone.
    base = (settings.BACKEND_BASE_URL or "").rstrip("/")
    photo = f"{base}{order.proof_image}" if (order.proof_image and base) else (order.proof_image or "")
    for admin in _consenting_admins(db):
        _send(
            db, order, "admin_proof_ready", admin.phone,
            [str(order.id), _items_str(order), photo],
            "BAKE_PROOF_UPLOADED", "admin",
            bool(admin.phone),
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


def dispatch_notifications(db: Session, order: Order, new_status: str,
                           rework: bool = False, previous: str | None = None):
    """
    Send WhatsApp notifications for a status change.

    `rework` distinguishes the two ways an order reaches IN_PRODUCTION: a baker
    starting work, and an admin rejecting it at quality check. Both used to send
    the rework template, so a baker was told to redo a cake the instant they
    started it.

    `previous` is the status it came from, and is what that distinction is
    really made of. The flag has to be remembered by every caller — the reject
    route sets it, the WhatsApp staff reply sets it, and the plain status
    endpoint takes it from the request body, where it defaults to false. One
    caller forgetting used to be harmless; now it would tell a customer their
    cake is being made at the moment it was sent back to be remade. A cake
    arriving here from AWAITING_APPROVAL is a redo whatever anybody passed.
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

        # ─── IN_PRODUCTION → customer, and the baker only on rework ───
        elif new_status == OrderStatus.IN_PRODUCTION.value:
            redo = rework or previous == OrderStatus.AWAITING_APPROVAL.value
            if redo:
                _send(db, order, "order_rework",
                      baker.phone if baker else "", [str(order.id)],
                      new_status, "baker", baker_ok,
                      skip_reason=None if order.assigned_baker_id
                      else "no baker on this order — sent back to the shop itself")
            else:
                # The customer heard nothing between "confirmed" and "ready",
                # which on a cake ordered a day or more ahead is a long silence
                # about something they are anxious about. This is the middle.
                #
                # Not on a rework: that is the same cake being made again after
                # it failed its check, and "being made now" a second time reads
                # as either a mistake or a confession.
                _send(db, order, "order_in_production", customer_phone,
                      [order.customer_name or "Customer", str(order.id)],
                      new_status, "customer", customer_ok)
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

        # ─── READY_FOR_PICKUP → customer ─────────────
        # The collection equivalent of "out for delivery", and the message that
        # was missing entirely: a customer who chose to collect was told their
        # cake was on its way to them, by a rider, to an address they had never
        # given. This one tells them where to come and until when.
        elif new_status == OrderStatus.READY_FOR_PICKUP.value:
            from app.services.store_hours import pickup_details
            details = pickup_details(db)
            _send(db, order, "order_ready_for_pickup", customer_phone,
                  [order.customer_name or "Customer", str(order.id),
                   details["line"], _delivery_str(order)],
                  new_status, "customer", customer_ok)

        # ─── COLLECTED → customer ────────────────────
        elif new_status == OrderStatus.COLLECTED.value:
            _send(db, order, "order_collected", customer_phone,
                  [order.customer_name or "Customer", str(order.id)],
                  new_status, "customer", customer_ok)

        # ─── NOT_COLLECTED → customer + admin ────────
        # The slot passed and the cake is still here. Nobody was told anything
        # before, because the state did not exist: a pickup that nobody came
        # for simply sat in the kitchen looking like an open delivery.
        elif new_status == OrderStatus.NOT_COLLECTED.value:
            from app.services.store_hours import pickup_details
            details = pickup_details(db)
            _send(db, order, "order_not_collected", customer_phone,
                  [order.customer_name or "Customer", str(order.id), details["line"]],
                  new_status, "customer", customer_ok)
            for admin in _consenting_admins(db):
                _send(db, order, "order_not_collected_admin", admin.phone,
                      [str(order.id), order.customer_name or "Customer",
                       order.user.phone if order.user else ""],
                      new_status, "admin", True)

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
