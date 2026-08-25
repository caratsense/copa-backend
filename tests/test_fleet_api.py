"""
Admin fleet snapshot — GET /delivery/admin/active

Covers who may call it, what it reports for each stage of a delivery, and how it
degrades when Redis is unavailable.
"""

from app.models.order import OrderStatus
from app.services.delivery_tracking import update_rider_location

from tests.conftest import auth, make_address, make_order, make_user


def _by_order(payload, order_id):
    return next(d for d in payload["deliveries"] if d["order_id"] == order_id)


# ─── AUTHORISATION ────────────────────────────────────

def test_admin_can_read_active_deliveries(client, admin):
    res = client.get("/delivery/admin/active", headers=auth(admin))
    assert res.status_code == 200
    assert res.json()["deliveries"] == []


def test_customer_cannot_read_the_fleet(client, customer):
    res = client.get("/delivery/admin/active", headers=auth(customer))
    assert res.status_code == 403


def test_rider_cannot_read_the_fleet(client, rider):
    assert client.get("/delivery/admin/active", headers=auth(rider)).status_code == 403


def test_baker_cannot_read_the_fleet(client, baker):
    assert client.get("/delivery/admin/active", headers=auth(baker)).status_code == 403


def test_unauthenticated_cannot_read_the_fleet(client):
    assert client.get("/delivery/admin/active").status_code == 401


# ─── SNAPSHOT CONTENT ─────────────────────────────────

def test_snapshot_contains_every_active_delivery(client, db, admin, customer, rider, other_rider):
    a = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    b = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, other_rider)
    update_rider_location(a.id, 26.85, 80.95)
    update_rider_location(b.id, 26.86, 80.96)

    payload = client.get("/delivery/admin/active", headers=auth(admin)).json()

    assert {d["order_id"] for d in payload["deliveries"]} == {a.id, b.id}
    assert _by_order(payload, a.id)["rider_name"] == "Rahul Rider"
    assert _by_order(payload, b.id)["rider_name"] == "Aman Rider"
    assert _by_order(payload, a.id)["customer_name"] == "Priya Customer"


def test_live_delivery_reports_its_position(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.8501, 80.9502)

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["tracking_state"] == "live"
    assert row["rider_lat"] == 26.8501
    assert row["rider_lng"] == 80.9502
    assert row["is_stale"] is False
    assert row["seconds_since_update"] < 5


def test_assigned_but_not_dispatched_has_no_position(client, db, admin, customer, rider):
    """A PACKAGED order's rider hasn't set off — it must not get a map pin."""
    order = make_order(db, customer, OrderStatus.PACKAGED, rider)

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["tracking_state"] == "assigned"
    assert row["rider_lat"] is None
    assert row["rider_lng"] is None
    assert row["is_stale"] is None


def test_dispatched_without_gps_is_awaiting_not_live(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["tracking_state"] == "awaiting_gps"
    assert row["rider_lat"] is None


def test_order_out_for_delivery_without_a_rider_is_flagged(client, db, admin, customer):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider=None)

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["tracking_state"] == "unassigned"
    assert row["rider_id"] is None


def test_stale_position_is_reported_as_stale(client, db, admin, customer, rider, fake_redis):
    import json

    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    # Backdate the stored fix well past the staleness threshold.
    from app.services.delivery_tracking import STALE_AFTER_SECONDS
    import time
    current = json.loads(fake_redis.get(f"delivery:{order.id}"))
    current["timestamp"] = time.time() - (STALE_AFTER_SECONDS + 60)
    fake_redis.set(f"delivery:{order.id}", json.dumps(current))

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["tracking_state"] == "stale"
    assert row["is_stale"] is True
    # The position is kept — stale means "old", not "discard it".
    assert row["rider_lat"] == 26.85


def test_delivered_order_leaves_the_fleet(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)
    assert len(client.get("/delivery/admin/active", headers=auth(admin)).json()["deliveries"]) == 1

    client.post(f"/orders/{order.id}/rider-delivered", headers=auth(rider))

    assert client.get("/delivery/admin/active", headers=auth(admin)).json()["deliveries"] == []


def test_deliveries_stay_independent(client, db, admin, customer, rider, other_rider):
    """Moving one rider must not disturb another's row."""
    a = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    b = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, other_rider)
    update_rider_location(a.id, 26.80, 80.90)
    update_rider_location(b.id, 26.90, 80.99)

    before = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), b.id)
    update_rider_location(a.id, 26.81, 80.91)
    after_payload = client.get("/delivery/admin/active", headers=auth(admin)).json()

    assert _by_order(after_payload, a.id)["rider_lat"] == 26.81
    assert _by_order(after_payload, b.id)["rider_lat"] == before["rider_lat"] == 26.90


def test_one_rider_can_carry_several_deliveries(client, db, admin, customer, rider):
    """Nothing in the schema forbids it, so the fleet view must handle it."""
    a = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    b = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(a.id, 26.85, 80.95)
    update_rider_location(b.id, 26.85, 80.95)

    payload = client.get("/delivery/admin/active", headers=auth(admin)).json()

    assert {d["order_id"] for d in payload["deliveries"]} == {a.id, b.id}
    assert all(d["rider_id"] == rider.id for d in payload["deliveries"])
    summary = next(r for r in payload["riders"] if r["rider_id"] == rider.id)
    assert summary["active_delivery_count"] == 2


def test_rider_roster_includes_idle_and_off_duty_riders(client, db, admin, customer, rider):
    from app.models.user import UserRole

    off_duty = make_user(db, "Off Duty Rider", UserRole.RIDER, on_duty=False)
    make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    riders = client.get("/delivery/admin/active", headers=auth(admin)).json()["riders"]
    by_id = {r["rider_id"]: r for r in riders}

    assert by_id[rider.id]["active_delivery_count"] == 1
    assert by_id[off_duty.id]["on_duty"] is False
    assert by_id[off_duty.id]["active_delivery_count"] == 0


def test_destination_resolved_from_the_customers_saved_address(client, db, admin, customer, rider):
    """Checkout composes "{flat}, {full_address}, {landmark}" — matched exactly."""
    make_address(db, customer, "Hazratganj, Lucknow", 26.8500, 80.9450,
                 flat_building="Flat 4B", landmark="near GPO")
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Flat 4B, Hazratganj, Lucknow, near GPO")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] == 26.8500
    assert row["dropoff_lng"] == 80.9450


def test_unmatched_address_yields_no_destination_rather_than_a_guess(client, db, admin, customer, rider):
    make_address(db, customer, "Gomti Nagar, Lucknow", 26.86, 81.00)
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Somewhere else entirely")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] is None
    assert row["dropoff_lng"] is None


def test_another_customers_address_is_never_used(client, db, admin, customer, other_customer, rider):
    """The address composes to exactly this order's text — but belongs to someone else."""
    make_address(db, other_customer, "Hazratganj, Lucknow", 26.85, 80.94,
                 flat_building="Flat 4B")
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Flat 4B, Hazratganj, Lucknow")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] is None


# ─── DEGRADED MODE ────────────────────────────────────

def test_redis_outage_degrades_instead_of_failing(client, db, admin, customer, rider, broken_redis):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)

    res = client.get("/delivery/admin/active", headers=auth(admin))

    assert res.status_code == 200
    payload = res.json()
    assert payload["live_tracking_available"] is False
    row = _by_order(payload, order.id)
    # The order is still listed with its DB facts; only the position is missing.
    assert row["rider_name"] == "Rahul Rider"
    assert row["rider_lat"] is None
    assert row["tracking_state"] == "awaiting_gps"


def test_snapshot_publishes_the_staleness_threshold(client, admin):
    from app.services.delivery_tracking import STALE_AFTER_SECONDS

    payload = client.get("/delivery/admin/active", headers=auth(admin)).json()
    assert payload["stale_after_seconds"] == STALE_AFTER_SECONDS


# ─── AUTHORISATION REGRESSIONS ────────────────────────
# Each of these covers a hole found while reviewing this change set.

def test_baker_cannot_read_another_orders_live_location(client, db, baker, customer, rider):
    """
    REST used to reject only mismatched *customers*, so any baker could read
    every order's rider position and the customer's drop-off coordinates.
    """
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    res = client.get(f"/delivery/{order.id}/location", headers=auth(baker))

    assert res.status_code == 403


def test_unassigned_rider_cannot_read_another_orders_live_location(client, db, customer, rider, other_rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    assert client.get(f"/delivery/{order.id}/location", headers=auth(other_rider)).status_code == 403


def test_assigned_rider_can_read_their_own_orders_location(client, db, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    res = client.get(f"/delivery/{order.id}/location", headers=auth(rider))

    assert res.status_code == 200
    assert res.json()["rider_lat"] == 26.85


def test_owner_and_admin_can_read_the_location(client, db, admin, customer, rider):
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)

    assert client.get(f"/delivery/{order.id}/location", headers=auth(customer)).status_code == 200
    assert client.get(f"/delivery/{order.id}/location", headers=auth(admin)).status_code == 200


def test_deactivated_admin_loses_api_access_immediately(client, db, admin, customer, rider):
    """
    A deactivated account kept full API access until its 24h token expired —
    including this endpoint's customer names, phones and addresses.
    """
    make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    headers = auth(admin)
    assert client.get("/delivery/admin/active", headers=headers).status_code == 200

    admin.is_active = False
    db.commit()

    assert client.get("/delivery/admin/active", headers=headers).status_code == 401


def test_a_different_building_on_the_same_road_is_not_matched(client, db, admin, customer, rider):
    """
    The case an exact match exists to prevent: a customer whose saved home is on
    a road, ordering to a free-text address on that same road, must not be given
    their home coordinates — that is a drop-off pin in the wrong place and an
    ETA computed to it.
    """
    make_address(db, customer, "Gomti Nagar, Lucknow", 26.8600, 81.0000)
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Flat 4, 45 Gomti Nagar, Lucknow, near park")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] is None


def test_two_saved_addresses_composing_identically_are_ambiguous(client, db, admin, customer, rider):
    """Same text, different coordinates — neither may be assumed."""
    make_address(db, customer, "Hazratganj, Lucknow", 26.8600, 81.0000)
    make_address(db, customer, "Hazratganj, Lucknow", 26.8700, 81.0100)
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Hazratganj, Lucknow")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] is None


def test_duplicate_saved_address_at_the_same_point_still_resolves(client, db, admin, customer, rider):
    """A duplicated row is not a conflict when both agree on the location."""
    make_address(db, customer, "Hazratganj, Lucknow", 26.8600, 81.0000)
    make_address(db, customer, "Hazratganj, Lucknow", 26.8600, 81.0000)
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Hazratganj, Lucknow")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] == 26.8600


def test_a_generic_saved_address_is_never_stamped_on_other_orders(client, db, admin, customer, rider):
    """A customer who saved just "Lucknow" must not have it stamped on every order."""
    make_address(db, customer, "Lucknow", 26.8000, 80.9000)
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="Flat 9, Some Road, Lucknow")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] is None


def test_whitespace_and_case_differences_do_not_break_the_match(client, db, admin, customer, rider):
    """Formatting drift between the saved row and the order text is not a mismatch."""
    make_address(db, customer, "Hazratganj,  Lucknow", 26.8520, 80.9490, flat_building="Flat 3A")
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider,
                       address="flat 3a, Hazratganj, Lucknow")

    row = _by_order(client.get("/delivery/admin/active", headers=auth(admin)).json(), order.id)

    assert row["dropoff_lat"] == 26.8520
