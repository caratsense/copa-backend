"""
Delivery tracking service — Redis primitives, ETA, staleness and lifecycle.

These sit below the HTTP layer: they check the storage contract the fleet view
and the customer map both depend on.
"""

import json
import time

from app.models.order import OrderStatus
from app.services import delivery_tracking as dt
from app.services.delivery_tracking import (
    STALE_AFTER_SECONDS,
    get_locations_bulk,
    get_rider_location,
    is_stale,
    redis_available,
    set_tracking_rider,
    start_delivery_tracking,
    stop_delivery_tracking,
    update_rider_location,
)

from tests.conftest import auth, make_order


# ─── BULK READS ───────────────────────────────────────

def test_bulk_read_returns_only_orders_that_have_a_position():
    update_rider_location(1, 26.85, 80.95)
    update_rider_location(3, 26.87, 80.97)

    result = get_locations_bulk([1, 2, 3])

    assert set(result) == {1, 3}
    assert result[1]["rider_lat"] == 26.85
    assert result[3]["rider_lat"] == 26.87


def test_bulk_read_of_nothing_costs_nothing():
    assert get_locations_bulk([]) == {}


def test_bulk_read_tolerates_duplicate_ids():
    update_rider_location(7, 26.85, 80.95)
    assert set(get_locations_bulk([7, 7, 7])) == {7}


def test_bulk_read_matches_single_read(fake_redis):
    start_delivery_tracking(9, rider_id=2, dropoff_lat=26.90, dropoff_lng=81.00)
    update_rider_location(9, 26.85, 80.95)

    single = get_rider_location(9)
    bulk = get_locations_bulk([9])[9]

    for field in ("rider_lat", "rider_lng", "dropoff_lat", "dropoff_lng", "eta_minutes"):
        assert single[field] == bulk[field], field


def test_one_corrupt_payload_does_not_blank_the_fleet(fake_redis):
    update_rider_location(1, 26.85, 80.95)
    fake_redis.set("delivery:2", "{{{ not json")
    update_rider_location(3, 26.87, 80.97)

    result = get_locations_bulk([1, 2, 3])

    # The bad entry is skipped; its neighbours are unaffected.
    assert set(result) == {1, 3}


# ─── STALENESS ────────────────────────────────────────

def test_fresh_position_is_not_stale():
    update_rider_location(1, 26.85, 80.95)
    assert get_rider_location(1)["seconds_since_update"] < 5
    assert is_stale(get_rider_location(1)["seconds_since_update"]) is False


def test_old_position_is_stale(fake_redis):
    update_rider_location(1, 26.85, 80.95)
    current = json.loads(fake_redis.get("delivery:1"))
    current["timestamp"] = time.time() - (STALE_AFTER_SECONDS + 1)
    fake_redis.set("delivery:1", json.dumps(current))

    assert is_stale(get_rider_location(1)["seconds_since_update"]) is True


def test_missing_timestamp_counts_as_stale():
    assert is_stale(None) is True


def test_threshold_boundary_is_inclusive():
    assert is_stale(STALE_AFTER_SECONDS - 0.1) is False
    assert is_stale(STALE_AFTER_SECONDS) is True


def test_clock_skew_does_not_produce_a_negative_age(fake_redis):
    update_rider_location(1, 26.85, 80.95)
    current = json.loads(fake_redis.get("delivery:1"))
    current["timestamp"] = time.time() + 30  # a fix from the "future"
    fake_redis.set("delivery:1", json.dumps(current))

    assert get_rider_location(1)["seconds_since_update"] == 0.0


# ─── ETA ──────────────────────────────────────────────

def test_no_destination_means_no_eta():
    """Better an absent ETA than a confidently wrong one."""
    start_delivery_tracking(1, rider_id=2)  # no dropoff resolvable
    update_rider_location(1, 26.85, 80.95)

    assert get_rider_location(1)["eta_minutes"] is None


def test_eta_is_computed_once_a_destination_is_known():
    start_delivery_tracking(1, rider_id=2, dropoff_lat=26.95, dropoff_lng=80.95)
    update_rider_location(1, 26.85, 80.95)  # ~11km away

    eta = get_rider_location(1)["eta_minutes"]

    assert eta is not None and eta > 0


def test_eta_shrinks_as_the_rider_closes_in():
    start_delivery_tracking(1, rider_id=2, dropoff_lat=26.95, dropoff_lng=80.95)
    update_rider_location(1, 26.85, 80.95)
    far = get_rider_location(1)["eta_minutes"]

    update_rider_location(1, 26.94, 80.95)
    near = get_rider_location(1)["eta_minutes"]

    assert near < far


def test_gps_history_stays_capped(fake_redis):
    for i in range(30):
        update_rider_location(1, 26.85 + i * 0.001, 80.95)

    assert fake_redis.llen("delivery:1:history") == dt.SPEED_HISTORY_SIZE


# ─── LIFECYCLE ────────────────────────────────────────

def test_stop_clears_the_live_position(fake_redis):
    start_delivery_tracking(1, rider_id=2, dropoff_lat=26.95, dropoff_lng=80.95)
    update_rider_location(1, 26.85, 80.95)

    stop_delivery_tracking(1)

    assert get_rider_location(1) is None
    assert fake_redis.exists("delivery:1:history") == 0
    # Metadata survives briefly, marked completed, for post-delivery lookups.
    assert json.loads(fake_redis.get("delivery:1:meta"))["status"] == "completed"


def test_stop_is_idempotent():
    update_rider_location(1, 26.85, 80.95)
    stop_delivery_tracking(1)
    stop_delivery_tracking(1)  # must not raise
    assert get_rider_location(1) is None


def test_reassignment_repoints_tracking_and_drops_the_old_route(fake_redis):
    start_delivery_tracking(1, rider_id=2, dropoff_lat=26.95, dropoff_lng=80.95)
    update_rider_location(1, 26.85, 80.95)

    set_tracking_rider(1, rider_id=8)

    meta = json.loads(fake_redis.get("delivery:1:meta"))
    assert meta["rider_id"] == 8
    # The previous rider's position and history are not inherited.
    assert get_rider_location(1) is None
    assert fake_redis.exists("delivery:1:history") == 0
    # The destination survives — it belongs to the order, not the rider.
    assert meta["dropoff_lat"] == 26.95


def test_reassignment_clears_position_even_without_metadata(fake_redis):
    """
    Metadata can be absent — evicted, expired, or a delivery that predates
    automatic tracking. The previous rider's fix must still be dropped.
    """
    update_rider_location(1, 26.85, 80.95)
    assert fake_redis.exists("delivery:1:meta") == 0

    set_tracking_rider(1, rider_id=8)

    assert get_rider_location(1) is None
    assert fake_redis.exists("delivery:1:history") == 0


def test_reassignment_to_the_same_rider_is_a_no_op(fake_redis):
    start_delivery_tracking(1, rider_id=2, dropoff_lat=26.95, dropoff_lng=80.95)
    update_rider_location(1, 26.85, 80.95)

    set_tracking_rider(1, rider_id=2)

    assert get_rider_location(1)["rider_lat"] == 26.85


# ─── REDIS OUTAGE ─────────────────────────────────────

def test_every_read_returns_empty_when_redis_is_down(broken_redis):
    assert get_rider_location(1) is None
    assert get_locations_bulk([1, 2, 3]) == {}
    assert redis_available() is False


def test_writes_report_failure_instead_of_raising(broken_redis):
    assert "error" in update_rider_location(1, 26.85, 80.95)
    assert "error" in start_delivery_tracking(1, rider_id=2)
    stop_delivery_tracking(1)      # must not raise
    set_tracking_rider(1, 3)       # must not raise


def test_redis_available_is_true_when_healthy():
    assert redis_available() is True


# ─── LIFECYCLE INTEGRATION ────────────────────────────

def test_going_out_for_delivery_starts_tracking(client, db, admin, customer, rider, fake_redis):
    """
    The bakery-side fix: tracking metadata used to depend on a client call that
    nothing made, so ETA was permanently null.
    """
    from tests.conftest import make_address

    make_address(db, customer, "Hazratganj, Lucknow", 26.8500, 80.9450,
                 flat_building="Flat 4B")
    order = make_order(db, customer, OrderStatus.PACKAGED, rider,
                       address="Flat 4B, Hazratganj, Lucknow")

    res = client.patch(f"/orders/{order.id}/status",
                       json={"status": "OUT_FOR_DELIVERY"}, headers=auth(admin))
    assert res.status_code == 200

    meta = json.loads(fake_redis.get(f"delivery:{order.id}:meta"))
    assert meta["rider_id"] == rider.id
    assert meta["dropoff_lat"] == 26.8500
    assert meta["status"] == "active"


def test_eta_works_end_to_end_after_dispatch(client, db, admin, customer, rider):
    from tests.conftest import make_address

    make_address(db, customer, "Gomti Nagar, Lucknow", 26.8600, 81.0000,
                 flat_building="Flat 9")
    order = make_order(db, customer, OrderStatus.PACKAGED, rider,
                       address="Flat 9, Gomti Nagar, Lucknow")
    client.patch(f"/orders/{order.id}/status",
                 json={"status": "OUT_FOR_DELIVERY"}, headers=auth(admin))

    update_rider_location(order.id, 26.8467, 80.9462)

    assert get_rider_location(order.id)["eta_minutes"] > 0


def test_admin_marking_delivered_also_cleans_up_redis(client, db, admin, customer, rider, fake_redis):
    """
    Cleanup used to live only in the rider's own route, so an admin closing an
    order out left tracking keys alive for the full 24h TTL.
    """
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)
    assert fake_redis.exists(f"delivery:{order.id}") == 1

    client.patch(f"/orders/{order.id}/status", json={"status": "DELIVERED"}, headers=auth(admin))

    assert fake_redis.exists(f"delivery:{order.id}") == 0


def test_dispatch_without_resolvable_coordinates_still_tracks(client, db, admin, customer, rider, fake_redis):
    """A free-text address has no coordinates anywhere — tracking must still work."""
    order = make_order(db, customer, OrderStatus.PACKAGED, rider, address="behind the big temple")

    client.patch(f"/orders/{order.id}/status",
                 json={"status": "OUT_FOR_DELIVERY"}, headers=auth(admin))

    meta = json.loads(fake_redis.get(f"delivery:{order.id}:meta"))
    assert meta["dropoff_lat"] is None
    update_rider_location(order.id, 26.85, 80.95)
    assert get_rider_location(order.id)["rider_lat"] == 26.85


def test_dispatch_survives_a_redis_outage(client, db, admin, customer, rider, broken_redis):
    """A tracking failure must not block the order's status transition."""
    order = make_order(db, customer, OrderStatus.PACKAGED, rider)

    res = client.patch(f"/orders/{order.id}/status",
                       json={"status": "OUT_FOR_DELIVERY"}, headers=auth(admin))

    assert res.status_code == 200
    assert res.json()["status"] == "OUT_FOR_DELIVERY"
