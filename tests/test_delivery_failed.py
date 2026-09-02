"""
A delivery that could not be completed, and the withdrawal of cash on delivery.

Until now OUT_FOR_DELIVERY -> DELIVERED was the only edge out of a delivery, so
a rider who found nobody home had to record the attempt as a success. That sent
the customer the "your cake has arrived" template for a cake they never
received, and closed the order as though it were done.

DELIVERY_FAILED is deliberately not terminal: the order goes back out, or it is
cancelled. It is also deliberately silent to the customer — there is no approved
Meta template for it, and inventing one would mean every attempt failing with
error 132001. The admin console surfaces it instead.
"""

from app.models.event import OrderEvent
from app.models.order import OrderStatus, PaymentStatus, VALID_TRANSITIONS
from app.models.user import UserRole
from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus
from app.schemas import StatusUpdate
from app.services.order_service import update_order_status

from tests.conftest import auth, make_order, make_user

import pytest


@pytest.fixture
def out_for_delivery(db):
    """An order in a rider's hands, with an admin to notice what happens to it."""
    admin = make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    admin.whatsapp_opt_in = True
    rider = make_user(db, "Aman", UserRole.RIDER, phone="+919333333333")
    rider.whatsapp_opt_in = True
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    customer.whatsapp_opt_in = True
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider=rider)
    order.payment_status = PaymentStatus.PAID
    db.commit()
    db.refresh(order)
    return order, rider, customer


# ─── THE LIFECYCLE ───────────────────────────────────

def test_a_failed_attempt_is_reachable_from_out_for_delivery(db):
    assert OrderStatus.DELIVERY_FAILED in VALID_TRANSITIONS[OrderStatus.OUT_FOR_DELIVERY]


def test_a_failed_delivery_can_go_out_again_or_be_cancelled(db):
    allowed = VALID_TRANSITIONS[OrderStatus.DELIVERY_FAILED]
    assert OrderStatus.OUT_FOR_DELIVERY in allowed, "a second attempt must be possible"
    assert OrderStatus.CANCELLED in allowed
    assert OrderStatus.DELIVERED not in allowed, \
        "a re-attempt is a real dispatch and should be recorded as one"


def test_rider_records_a_failed_delivery(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery

    res = client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                      json={"reason": "Nobody home, waited 15 minutes"})

    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERY_FAILED


def test_the_reason_lands_on_the_order_timeline(client, db, wa_secret, out_for_delivery):
    """The reason is the whole point — it is what the next person acts on."""
    order, rider, _ = out_for_delivery

    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Wrong address, customer not answering"})

    events = db.query(OrderEvent).filter(
        OrderEvent.order_id == order.id,
        OrderEvent.event_type == "STATUS_CHANGED",
    ).all()
    reasons = [e.payload.get("reason") for e in events if e.payload]
    assert "Wrong address, customer not answering" in reasons


def test_a_reason_is_required(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery
    res = client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                      json={"reason": ""})
    assert res.status_code == 422
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


def test_the_customer_is_not_told_the_cake_arrived(client, db, wa_secret, out_for_delivery):
    """The entire reason this status exists."""
    order, rider, _ = out_for_delivery

    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Nobody home"})

    sent = [m.template_key for m in db.query(WhatsAppMessage).filter(
        WhatsAppMessage.order_id == order.id,
        WhatsAppMessage.status == WhatsAppMessageStatus.SENT).all()]
    assert "order_delivered" not in sent, "told the customer about a cake they never got"


def test_another_rider_cannot_fail_someone_elses_delivery(client, db, wa_secret, out_for_delivery):
    order, _, _ = out_for_delivery
    intruder = make_user(db, "Other", UserRole.RIDER, phone="+919444444444")

    res = client.post(f"/orders/{order.id}/delivery-failed", headers=auth(intruder),
                      json={"reason": "not mine"})

    assert res.status_code == 403
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


def test_it_can_only_fail_from_out_for_delivery(client, db, wa_secret):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    rider = make_user(db, "Aman", UserRole.RIDER, phone="+919333333333")
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, customer, OrderStatus.PACKAGED, rider=rider)

    res = client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                      json={"reason": "too early"})
    assert res.status_code == 400


def test_admin_sends_a_failed_delivery_out_again(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()

    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Nobody home"})
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERY_FAILED

    res = client.post(f"/orders/{order.id}/retry-delivery", headers=auth(admin))
    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


def test_tracking_is_torn_down_when_a_delivery_fails(client, db, wa_secret,
                                                     out_for_delivery, fake_redis):
    """Nobody is carrying it, so it must not sit on the map showing a position."""
    from app.services.delivery_tracking import update_rider_location

    order, rider, _ = out_for_delivery
    update_rider_location(order.id, 26.8467, 80.9462)
    assert fake_redis.exists(f"delivery:{order.id}") == 1

    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Nobody home"})

    assert fake_redis.exists(f"delivery:{order.id}") == 0


def test_a_failed_delivery_stays_in_the_riders_queue(client, db, wa_secret, out_for_delivery):
    """It should not vanish the moment they report the problem."""
    order, rider, _ = out_for_delivery
    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Nobody home"})

    queue = client.get("/orders/rider/my-queue", headers=auth(rider)).json()
    assert any(o["id"] == order.id for o in queue)


def test_a_failed_delivery_stays_on_the_admin_fleet_board(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()

    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Nobody home"})

    fleet = client.get("/delivery/admin/active", headers=auth(admin)).json()
    assert any(d["order_id"] == order.id for d in fleet["deliveries"]), \
        "the one delivery needing a decision dropped off the board"


def test_the_dashboard_counts_it(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()

    client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                json={"reason": "Nobody home"})

    stats = client.get("/dashboard/stats", headers=auth(admin)).json()
    assert stats["delivery_failed_orders"] == 1


# ─── CASH ON DELIVERY IS WITHDRAWN ───────────────────

def test_cod_orders_cannot_be_created(client, db, customer_user, catalogue_min):
    """The client withdrew COD. Refuse plainly rather than charging the card."""
    order = make_order(db, customer_user, OrderStatus.CONFIRMED)

    res = client.post("/payments/create-order", headers=auth(customer_user),
                      json={"order_id": order.id, "payment_method": "COD"})

    assert res.status_code == 400
    assert "cash on delivery" in res.json()["detail"].lower()
    db.refresh(order)
    assert order.payment_status == PaymentStatus.PENDING


def test_the_collect_cash_endpoint_is_gone(client, db, customer_user):
    order = make_order(db, customer_user, OrderStatus.OUT_FOR_DELIVERY)
    res = client.post(f"/orders/{order.id}/collect-cod", headers=auth(customer_user))
    assert res.status_code == 404


def test_an_unpaid_online_order_still_cannot_enter_production(db, customer_user):
    """With COD gone, payment is the only route into the kitchen."""
    from app.services.order_service import is_payable

    order = make_order(db, customer_user, OrderStatus.CONFIRMED)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PENDING
    db.commit()

    assert is_payable(order) is False


def test_legacy_cod_orders_are_not_stranded(db, customer_user):
    """
    Orders placed while COD existed must still be able to finish. Refusing them
    would freeze work the bakery has already committed to.
    """
    from app.services.order_service import is_payable

    order = make_order(db, customer_user, OrderStatus.CONFIRMED)
    order.payment_method = "COD"
    order.payment_status = PaymentStatus.PENDING
    db.commit()

    assert is_payable(order) is True


@pytest.fixture
def customer_user(db):
    return make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")


@pytest.fixture
def catalogue_min(db):
    return None


# ─── THE REASON RULE BELONGS TO THE TRANSITION ───────

def test_a_plain_status_patch_cannot_skip_the_reason(client, db, wa_secret, out_for_delivery):
    """
    The dedicated endpoint validates the reason with Pydantic, but the generic
    admin status update reaches the same transition. Without the rule in the
    service, an admin working from the orders table could file a failed
    delivery with no explanation -- and the reason is the only thing the next
    person has to act on.
    """
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()

    res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                       json={"status": "DELIVERY_FAILED"})

    assert res.status_code == 422
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


def test_a_plain_status_patch_with_a_reason_is_accepted(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()

    res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                       json={"status": "DELIVERY_FAILED", "reason": "Gate locked"})

    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERY_FAILED


# ─── THE TWO REDISPATCH PATHS MUST AGREE ─────────────
#
# A failed delivery can be sent out again from the fleet board
# (POST /orders/{id}/retry-delivery) or from the admin orders table
# (PATCH /orders/{id}/status). Two doors into one transition is how the last
# bug got in: the reason requirement lived in one endpoint, so the other door
# skipped it. These tests hold the doors level.

def _fail_it(client, order, rider):
    r = client.post(f"/orders/{order.id}/delivery-failed", headers=auth(rider),
                    json={"reason": "Nobody home"})
    assert r.status_code == 200
    return r


def test_fleet_board_redispatch_needs_a_rider(client, db, wa_secret, out_for_delivery):
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()
    _fail_it(client, order, rider)

    order.assigned_rider_id = None       # e.g. the rider went off duty
    db.commit()

    res = client.post(f"/orders/{order.id}/retry-delivery", headers=auth(admin))
    assert res.status_code == 400
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERY_FAILED


def test_admin_table_redispatch_needs_a_rider_too(client, db, wa_secret, out_for_delivery):
    """
    The orders table reaches the same transition through the generic status
    PATCH. Before the prerequisite moved into update_order_status this path
    dispatched an order with nobody carrying it, and tracking recorded it
    against rider 0.
    """
    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()
    _fail_it(client, order, rider)

    order.assigned_rider_id = None
    db.commit()

    res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                       json={"status": "OUT_FOR_DELIVERY"})
    assert res.status_code == 400, "the orders table bypassed the rider requirement"
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERY_FAILED


@pytest.mark.parametrize("path", ["fleet", "orders_table"])
def test_both_redispatch_paths_restart_tracking(client, db, wa_secret, fake_redis,
                                                out_for_delivery, path):
    """
    Whichever door is used, tracking comes back through the same lifecycle.

    Two keys, and they mean different things: `delivery:{id}` is the rider's
    last reported position and only GPS writes it, while `delivery:{id}:meta`
    is the tracking session itself. A redispatch opens a session; it does not
    invent a position, which is the documented "awaiting_gps" state.
    """
    import json

    from app.services.delivery_tracking import update_rider_location

    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()
    update_rider_location(order.id, 26.8467, 80.9462)

    _fail_it(client, order, rider)
    assert fake_redis.exists(f"delivery:{order.id}") == 0, "stale position kept"
    assert fake_redis.exists(f"delivery:{order.id}:meta") == 0, "session kept"

    if path == "fleet":
        res = client.post(f"/orders/{order.id}/retry-delivery", headers=auth(admin))
    else:
        res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                           json={"status": "OUT_FOR_DELIVERY"})

    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY

    meta_raw = fake_redis.get(f"delivery:{order.id}:meta")
    assert meta_raw, "tracking session did not restart"
    meta = json.loads(meta_raw)
    assert meta["rider_id"] == rider.id,         f"tracking restarted against rider {meta['rider_id']}, not the assigned one"
    assert fake_redis.exists(f"delivery:{order.id}") == 0,         "a redispatch must not resurrect the position from the failed attempt"


@pytest.mark.parametrize("path", ["fleet", "orders_table"])
def test_neither_path_can_redispatch_from_the_wrong_status(client, db, wa_secret,
                                                           out_for_delivery, path):
    order, rider, _ = out_for_delivery      # still OUT_FOR_DELIVERY, never failed
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()

    if path == "fleet":
        res = client.post(f"/orders/{order.id}/retry-delivery", headers=auth(admin))
    else:
        res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                           json={"status": "OUT_FOR_DELIVERY"})

    assert res.status_code == 400


# ─── THE INVARIANT, PINNED WHOLE ─────────────────────

def test_a_rejected_failure_leaves_absolutely_nothing_behind(client, db, wa_secret,
                                                             fake_redis, out_for_delivery):
    """
    A 422 must be inert: no status change, no timeline entry, and tracking left
    exactly as it was. A guard that rejects the request but has already torn
    down the delivery would be worse than no guard.
    """
    from app.services.delivery_tracking import update_rider_location

    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()
    update_rider_location(order.id, 26.8467, 80.9462)

    before = db.query(OrderEvent).filter(OrderEvent.order_id == order.id).count()

    res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                       json={"status": "DELIVERY_FAILED"})

    assert res.status_code == 422
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY
    assert db.query(OrderEvent).filter(OrderEvent.order_id == order.id).count() == before, \
        "a rejected transition wrote to the timeline"
    assert fake_redis.exists(f"delivery:{order.id}") == 1, \
        "a rejected transition tore down a live delivery"


def test_an_accepted_failure_records_exactly_one_of_everything(client, db, wa_secret,
                                                               fake_redis, out_for_delivery):
    from app.services.delivery_tracking import update_rider_location

    order, rider, _ = out_for_delivery
    admin = db.query(type(rider)).filter_by(role=UserRole.ADMIN).first()
    update_rider_location(order.id, 26.8467, 80.9462)

    res = client.patch(f"/orders/{order.id}/status", headers=auth(admin),
                       json={"status": "DELIVERY_FAILED", "reason": "Gate locked"})

    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERY_FAILED

    events = [e for e in db.query(OrderEvent).filter(
        OrderEvent.order_id == order.id,
        OrderEvent.event_type == "STATUS_CHANGED").all()
        if e.payload and e.payload.get("to") == "DELIVERY_FAILED"]
    assert len(events) == 1, f"expected one STATUS_CHANGED, got {len(events)}"
    assert events[0].payload["reason"] == "Gate locked"
    assert events[0].payload["from"] == "OUT_FOR_DELIVERY"

    assert fake_redis.exists(f"delivery:{order.id}") == 0, "tracking was not removed"
