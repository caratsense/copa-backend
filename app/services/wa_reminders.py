"""
The messages that fire because time passed, rather than because somebody acted.

Everything else the shop sends hangs off a status changing: an order is
confirmed, a cake is ready, somebody collects it. These two have no such
moment — they are about something NOT happening.

    an order placed and never paid for
    a collection slot coming up

The first is the expensive one. Three of the shop's first seven orders were
placed and never paid, and nobody was told anything: not the customer, who in
one case had a cake booked for a date they never learned was unconfirmed, and
not the shop, which found out by looking at a screen. The second is cheaper
than its opposite — there is already a message for a cake nobody came for, and
none for preventing that.

WHAT MAKES THIS SAFE TO RUN OFTEN
---------------------------------
Nothing here remembers anything. Whether a reminder has gone out is read from
the outbox itself: queue_and_send records every message, including the ones it
deliberately skipped, so "is there already a row for this order and this
template" is the whole of the bookkeeping. Run it every minute or once a day
and each customer still hears each thing once.

The order row is locked while that check and the send happen, so two replicas
racing cannot both decide to be the first.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.order import Order, OrderStatus
from app.models.user import User
from app.models.whatsapp_message import WhatsAppMessage
from app.services.order_service import _enrich_order, _lock_order, is_payable
from app.services.wa_consent import may_message

settings = get_settings()
logger = logging.getLogger(__name__)


# Long enough that nobody is chased while they are still on the payment page,
# short enough that an abandoned checkout is still warm. PayU sessions do not
# last an hour.
PAYMENT_CHASE_AFTER = timedelta(hours=1)

# A collection reminder is only a reminder if it arrives before the day. The
# window is deliberately wide: whatever the job's interval, the first run that
# sees the order sends it, and the outbox stops the second.
PICKUP_REMINDER_WITHIN = timedelta(hours=24)

# Nothing to chase once the date is behind us. A cake booked for Tuesday and
# unpaid on Thursday is a conversation, not a payment link.
_CLOSED = (OrderStatus.CANCELLED, OrderStatus.COLLECTED, OrderStatus.DELIVERED,
           OrderStatus.NOT_COLLECTED)


def _aware(value: datetime | None) -> datetime | None:
    """
    Timestamps, comparable.

    Postgres hands these back timezone-aware and SQLite hands them back naive,
    so comparing one against `now` works in production and raises in tests —
    or the other way round, which is worse.
    """
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def already_sent(db: Session, order_id: int, template_key: str) -> bool:
    """
    Whether this order has already had this message.

    Counts a skipped one too. If there was nobody to tell — no opt-in, no
    phone — that was decided once and recorded; asking again every minute for
    the rest of the order's life would be a different bug.

    A FAILED row counts as well, which matters most on the day this ships: if
    the sweep runs before the templates exist in Meta, every waiting order
    gets one attempt, Meta refuses it, and the sweep will not try again. That
    is deliberate rather than tidy — the recovery is the retry on the admin
    WhatsApp screen, which replays the row with the amount and the link it was
    built with. Approve the templates first and none of this comes up.
    """
    return db.query(WhatsAppMessage).filter(
        WhatsAppMessage.order_id == order_id,
        WhatsAppMessage.template_key == template_key,
    ).first() is not None


# ─── WHAT IS DUE ──────────────────────────────────────


def due_payment_chases(db: Session, now: datetime | None = None) -> list[Order]:
    """Orders placed, not paid for, and old enough to be worth a nudge."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - PAYMENT_CHASE_AFTER

    candidates = db.query(Order).filter(
        Order.status == OrderStatus.RECEIVED,
    ).all()

    due = []
    for order in candidates:
        if is_payable(order):
            continue                                   # paid, or a legacy COD row
        created = _aware(order.created_at)
        if created is None or created > cutoff:
            continue                                   # still at the payment page
        when = _aware(order.delivery_time)
        if when is not None and when <= now:
            continue                                   # the date has gone
        due.append(order)
    return due


def due_pickup_reminders(db: Session, now: datetime | None = None) -> list[Order]:
    """Paid collection orders whose slot is close enough to be worth saying."""
    now = now or datetime.now(timezone.utc)
    horizon = now + PICKUP_REMINDER_WITHIN

    candidates = db.query(Order).filter(
        Order.fulfilment == "PICKUP",
        Order.delivery_time.isnot(None),
    ).all()

    due = []
    for order in candidates:
        if order.status in _CLOSED:
            continue
        if not is_payable(order):
            continue                                   # it gets the payment chase instead
        when = _aware(order.delivery_time)
        if when is None or not (now < when <= horizon):
            continue
        due.append(order)
    return due


# ─── SENDING ──────────────────────────────────────────


def _pay_link(order: Order) -> str:
    base = settings.frontend_base_url
    return f"{base}/orders/{order.id}" if base else ""


def send_payment_chase(db: Session, order: Order) -> bool:
    """
    Ask for the payment once, and tell the shop at the same moment.

    Returns whether anything was sent, so a caller can count. False means it
    had already been done, which is the normal answer on every run after the
    first.
    """
    from app.services.wa_notifications import _consenting_admins, _money, _send

    locked = _lock_order(db, order.id) or order
    if already_sent(db, locked.id, "order_payment_pending"):
        return False

    _enrich_order(locked)
    customer: User | None = locked.user
    phone = customer.phone if customer else ""
    amount = _money(locked.total_price)

    _send(
        db, locked, "order_payment_pending", phone,
        [locked.customer_name or "Customer", str(locked.id), amount, _pay_link(locked)],
        "payment_pending", "customer",
        may_message(customer) and bool(phone),
    )
    for admin in _consenting_admins(db):
        _send(
            db, locked, "admin_payment_pending", admin.phone,
            [str(locked.id), locked.customer_name or "Customer", phone or "-", amount],
            "payment_pending", "admin", True,
        )
    db.commit()
    return True


def send_pickup_reminder(db: Session, order: Order) -> bool:
    """Remind one customer where and when to collect."""
    from app.services.store_hours import pickup_details
    from app.services.wa_notifications import _delivery_str, _send

    locked = _lock_order(db, order.id) or order
    if already_sent(db, locked.id, "order_pickup_reminder"):
        return False

    _enrich_order(locked)
    customer: User | None = locked.user
    phone = customer.phone if customer else ""

    _send(
        db, locked, "order_pickup_reminder", phone,
        [locked.customer_name or "Customer", str(locked.id),
         _delivery_str(locked), pickup_details(db)["line"]],
        "pickup_reminder", "customer",
        may_message(customer) and bool(phone),
    )
    db.commit()
    return True


def run_due_reminders(db: Session, now: datetime | None = None) -> dict[str, int]:
    """
    Every reminder that has come due. Safe to call as often as you like.

    One order failing must not stop the rest: a bad row would otherwise mean
    nobody gets anything, for as long as that row exists.
    """
    now = now or datetime.now(timezone.utc)
    counts = {"payment_chases": 0, "pickup_reminders": 0}

    for order in due_payment_chases(db, now):
        try:
            if send_payment_chase(db, order):
                counts["payment_chases"] += 1
        except Exception:
            db.rollback()
            logger.exception("[WA] Payment chase failed for order %s", order.id)

    for order in due_pickup_reminders(db, now):
        try:
            if send_pickup_reminder(db, order):
                counts["pickup_reminders"] += 1
        except Exception:
            db.rollback()
            logger.exception("[WA] Pickup reminder failed for order %s", order.id)

    if counts["payment_chases"] or counts["pickup_reminders"]:
        logger.info("[WA] Reminders sent: %s", counts)
    return counts
