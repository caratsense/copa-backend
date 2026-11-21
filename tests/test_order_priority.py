"""
Flagging an order urgent.

WHAT THIS IS FOR
----------------
The admin order page has a priority control: the shop saying "this one
first" to itself, so the queue can float it to the top of its section. It is
not a status. Nothing gates on it, no transition reads it, and no message is
sent — which is exactly why it needs pinning down, because a field nothing
else depends on is a field that can quietly stop working and nobody notices
until a rush cake comes out last.

WHY A ROUTE TEST AND NOT A SERVICE TEST
---------------------------------------
The first version of this endpoint wrote the value, committed, and then threw
on the way out — it called _enrich_order(db, order) when the helper takes the
order alone. The database was right and the response was a 500. A 500 carries
no CORS headers, so the browser reported it as "could not reach the server"
and the page went on showing the old value while the row underneath had
already changed. Everything below goes through the HTTP route for that
reason.
"""

import pytest

from app.models.event import OrderEvent
from app.models.order import OrderStatus
from app.models.user import UserRole

from tests.conftest import auth, make_order, make_user


@pytest.fixture
def admin(db):
    return make_user(db, "Shriya Admin", UserRole.ADMIN)


@pytest.fixture
def order(db, customer):
    return make_order(db, customer, status=OrderStatus.CONFIRMED)


def _set(client, admin, order_id, value):
    return client.patch(
        f"/orders/{order_id}/priority",
        json={"priority": value},
        headers=auth(admin),
    )


# ── Setting it ────────────────────────────────────────


def test_rush_is_set_and_comes_back_on_the_order(client, admin, order):
    res = _set(client, admin, order.id, "RUSH")
    assert res.status_code == 200, res.text
    assert res.json()["priority"] == "RUSH"

    # And on the next read, not only in the response we just built.
    again = client.get(f"/orders/{order.id}", headers=auth(admin))
    assert again.json()["priority"] == "RUSH"


def test_the_response_is_still_a_whole_order(client, admin, order):
    """
    The 500 this test exists for was in the enrichment, after the commit.

    So it is not enough that the status code is 200 — the body has to carry
    the fields the page reads off it, which is what _enrich_order adds.
    """
    body = _set(client, admin, order.id, "RUSH").json()
    for field in ("id", "status", "total_price", "customer_name", "items"):
        assert field in body, f"{field} missing from the response"
    assert body["customer_name"] == "Priya Customer"


def test_it_is_case_and_space_insensitive(client, admin, order):
    assert _set(client, admin, order.id, "  rush  ").json()["priority"] == "RUSH"


def test_clearing_it_puts_the_order_back_to_ordinary(client, admin, order):
    _set(client, admin, order.id, "RUSH")
    res = _set(client, admin, order.id, None)
    assert res.status_code == 200, res.text
    assert res.json()["priority"] is None


def test_an_empty_string_clears_it_too(client, admin, order):
    _set(client, admin, order.id, "RUSH")
    assert _set(client, admin, order.id, "").json()["priority"] is None


def test_setting_the_same_value_twice_is_harmless(client, admin, order, db):
    _set(client, admin, order.id, "RUSH")
    res = _set(client, admin, order.id, "RUSH")
    assert res.status_code == 200, res.text
    assert res.json()["priority"] == "RUSH"
    # And it does not write a second timeline entry saying nothing changed.
    events = db.query(OrderEvent).filter(
        OrderEvent.order_id == order.id,
        OrderEvent.event_type == "ORDER_PRIORITY_SET",
    ).all()
    assert len(events) == 1


# ── Refusing it ───────────────────────────────────────


def test_an_unknown_priority_is_refused(client, admin, order):
    res = _set(client, admin, order.id, "URGENT")
    assert res.status_code == 400
    assert "URGENT" in res.json()["detail"]

    # And nothing was written.
    assert client.get(f"/orders/{order.id}", headers=auth(admin)).json()["priority"] is None


def test_a_missing_order_is_a_404(client, admin):
    assert _set(client, admin, 999_999, "RUSH").status_code == 404


def test_a_customer_cannot_flag_their_own_order(client, customer, order):
    res = client.patch(
        f"/orders/{order.id}/priority",
        json={"priority": "RUSH"},
        headers=auth(customer),
    )
    assert res.status_code == 403


def test_a_baker_cannot_flag_an_order(client, db, order):
    baker = make_user(db, "Ravi Baker", UserRole.BAKER)
    res = client.patch(
        f"/orders/{order.id}/priority",
        json={"priority": "RUSH"},
        headers=auth(baker),
    )
    assert res.status_code == 403


# ── Recorded ──────────────────────────────────────────


def test_it_lands_on_the_timeline_with_who_said_so(client, admin, order, db):
    _set(client, admin, order.id, "RUSH")
    event = db.query(OrderEvent).filter(
        OrderEvent.order_id == order.id,
        OrderEvent.event_type == "ORDER_PRIORITY_SET",
    ).one()
    assert event.payload["to"] == "RUSH"
    assert event.payload["from"] is None
    assert event.payload["by"] == "Shriya Admin"


def test_clearing_is_recorded_as_well(client, admin, order, db):
    _set(client, admin, order.id, "RUSH")
    _set(client, admin, order.id, None)
    events = db.query(OrderEvent).filter(
        OrderEvent.order_id == order.id,
        OrderEvent.event_type == "ORDER_PRIORITY_SET",
    ).order_by(OrderEvent.id).all()
    assert [e.payload["to"] for e in events] == ["RUSH", None]
    assert events[1].payload["from"] == "RUSH"


# ── Not the customer's business ───────────────────────


def test_the_customer_tracking_view_never_mentions_it(client, admin, customer, order):
    """
    How the shop ranks its own morning is not something to show the person
    waiting for the cake: "RUSH" on their tracking page reads as a promise
    nobody made them.
    """
    _set(client, admin, order.id, "RUSH")
    res = client.get(f"/orders/track/{order.id}", headers=auth(customer))
    if res.status_code == 404:
        pytest.skip("no tracking route on this build")
    assert res.status_code == 200, res.text
    assert "priority" not in res.json()
