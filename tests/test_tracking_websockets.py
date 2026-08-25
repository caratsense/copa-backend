"""
Tracking WebSocket authorisation and routing.

The order id in these URLs is attacker-controlled, so every socket is asserted
both for who it lets in and for who it keeps out.
"""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.models.order import OrderStatus

from tests.conftest import make_order, token_for


def ws_url(path, user):
    return f"{path}?token={token_for(user)}"


def expect_rejected(client, url):
    """A socket that is refused during the handshake, with its close code."""
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_text()
    return exc.value.code


# ─── /ws/track/{order_id} — WATCHING A DELIVERY ──────

def test_owner_can_watch_their_own_delivery(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/track/{order.id}", customer)) as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_another_customer_cannot_watch_by_guessing_the_order_id(client, db, customer, other_customer, rider):
    """The core leak: any logged-in account could watch any stranger's rider."""
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    assert expect_rejected(client, ws_url(f"/ws/track/{order.id}", other_customer)) == 4003


def test_admin_can_watch_any_delivery(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/track/{order.id}", admin)) as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_assigned_rider_can_watch_their_delivery(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/track/{order.id}", rider)) as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_unassigned_rider_cannot_watch_someone_elses_delivery(client, db, customer, rider, other_rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    assert expect_rejected(client, ws_url(f"/ws/track/{order.id}", other_rider)) == 4003


def test_unauthenticated_watcher_is_rejected(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    assert expect_rejected(client, f"/ws/track/{order.id}") == 4001


def test_invalid_token_is_rejected(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    assert expect_rejected(client, f"/ws/track/{order.id}?token=not-a-jwt") == 4001


def test_deactivated_user_is_rejected_even_with_a_valid_token(client, db, customer, rider):
    """Role and status come from the database, not from the token's claims."""
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    url = ws_url(f"/ws/track/{order.id}", customer)

    customer.is_active = False
    db.commit()

    assert expect_rejected(client, url) == 4001


def test_nonexistent_order_is_indistinguishable_from_forbidden(client, customer):
    """Probing must not reveal which order ids exist."""
    assert expect_rejected(client, ws_url("/ws/track/999999", customer)) == 4003


# ─── /ws/rider/{order_id} — SUBMITTING GPS ───────────

def test_assigned_rider_can_submit_gps(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as ws:
        ws.send_json({"lat": 26.85, "lng": 80.95})
        assert ws.receive_json()["status"] == "ok"

    from app.services.delivery_tracking import get_rider_location
    assert get_rider_location(order.id)["rider_lat"] == 26.85


def test_a_different_rider_cannot_submit_gps_for_that_order(client, db, customer, rider, other_rider):
    """Spoofing another rider's position by editing the order id in the URL."""
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    assert expect_rejected(client, ws_url(f"/ws/rider/{order.id}", other_rider)) == 4003

    from app.services.delivery_tracking import get_rider_location
    assert get_rider_location(order.id) is None


def test_customer_cannot_submit_gps(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    # The customer owns the order, so passes the ownership gate, and is then
    # rejected on role — GPS submission is riders and admins only.
    assert expect_rejected(client, ws_url(f"/ws/rider/{order.id}", customer)) == 4003


def test_unauthenticated_rider_socket_is_rejected(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    assert expect_rejected(client, f"/ws/rider/{order.id}") == 4001


def test_gps_is_refused_for_an_order_not_out_for_delivery(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.PACKAGED, rider)
    assert expect_rejected(client, ws_url(f"/ws/rider/{order.id}", rider)) == 4003


@pytest.mark.parametrize("payload", [
    {"lat": 91.0, "lng": 80.0},
    {"lat": -91.0, "lng": 80.0},
    {"lat": 26.0, "lng": 181.0},
    {"lat": 26.0, "lng": -181.0},
    {"lat": "not-a-number", "lng": 80.0},
    {"lat": None, "lng": 80.0},
    {"lat": 26.0},
    {"lat": True, "lng": 80.0},
])
def test_invalid_coordinates_are_rejected_without_dropping_the_socket(client, db, customer, rider, payload):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as ws:
        ws.send_json(payload)
        assert "error" in ws.receive_json()

        # The connection survives, so one bad reading doesn't end tracking.
        ws.send_json({"lat": 26.85, "lng": 80.95})
        assert ws.receive_json()["status"] == "ok"

    from app.services.delivery_tracking import get_rider_location
    assert get_rider_location(order.id)["rider_lat"] == 26.85


def test_malformed_frames_do_not_drop_the_socket(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as ws:
        ws.send_text("{not json")
        assert "error" in ws.receive_json()

        ws.send_text("[1, 2, 3]")  # valid JSON, wrong shape
        assert "error" in ws.receive_json()

        ws.send_text("ping")
        assert ws.receive_text() == "pong"

        ws.send_json({"lat": 26.85, "lng": 80.95})
        assert ws.receive_json()["status"] == "ok"


def test_redis_outage_does_not_kill_the_rider_socket(client, db, customer, rider, broken_redis):
    """A Redis blip must not end a live delivery's GPS stream."""
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as ws:
        ws.send_json({"lat": 26.85, "lng": 80.95})
        assert ws.receive_json()["status"] == "ok"

        ws.send_text("ping")
        assert ws.receive_text() == "pong"


# ─── GPS FAN-OUT ──────────────────────────────────────

def test_gps_reaches_the_watching_customer(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url(f"/ws/track/{order.id}", customer)) as watcher:
        with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as gps:
            gps.send_json({"lat": 26.8512, "lng": 80.9534})
            gps.receive_json()  # the rider's own ack

            update = watcher.receive_json()
            assert update["rider_lat"] == 26.8512
            assert update["rider_lng"] == 80.9534
            assert update["order_id"] == order.id


def test_gps_for_one_order_never_reaches_another_orders_watcher(client, db, customer, rider, other_rider):
    a = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    b = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, other_rider)

    with client.websocket_connect(ws_url(f"/ws/track/{b.id}", customer)) as watcher_b:
        with client.websocket_connect(ws_url(f"/ws/rider/{a.id}", rider)) as gps_a:
            gps_a.send_json({"lat": 26.10, "lng": 80.10})
            gps_a.receive_json()

        # Order B's watcher must see nothing from order A's rider.
        with client.websocket_connect(ws_url(f"/ws/rider/{b.id}", other_rider)) as gps_b:
            gps_b.send_json({"lat": 26.99, "lng": 80.99})
            gps_b.receive_json()
            assert watcher_b.receive_json()["rider_lat"] == 26.99


# ─── /ws/delivery/admin — THE FLEET STREAM ───────────

def test_admin_receives_a_snapshot_on_connect(client, db, admin, customer, rider):
    from app.services.delivery_tracking import update_rider_location

    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as ws:
        msg = ws.receive_json()

    assert msg["type"] == "snapshot"
    assert len(msg["deliveries"]) == 1
    assert msg["deliveries"][0]["order_id"] == order.id
    assert msg["deliveries"][0]["rider_name"] == "Rahul Rider"
    assert msg["deliveries"][0]["tracking_state"] == "live"


def test_customer_cannot_open_the_fleet_stream(client, customer):
    assert expect_rejected(client, ws_url("/ws/delivery/admin", customer)) == 4003


def test_rider_cannot_open_the_fleet_stream(client, rider):
    assert expect_rejected(client, ws_url("/ws/delivery/admin", rider)) == 4003


def test_baker_cannot_open_the_fleet_stream(client, baker):
    assert expect_rejected(client, ws_url("/ws/delivery/admin", baker)) == 4003


def test_unauthenticated_fleet_stream_is_rejected(client):
    assert expect_rejected(client, "/ws/delivery/admin") == 4001


def test_demoted_admin_loses_fleet_access_immediately(client, db, admin):
    """A token minted while admin must stop working the moment the role changes."""
    from app.models.user import UserRole

    url = ws_url("/ws/delivery/admin", admin)
    admin.role = UserRole.CUSTOMER
    db.commit()

    assert expect_rejected(client, url) == 4003


def test_admin_can_resync_on_demand(client, db, admin, customer, rider):
    """Backs the frontend's reconnect/refocus resynchronisation."""
    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as ws:
        assert ws.receive_json()["deliveries"] == []

        make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

        ws.send_text("resync")
        refreshed = ws.receive_json()

    assert refreshed["type"] == "snapshot"
    assert len(refreshed["deliveries"]) == 1


def test_admin_sees_rider_positions_live(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        fleet.receive_json()  # snapshot marks the order active

        with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as gps:
            gps.send_json({"lat": 26.8477, "lng": 80.9488})
            gps.receive_json()

            event = fleet.receive_json()

    assert event["type"] == "location_update"
    assert event["order_id"] == order.id
    assert event["rider_id"] == rider.id
    assert event["rider_name"] == "Rahul Rider"
    assert event["rider_lat"] == 26.8477


def test_admin_and_customer_both_receive_the_same_position(client, db, admin, customer, rider):
    """Admin visibility is an extra subscriber, not a replacement."""
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        fleet.receive_json()
        with client.websocket_connect(ws_url(f"/ws/track/{order.id}", customer)) as watcher:
            with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as gps:
                gps.send_json({"lat": 26.8600, "lng": 80.9700})
                gps.receive_json()

                customer_update = watcher.receive_json()
                admin_update = fleet.receive_json()

    assert customer_update["rider_lat"] == admin_update["rider_lat"] == 26.8600


def test_fleet_stream_ignores_positions_for_untracked_orders(client, db, admin, customer, rider):
    """
    PostgreSQL decides what is active. A rider socket that keeps pushing after a
    delivery ends must not put the order back on the dashboard.
    """
    from app.api.routes.websocket import fleet_manager
    from app.services.delivery_tracking import update_rider_location

    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        fleet.receive_json()

        # Simulate the order completing: the fleet stops tracking it.
        fleet_manager.untrack(order.id)
        update_rider_location(order.id, 26.87, 80.97)

        with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as gps:
            gps.send_json({"lat": 26.88, "lng": 80.98})
            gps.receive_json()

        # Nothing was forwarded — a resync still shows the real state.
        fleet.send_text("resync")
        assert fleet.receive_json()["type"] == "snapshot"


def test_packaging_an_order_notifies_the_admin_stream(client, db, admin, customer, rider):
    """
    A delivery that joins the fleet before it sets off must still appear without
    a manual refresh, even though it has no position to stream.
    """
    from app.models.order import OrderStatus as OS

    order = make_order(db, customer, OS.AWAITING_APPROVAL, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        assert fleet.receive_json()["deliveries"] == []

        res = client.patch(f"/orders/{order.id}/status", json={"status": "PACKAGED"},
                           headers={"Authorization": f"Bearer {token_for(admin)}"})
        assert res.status_code == 200

        event = fleet.receive_json()
        assert event["type"] == "fleet_changed"
        assert event["order_id"] == order.id

        # The resync the client would send then returns the complete row.
        fleet.send_text("resync")
        snapshot = fleet.receive_json()

    row = next(d for d in snapshot["deliveries"] if d["order_id"] == order.id)
    assert row["tracking_state"] == "assigned"
    assert row["rider_lat"] is None


def test_reassigning_a_packaged_order_notifies_the_admin_stream(client, db, admin, customer, rider, other_rider):
    from app.models.order import OrderStatus as OS

    order = make_order(db, customer, OS.PACKAGED, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        fleet.receive_json()  # snapshot

        res = client.post(f"/admin/orders/{order.id}/assign-rider",
                          json={"staff_id": other_rider.id},
                          headers={"Authorization": f"Bearer {token_for(admin)}"})
        assert res.status_code == 200

        assert fleet.receive_json()["type"] == "fleet_changed"


def test_reassigning_an_in_flight_order_drops_the_previous_position(client, db, admin, customer, rider, other_rider):
    from app.services.delivery_tracking import get_rider_location, update_rider_location

    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        fleet.receive_json()  # snapshot

        res = client.post(f"/admin/orders/{order.id}/assign-rider",
                          json={"staff_id": other_rider.id},
                          headers={"Authorization": f"Bearer {token_for(admin)}"})
        assert res.status_code == 200

        event = fleet.receive_json()

    assert event["type"] == "rider_reassigned"
    assert event["rider_id"] == other_rider.id
    assert event["previous_rider_id"] == rider.id
    # The new rider inherits nothing from the old one's route.
    assert get_rider_location(order.id) is None

    # And the previous rider can no longer push positions for it.
    assert expect_rejected(client, ws_url(f"/ws/rider/{order.id}", rider)) == 4003


def test_delivery_completion_reaches_the_admin_stream(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        fleet.receive_json()  # snapshot

        res = client.post(f"/orders/{order.id}/rider-delivered", headers={
            "Authorization": f"Bearer {token_for(rider)}"
        })
        assert res.status_code == 200

        event = fleet.receive_json()

    assert event["type"] == "delivery_completed"
    assert event["order_id"] == order.id


# ─── AUTHORISATION REGRESSIONS ────────────────────────

def test_customer_cannot_open_the_unscoped_order_feed(client, customer):
    """
    /ws/orders broadcasts every order's events to everyone connected, so a
    customer must not be able to join it.
    """
    assert expect_rejected(client, ws_url("/ws/orders", customer)) == 4003


def test_staff_can_open_the_order_feed(client, admin, baker, rider):
    for user in (admin, baker, rider):
        with client.websocket_connect(ws_url("/ws/orders", user)) as ws:
            ws.send_text("ping")
            assert ws.receive_text() == "pong"


def test_unauthenticated_order_feed_is_rejected(client):
    assert expect_rejected(client, "/ws/orders") == 4001


def test_reassignment_disconnects_the_previous_riders_gps_socket(client, db, admin, customer, rider, other_rider):
    """
    The old rider's device keeps reporting. If its socket stays open, the next
    ping re-creates the Redis keys the reassignment just cleared and re-attaches
    their position to an order that is no longer theirs.
    """
    from app.services.delivery_tracking import get_rider_location

    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url(f"/ws/rider/{order.id}", rider)) as gps:
        gps.send_json({"lat": 26.85, "lng": 80.95})
        assert gps.receive_json()["status"] == "ok"

        res = client.post(f"/admin/orders/{order.id}/assign-rider",
                          json={"staff_id": other_rider.id},
                          headers={"Authorization": f"Bearer {token_for(admin)}"})
        assert res.status_code == 200

        # The server closed the socket, so the old rider cannot push again.
        # Which exception surfaces depends on whether the close is observed on
        # the send or the receive, so accept either.
        with pytest.raises((WebSocketDisconnect, RuntimeError)):
            for _ in range(5):
                gps.send_json({"lat": 26.86, "lng": 80.96})
                gps.receive_json()

    assert get_rider_location(order.id) is None


def test_completed_order_is_not_resurrected_by_a_later_snapshot(client, db, admin, customer, rider):
    """
    A snapshot reads the database in a worker thread, so its rows can already be
    stale by the time they are sent. A delivery completed in that window must
    not come back onto the map.
    """
    from app.api.routes.websocket import fleet_manager

    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    with client.websocket_connect(ws_url("/ws/delivery/admin", admin)) as fleet:
        assert len(fleet.receive_json()["deliveries"]) == 1

        # Mark it completed the way the lifecycle does, without touching the DB,
        # to reproduce exactly the stale-read window.
        fleet_manager.untrack(order.id)

        fleet.send_text("resync")
        snapshot = fleet.receive_json()

    assert order.id not in {d["order_id"] for d in snapshot["deliveries"]}
    assert order.id not in fleet_manager.active_orders
