"""
Fleet Tracking Service
=======================
Builds the admin's operational view of every delivery currently in flight.

The split that the rest of the tracking system already relies on is preserved
here deliberately:

  PostgreSQL  → which orders are active, who is assigned, who the customer is.
  Redis       → where the rider physically is right now.

So the active set is always derived from a DB query and then *decorated* with
whatever Redis happens to hold. Redis is never scanned for the active set. That
ordering is what makes a delivered order impossible to resurrect on the map: a
stray `delivery:{id}` key for an order that PostgreSQL says is DELIVERED is
simply never looked up.
"""

from typing import Optional

from sqlalchemy.orm import Session, noload, selectinload

from app.config import get_settings
from app.models.address import Address
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services.delivery_tracking import STALE_AFTER_SECONDS, get_locations_bulk, is_stale

settings = get_settings()

# Orders a rider is responsible for right now. PACKAGED is included so the
# dashboard can show "assigned, not yet collected" honestly instead of the order
# silently appearing only once GPS starts.
FLEET_STATUSES = [OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY]


def get_active_deliveries(db: Session) -> list[dict]:
    """
    Every delivery an admin should currently be watching, newest assignment last.

    One DB query for the orders (with customer + rider eager-loaded) and one
    pipelined Redis round trip for all positions — not one of each per order.
    """
    orders: list[Order] = (
        db.query(Order)
        .options(
            selectinload(Order.user),
            selectinload(Order.rider),
            # Every Order relationship defaults to lazy="selectin", so without
            # these the snapshot also pulls each order's items, events and baker
            # — none of which a fleet row uses.
            noload(Order.items),
            noload(Order.events),
            noload(Order.baker),
        )
        .filter(Order.status.in_(FLEET_STATUSES))
        .order_by(Order.delivery_time.asc().nullslast(), Order.created_at.asc())
        .all()
    )
    if not orders:
        return []

    # Only OUT_FOR_DELIVERY orders can have a live position — a PACKAGED order's
    # rider has not started, so asking Redis about it would always miss.
    tracked_ids = [o.id for o in orders if o.status == OrderStatus.OUT_FOR_DELIVERY]
    locations = get_locations_bulk(tracked_ids)

    dropoffs = _resolve_dropoffs(db, orders)

    return [_serialise(o, locations.get(o.id), dropoffs.get(o.id)) for o in orders]


def get_rider_summary(db: Session) -> list[dict]:
    """
    All riders and how many active deliveries each is carrying.

    Lets the dashboard distinguish "no rider is out" from "riders are out but
    none is transmitting", and surfaces off-duty riders truthfully rather than
    hiding them.
    """
    riders: list[User] = (
        db.query(User)
        .filter(User.role == UserRole.RIDER)
        .order_by(User.name)
        .all()
    )
    if not riders:
        return []

    counts: dict[int, int] = {}
    rows = (
        db.query(Order.assigned_rider_id, Order.id)
        .filter(
            Order.status.in_(FLEET_STATUSES),
            Order.assigned_rider_id.isnot(None),
        )
        .all()
    )
    for rider_id, _ in rows:
        counts[rider_id] = counts.get(rider_id, 0) + 1

    return [
        {
            "rider_id": r.id,
            "rider_name": r.name,
            "phone": r.phone,
            "is_active": bool(r.is_active),
            "on_duty": bool(r.on_duty),
            "active_delivery_count": counts.get(r.id, 0),
        }
        for r in riders
    ]


def build_snapshot(db: Session) -> dict:
    """The complete initial state an admin dashboard needs on load or reconnect."""
    return {
        "deliveries": get_active_deliveries(db),
        "riders": get_rider_summary(db),
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }


# ─── INTERNALS ────────────────────────────────────────

def _serialise(order: Order, location: Optional[dict], dropoff: Optional[tuple]) -> dict:
    """One fleet row: the DB facts, plus live position when we genuinely have one."""
    customer = order.user
    rider = order.rider

    # Redis meta carries the dropoff captured when tracking started; fall back to
    # the customer's saved address coordinates. Never a placeholder.
    dropoff_lat = (location or {}).get("dropoff_lat")
    dropoff_lng = (location or {}).get("dropoff_lng")
    if (dropoff_lat is None or dropoff_lng is None) and dropoff:
        dropoff_lat, dropoff_lng = dropoff

    return {
        "order_id": order.id,
        "order_status": order.status.value if hasattr(order.status, "value") else order.status,
        "tracking_state": _tracking_state(order, location),
        "rider_id": order.assigned_rider_id,
        "rider_name": rider.name if rider else None,
        "rider_phone": rider.phone if rider else None,
        "rider_on_duty": bool(rider.on_duty) if rider else None,
        "customer_name": customer.name if customer else None,
        "customer_phone": customer.phone if customer else None,
        "delivery_address": order.delivery_address,
        "delivery_time": order.delivery_time,
        "total_price": order.total_price,
        "payment_method": order.payment_method,
        "payment_status": (
            order.payment_status.value
            if hasattr(order.payment_status, "value")
            else order.payment_status
        ),
        "dropoff_lat": dropoff_lat,
        "dropoff_lng": dropoff_lng,
        **_position_fields(location),
    }


def _position_fields(location: Optional[dict]) -> dict:
    """The live-position half of a fleet row — all None when there is no fix."""
    loc = location or {}
    age = loc.get("seconds_since_update")
    return {
        "rider_lat": loc.get("rider_lat"),
        "rider_lng": loc.get("rider_lng"),
        "eta_minutes": loc.get("eta_minutes"),
        "updated_at": loc.get("updated_at"),
        "seconds_since_update": age,
        "is_stale": is_stale(age) if loc.get("rider_lat") is not None else None,
    }


def _tracking_state(order: Order, location: Optional[dict]) -> str:
    """
    Why this delivery looks the way it does — the states the UI renders.

    unassigned   : out for delivery but nobody is assigned (an ops problem)
    assigned     : packaged and assigned; the rider has not set off, so by design
                   there is no position to show
    awaiting_gps : out for delivery, but no fix has arrived yet
    stale        : we have a position, but it is older than the live threshold
    live         : transmitting now
    """
    if order.assigned_rider_id is None:
        return "unassigned"
    if order.status != OrderStatus.OUT_FOR_DELIVERY:
        return "assigned"
    if not location or location.get("rider_lat") is None:
        return "awaiting_gps"
    return "stale" if is_stale(location.get("seconds_since_update")) else "live"


def _resolve_dropoffs(db: Session, orders: list[Order]) -> dict[int, tuple]:
    """
    Destination coordinates for a batch of orders, or nothing.

    `Order.delivery_address` is a flattened string with no foreign key back to
    the Address row, so the match is made by reproducing exactly what checkout
    composes (see `_composed_address`) and comparing the whole string. Orders
    that do not reproduce exactly, and orders where two saved addresses
    reproduce the same text at different coordinates, get no coordinates at all
    rather than a guess.

    Scoped to the ordering customer's own addresses throughout.
    """
    wanted = [o for o in orders if o.delivery_address and o.user_id]
    if not wanted:
        return {}

    user_ids = {o.user_id for o in wanted}
    rows: list[Address] = (
        db.query(Address)
        .filter(
            Address.user_id.in_(user_ids),
            Address.latitude.isnot(None),
            Address.longitude.isnot(None),
        )
        .all()
    )
    if not rows:
        return {}

    by_user: dict[int, list[Address]] = {}
    for a in rows:
        by_user.setdefault(a.user_id, []).append(a)

    out: dict[int, tuple] = {}
    for order in wanted:
        text = _normalise(order.delivery_address)
        matches = {
            (addr.latitude, addr.longitude)
            for addr in by_user.get(order.user_id, [])
            if _composed_address(addr) == text
        }
        # Exactly one saved address reproduces this order's text. Two saved
        # addresses that compose identically but sit at different coordinates
        # are genuinely ambiguous, so neither is used.
        if len(matches) == 1:
            out[order.id] = matches.pop()

    return out


def _normalise(value: Optional[str]) -> str:
    return " ".join((value or "").split()).strip().lower()


def _composed_address(addr: Address) -> str:
    """
    Reproduce the string checkout writes into `Order.delivery_address`.

    Checkout joins the saved address's parts as
    "{flat_building}, {full_address}, {landmark}" (skipping blanks), and the
    order keeps no foreign key back to the row. Rebuilding that exact string and
    requiring an exact match is what makes this safe: a substring test cannot
    tell "this *is* the saved address" from "this is a different building on the
    same road", and would hand a customer's home coordinates to an order going
    to their office — a drop-off pin in the wrong place and an ETA computed to
    it. An order typed as free text simply resolves to no coordinates, which is
    the honest answer.
    """
    parts = [addr.flat_building, addr.full_address, addr.landmark]
    return _normalise(", ".join(p for p in parts if p and p.strip()))


def resolve_order_coordinates(db: Session, order: Order) -> tuple:
    """
    (pickup_lat, pickup_lng, dropoff_lat, dropoff_lng) for starting tracking.

    Pickup is the bakery, configured once rather than hardcoded at each call
    site. Dropoff may legitimately be (None, None) — an order placed with a
    free-text address has no coordinates anywhere in the system, and inventing
    them would produce a confident but fictional ETA.
    """
    dropoff = _resolve_dropoffs(db, [order]).get(order.id) or (None, None)
    return (settings.BAKERY_LAT, settings.BAKERY_LNG, dropoff[0], dropoff[1])
