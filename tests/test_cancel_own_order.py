"""
A customer calling off their own order.

There was no way to do it. CANCELLED was a status the admin table could set,
but the order page offered one button and it said Pay, so an unpaid order the
customer no longer wanted could only be abandoned - which looks exactly like a
payment that failed.
"""

import pytest

from app.models.order import OrderStatus, PaymentStatus

from tests.conftest import auth, make_order


def test_a_customer_cancels_their_unpaid_order(db, client, customer):
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    res = client.post(f"/orders/{order.id}/cancel", json={}, headers=auth(customer))

    assert res.status_code == 200, res.text
    db.refresh(order)
    assert order.status == OrderStatus.CANCELLED


def test_cancelling_twice_is_not_an_error(db, client, customer):
    """A second tap, or a stale page, should not produce a failure."""
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    client.post(f"/orders/{order.id}/cancel", json={}, headers=auth(customer))
    res = client.post(f"/orders/{order.id}/cancel", json={}, headers=auth(customer))

    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"


def test_nobody_cancels_somebody_elses_order(db, client, customer, other_customer):
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    res = client.post(f"/orders/{order.id}/cancel", json={},
                      headers=auth(other_customer))

    assert res.status_code == 403
    db.refresh(order)
    assert order.status == OrderStatus.RECEIVED


def test_a_paid_order_is_not_cancelled_by_a_button(db, client, customer):
    """Cancelling a paid order is a refund, and a refund is the shop's call."""
    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    order.payment_status = PaymentStatus.PAID
    db.commit()

    res = client.post(f"/orders/{order.id}/cancel", json={}, headers=auth(customer))

    assert res.status_code == 400
    assert "refund" in res.json()["detail"].lower()
    db.refresh(order)
    assert order.status == OrderStatus.RECEIVED


def test_a_cake_already_being_made_is_not_cancelled_here(db, client, customer, baker):
    order = make_order(db, customer, status=OrderStatus.IN_PRODUCTION, baker=baker)

    res = client.post(f"/orders/{order.id}/cancel", json={}, headers=auth(customer))

    assert res.status_code == 400
    assert "already started" in res.json()["detail"].lower()


def test_cancelling_records_the_transition(db, client, customer):
    """Routed through update_order_status, so it emits like any other change."""
    from app.models.event import OrderEvent

    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    client.post(f"/orders/{order.id}/cancel", json={}, headers=auth(customer))

    events = (db.query(OrderEvent)
              .filter(OrderEvent.order_id == order.id,
                      OrderEvent.event_type == "STATUS_CHANGED")
              .all())
    assert any(e.payload.get("to") == "CANCELLED" for e in events)
