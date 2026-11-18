"""
The two messages that fire because time passed.

WHAT WAS NOT HAPPENING
----------------------
An order placed and never paid for was told nothing, by anybody, ever. Three
of the shop's first seven orders died exactly there — two were one customer
trying twice before his third attempt worked, and the third was a cake booked
for a Friday whose customer was never told it was not confirmed. The shop's
only way of knowing was to look at a screen.

And a collection-only bakery with booked slots had a message for a cake nobody
came for, and none for preventing it.

Neither could exist, however many templates were approved, because nothing in
the deployment ran on a clock.

WHAT THESE PIN
--------------
Mostly that nobody is messaged twice. A reminder is the one kind of message
where the bug and the feature look identical from the outside: the system
working is a customer hearing from you, and the system broken is a customer
hearing from you again, and again. Whether something has been sent is read
from the outbox, so these also pin that a SKIPPED send counts as done.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.order import OrderStatus, PaymentStatus
from app.models.user import UserRole
from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus
from app.services.wa_reminders import (
    PAYMENT_CHASE_AFTER,
    due_payment_chases,
    due_pickup_reminders,
    run_due_reminders,
)

from tests.conftest import make_order, make_user


NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def people(db):
    """A customer and the shop, both reachable."""
    def opted(name, role, phone):
        u = make_user(db, name, role, phone=phone)
        u.whatsapp_opt_in = True
        db.commit()
        return u

    return {
        "customer": opted("Prashasti", UserRole.CUSTOMER, "+919222222222"),
        "admin": opted("Shriya", UserRole.ADMIN, "+919554444462"),
    }


def _order(db, customer, *, status=OrderStatus.RECEIVED, paid=False,
           placed_ago=timedelta(hours=2), due_in=timedelta(days=2),
           fulfilment="PICKUP", method="ONLINE"):
    order = make_order(db, customer, status=status, fulfilment=fulfilment)
    order.payment_method = method
    order.payment_status = PaymentStatus.PAID if paid else PaymentStatus.PENDING
    order.created_at = NOW - placed_ago
    order.delivery_time = (NOW + due_in) if due_in is not None else None
    db.commit()
    return order


def keys(db, order_id=None):
    q = db.query(WhatsAppMessage)
    if order_id:
        q = q.filter(WhatsAppMessage.order_id == order_id)
    return [m.template_key for m in q.order_by(WhatsAppMessage.id.asc()).all()]


# ─── CHASING THE MONEY ────────────────────────────────


def test_an_unpaid_order_is_chased(client, db, wa_secret, people):
    """The message that did not exist, for the orders that were lost."""
    order = _order(db, people["customer"])

    counts = run_due_reminders(db, now=NOW)

    assert counts["payment_chases"] == 1
    assert keys(db, order.id) == ["order_payment_pending", "admin_payment_pending"]
    assert all(m.status == WhatsAppMessageStatus.SENT
               for m in db.query(WhatsAppMessage).all())


def test_the_customer_is_told_what_to_pay_and_where(client, db, wa_secret, people):
    order = _order(db, people["customer"])
    run_due_reminders(db, now=NOW)

    msg = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.template_key == "order_payment_pending").one()
    name, oid, amount, link = msg.payload
    assert name == "Prashasti"
    assert oid == str(order.id)
    assert "1,000" in amount or "1000" in amount     # make_order prices at 1000
    assert link.endswith(f"/orders/{order.id}")


def test_nobody_is_chased_while_they_are_still_paying(client, db, wa_secret, people):
    """
    An hour, because a customer at the payment page is not an abandoned
    checkout and a message that arrives mid-payment reads as an accusation.
    """
    _order(db, people["customer"], placed_ago=PAYMENT_CHASE_AFTER - timedelta(minutes=5))

    assert run_due_reminders(db, now=NOW)["payment_chases"] == 0
    assert keys(db) == []


def test_a_paid_order_is_not_chased(client, db, wa_secret, people):
    _order(db, people["customer"], status=OrderStatus.CONFIRMED, paid=True)

    assert due_payment_chases(db, NOW) == []


def test_a_legacy_cod_order_is_not_chased(client, db, wa_secret, people):
    """COD is withdrawn but grandfathered: those owe nothing up front."""
    _order(db, people["customer"], method="COD")

    assert due_payment_chases(db, NOW) == []


def test_an_order_whose_date_has_gone_is_not_chased(client, db, wa_secret, people):
    """
    A cake booked for Tuesday and unpaid on Thursday is a conversation, not a
    payment link.
    """
    _order(db, people["customer"], due_in=-timedelta(days=1))

    assert due_payment_chases(db, NOW) == []


def test_a_cancelled_order_is_not_chased(client, db, wa_secret, people):
    _order(db, people["customer"], status=OrderStatus.CANCELLED)

    assert due_payment_chases(db, NOW) == []


# ─── BEFORE THE SLOT ──────────────────────────────────


def test_a_collection_tomorrow_is_reminded(client, db, wa_secret, people):
    order = _order(db, people["customer"], status=OrderStatus.PACKAGED,
                   paid=True, due_in=timedelta(hours=20))

    counts = run_due_reminders(db, now=NOW)

    assert counts["pickup_reminders"] == 1
    assert keys(db, order.id) == ["order_pickup_reminder"]


def test_a_collection_next_week_is_not(client, db, wa_secret, people):
    _order(db, people["customer"], status=OrderStatus.PACKAGED,
           paid=True, due_in=timedelta(days=5))

    assert due_pickup_reminders(db, NOW) == []


def test_a_slot_already_passed_is_not_reminded(client, db, wa_secret, people):
    _order(db, people["customer"], status=OrderStatus.PACKAGED,
           paid=True, due_in=-timedelta(hours=1))

    assert due_pickup_reminders(db, NOW) == []


def test_a_delivery_is_not_reminded_to_collect(client, db, wa_secret, people):
    """It is not coming to them; they are not coming to it."""
    _order(db, people["customer"], status=OrderStatus.PACKAGED, paid=True,
           fulfilment="DELIVERY", due_in=timedelta(hours=20))

    assert due_pickup_reminders(db, NOW) == []


def test_an_unpaid_collection_gets_the_payment_chase_instead(client, db, wa_secret, people):
    """
    Both rules match this order. Telling somebody when to collect a cake
    nobody is baking would be the wrong one of the two.
    """
    order = _order(db, people["customer"], paid=False, due_in=timedelta(hours=20))

    counts = run_due_reminders(db, now=NOW)

    assert counts == {"payment_chases": 1, "pickup_reminders": 0}
    assert "order_pickup_reminder" not in keys(db, order.id)


@pytest.mark.parametrize("status", [OrderStatus.COLLECTED, OrderStatus.CANCELLED,
                                    OrderStatus.NOT_COLLECTED])
def test_a_closed_order_is_not_reminded(client, db, wa_secret, people, status):
    _order(db, people["customer"], status=status, paid=True, due_in=timedelta(hours=20))

    assert due_pickup_reminders(db, NOW) == []


# ─── ONCE, AND ONLY ONCE ──────────────────────────────


def test_a_sweep_that_runs_again_sends_nothing(client, db, wa_secret, people):
    """
    The sweep runs every few minutes for as long as the app is up. The whole
    design rests on this: a customer hears each thing once however often it
    runs.
    """
    _order(db, people["customer"])

    first = run_due_reminders(db, now=NOW)
    second = run_due_reminders(db, now=NOW + timedelta(minutes=15))
    third = run_due_reminders(db, now=NOW + timedelta(hours=6))

    assert first["payment_chases"] == 1
    assert second["payment_chases"] == 0
    assert third["payment_chases"] == 0
    assert len(db.query(WhatsAppMessage).filter(
        WhatsAppMessage.template_key == "order_payment_pending").all()) == 1


def test_a_reminder_that_was_skipped_still_counts_as_done(client, db, wa_secret, people):
    """
    A customer with no opt-in is recorded as skipped, once. Asking again every
    fifteen minutes for the life of the order would be a different bug, and
    the outbox would fill with a column of the same refusal.
    """
    people["customer"].whatsapp_opt_in = False
    db.commit()
    order = _order(db, people["customer"])

    run_due_reminders(db, now=NOW)
    run_due_reminders(db, now=NOW + timedelta(hours=2))

    mine = [m for m in db.query(WhatsAppMessage).all()
            if m.template_key == "order_payment_pending"]
    assert len(mine) == 1
    assert mine[0].status == WhatsAppMessageStatus.SKIPPED
    assert "opt-in" in (mine[0].last_error or "")


def test_the_shop_is_told_even_when_the_customer_cannot_be(client, db, wa_secret, people):
    """
    The customer being unreachable is exactly when somebody has to pick up a
    phone, so the admin half must not be skipped with it.
    """
    people["customer"].whatsapp_opt_in = False
    db.commit()
    _order(db, people["customer"])

    run_due_reminders(db, now=NOW)

    admin_msg = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.template_key == "admin_payment_pending").one()
    assert admin_msg.status == WhatsAppMessageStatus.SENT


def test_two_orders_are_two_chases(client, db, wa_secret, people):
    """Keyed per order, not per customer."""
    _order(db, people["customer"])
    _order(db, people["customer"])

    assert run_due_reminders(db, now=NOW)["payment_chases"] == 2
