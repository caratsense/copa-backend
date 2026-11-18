"""
The number on the dashboard's "needs you" banner.

WHAT IT SAID
------------
"3 orders not yet with a baker", on a morning when the orders screen showed
nothing at all: Open 0, Everything 7. Both screens were reading the same seven
rows.

The count was every order at RECEIVED or CONFIRMED whatever its payment. The
orders screen drops unpaid attempts — an ONLINE order still at RECEIVED with
nothing paid is somebody who got to the payment page and left, and is_payable
already stops the kitchen touching it — so three abandoned checkouts were work
on one screen and not orders at all on the other. The honest number was the
other one.

These pin the rule the kitchen actually runs on, in both directions, because
counting too few would be the worse failure: an order paid for and waiting
would stop being mentioned anywhere.
"""

import pytest

from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.user import UserRole

from tests.conftest import auth, make_order, make_user


def _stats(client, admin):
    res = client.get("/dashboard/stats", headers=auth(admin))
    assert res.status_code == 200, res.text
    return res.json()


def _order(db, customer, status, *, paid, method="ONLINE"):
    order = make_order(db, customer, status=status)
    order.payment_method = method
    order.payment_status = PaymentStatus.PAID if paid else PaymentStatus.PENDING
    db.commit()
    return order


# ─── WHAT COUNTS ──────────────────────────────────────


def test_an_abandoned_checkout_is_not_work_waiting(client, db, admin, customer):
    """
    The three orders on the banner. Placed, never paid, nothing owed, nobody
    baking — and the orders screen already agreed they were not work.
    """
    _order(db, customer, OrderStatus.RECEIVED, paid=False)
    _order(db, customer, OrderStatus.RECEIVED, paid=False)

    assert _stats(client, admin)["pending_orders"] == 0


def test_a_paid_order_waiting_to_be_started_is_counted(client, db, admin, customer):
    """The thing the banner is for. Counting too few is the worse mistake."""
    _order(db, customer, OrderStatus.CONFIRMED, paid=True)

    assert _stats(client, admin)["pending_orders"] == 1


def test_a_paid_order_still_at_received_is_counted(client, db, admin, customer):
    """
    Payment flips an order to CONFIRMED, so this is the gap between the money
    landing and that happening. It is real work either way, and the banner
    used to count it and then link to a screen filtered to CONFIRMED, where it
    could not appear.
    """
    _order(db, customer, OrderStatus.RECEIVED, paid=True)

    assert _stats(client, admin)["pending_orders"] == 1


def test_a_legacy_cod_order_is_counted_unpaid(client, db, admin, customer):
    """
    Cash on delivery is withdrawn but grandfathered: those rows are workable
    before any money arrives, which is exactly what is_payable says and what
    the kitchen goes by.
    """
    _order(db, customer, OrderStatus.CONFIRMED, paid=False, method="COD")

    assert _stats(client, admin)["pending_orders"] == 1


def test_an_order_already_in_the_kitchen_is_not_waiting(client, db, admin, customer):
    """It has been started. It belongs to the next tile, not this banner."""
    _order(db, customer, OrderStatus.IN_PRODUCTION, paid=True)

    assert _stats(client, admin)["pending_orders"] == 0


@pytest.mark.parametrize("status", [OrderStatus.DELIVERED, OrderStatus.CANCELLED,
                                    OrderStatus.COLLECTED])
def test_a_finished_order_is_not_waiting(client, db, admin, customer, status):
    _order(db, customer, status, paid=True)

    assert _stats(client, admin)["pending_orders"] == 0


def test_the_mix_that_produced_the_wrong_number(client, db, admin, customer):
    """
    The morning itself: a few abandoned checkouts, some finished orders, and
    one real job. Seven rows, one of them work.
    """
    for _ in range(3):
        _order(db, customer, OrderStatus.RECEIVED, paid=False)
    _order(db, customer, OrderStatus.DELIVERED, paid=True)
    _order(db, customer, OrderStatus.CANCELLED, paid=False)
    _order(db, customer, OrderStatus.COLLECTED, paid=True)
    _order(db, customer, OrderStatus.CONFIRMED, paid=True)

    assert db.query(Order).count() == 7
    assert _stats(client, admin)["pending_orders"] == 1
