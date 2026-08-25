"""
WebSocket Endpoints
====================

1. ws://localhost:8000/ws/orders?token=<jwt>
   - Authenticated users: live order status updates
   - Admin: broadcasts for all orders

2. ws://localhost:8000/ws/rider/{order_id}?token=<jwt>
   - Rider sends GPS every 5-10 seconds
   - Only the rider that order is assigned to (or an admin) may connect

3. ws://localhost:8000/ws/track/{order_id}?token=<jwt>
   - Customer receives rider's live GPS position + ETA for their own order
   - Admins and the assigned rider may also watch

4. ws://localhost:8000/ws/delivery/admin?token=<jwt>
   - Admin only: one subscription for the whole fleet.
   - Sends a full snapshot on connect (so a reconnect resynchronises without a
     separate REST call), then incremental events.

AUTHORISATION
Every socket here is authorised server-side against the database, not against
claims in the token alone: a token minted before a user was demoted or
deactivated must not still grant admin fleet access, and an order id in the URL
is attacker-controlled. Who may observe a given delivery is decided by
`app.core.auth.can_observe_delivery`, shared with the REST location endpoint so
the two cannot answer differently.

Rejections close with 4001/4003, but note that uvicorn turns a close-before-
accept into an HTTP 403 handshake failure — a browser only ever sees close code
1006, so clients cannot distinguish "forbidden" from "network dropped" here.

FRONTEND USAGE:

Rider app (sending GPS):
    const ws = new WebSocket("ws://localhost:8000/ws/rider/42?token=<rider_jwt>");
    navigator.geolocation.watchPosition((pos) => {
        ws.send(JSON.stringify({
            lat: pos.coords.latitude,
            lng: pos.coords.longitude
        }));
    }, null, { enableHighAccuracy: true });

Customer app (watching delivery):
    const ws = new WebSocket("ws://localhost:8000/ws/track/42?token=<customer_jwt>");
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        // data = { rider_lat, rider_lng, eta_minutes, ... }
        updateMapMarker(data.rider_lat, data.rider_lng);
        updateETA(data.eta_minutes);
    };

Admin dashboard (watching the fleet):
    const ws = new WebSocket("ws://localhost:8000/ws/delivery/admin?token=<admin_jwt>");
    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        // msg.type = "snapshot" | "location_update" | "delivery_started"
        //          | "delivery_completed" | "rider_reassigned"
    };
"""

import json
import logging
from collections import OrderedDict
from typing import List, Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.encoders import jsonable_encoder
from jose import jwt, JWTError
from sqlalchemy.orm import Session, noload, selectinload
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.auth import can_observe_delivery
from app.db import SessionLocal
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services.delivery_tracking import (
    update_rider_location,
    get_rider_location,
)

router = APIRouter(tags=["WebSocket"])
settings = get_settings()
logger = logging.getLogger(__name__)

# A socket the *server* closes from elsewhere — the order lifecycle winding a
# delivery down — leaves this handler's pending `receive_text()` raising
# RuntimeError("WebSocket is not connected") rather than WebSocketDisconnect.
# Both mean the same thing here: stop reading and clean up.
DISCONNECTED = (WebSocketDisconnect, RuntimeError)

# Close codes used consistently across every socket here.
WS_UNAUTHENTICATED = 4001
WS_FORBIDDEN = 4003
WS_NOT_FOUND = 4004


# ─── CONNECTION MANAGERS ──────────────────────────────

class ConnectionManager:
    """Manages active WebSocket connections for order updates."""

    def __init__(self):
        self.active: List[WebSocket] = []
        self.admin_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket, is_admin: bool = False):
        await websocket.accept()
        self.active.append(websocket)
        if is_admin:
            self.admin_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)
        if websocket in self.admin_connections:
            self.admin_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Snapshot the list: a concurrent disconnect (or the dead-socket cleanup
        # below) mutates it, and an index-based iterator would then skip a
        # recipient entirely.
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_to_admins(self, message: dict):
        dead = []
        for ws in list(self.admin_connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class DeliveryTrackingManager:
    """Manages WebSocket connections for delivery tracking per order."""

    def __init__(self):
        # order_id → list of WebSocket connections watching that delivery
        self.watchers: Dict[int, List[WebSocket]] = {}

    async def add_watcher(self, order_id: int, websocket: WebSocket):
        await websocket.accept()
        if order_id not in self.watchers:
            self.watchers[order_id] = []
        self.watchers[order_id].append(websocket)

    def remove_watcher(self, order_id: int, websocket: WebSocket):
        if order_id in self.watchers:
            if websocket in self.watchers[order_id]:
                self.watchers[order_id].remove(websocket)
            if not self.watchers[order_id]:
                del self.watchers[order_id]

    async def broadcast_location(self, order_id: int, location_data: dict):
        """Send rider's GPS to all watchers of this order."""
        if order_id not in self.watchers:
            return
        dead = []
        for ws in list(self.watchers[order_id]):
            try:
                await ws.send_json(location_data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove_watcher(order_id, ws)

    async def close_order(self, order_id: int, message: Optional[dict] = None):
        """
        Tell everyone watching an order that it is over, then drop them.

        Without this a customer's map sits on the rider's last known point
        indefinitely after delivery, with no signal that tracking has ended.
        """
        for ws in list(self.watchers.get(order_id, [])):
            try:
                if message:
                    await ws.send_json(message)
                await ws.close()
            except Exception:
                pass
            self.remove_watcher(order_id, ws)


class FleetManager:
    """
    Admin fleet subscribers — one connection observes every active delivery.

    Deliberately flat: admins are few, and a per-order subscription model is
    exactly the fan-out problem this endpoint exists to remove. `active_orders`
    is the gate that keeps a delivered order from reappearing if a stale rider
    socket keeps pushing GPS.
    """

    # Completed orders remembered long enough to outlive an in-flight snapshot.
    # DELIVERED is terminal in VALID_TRANSITIONS, so an order that lands here
    # can never legitimately become active again.
    COMPLETED_MEMORY = 512

    def __init__(self):
        self.admins: List[WebSocket] = []
        self.active_orders: set[int] = set()
        self.completed_orders: OrderedDict[int, None] = OrderedDict()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.admins.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.admins:
            self.admins.remove(websocket)

    def track(self, order_id: int):
        self.active_orders.add(order_id)
        self.completed_orders.pop(order_id, None)

    def untrack(self, order_id: int):
        self.active_orders.discard(order_id)
        self.completed_orders[order_id] = None
        self.completed_orders.move_to_end(order_id)
        while len(self.completed_orders) > self.COMPLETED_MEMORY:
            self.completed_orders.popitem(last=False)

    def is_completed(self, order_id: int) -> bool:
        return order_id in self.completed_orders

    def sync_active(self, order_ids) -> None:
        """
        Reset the active set from a freshly built snapshot, minus anything that
        completed while that snapshot was being built.

        A snapshot reads the database in a worker thread; an order can be
        delivered during that hop, so the rows coming back may already be out of
        date. Without the tombstones a resync would put the delivered order back
        on every admin's map, where it would sit until the next refresh.
        """
        self.active_orders = {oid for oid in order_ids if not self.is_completed(oid)}

    async def broadcast(self, message: dict):
        if not self.admins:
            return
        payload = jsonable_encoder(message)
        dead = []
        for ws in list(self.admins):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_location(self, order_id: int, message: dict):
        """
        Forward a rider position, but only for an order we believe is active.

        PostgreSQL decides what is active; Redis and the rider socket do not get
        to resurrect a completed delivery on the dashboard.
        """
        if order_id not in self.active_orders:
            return
        await self.broadcast(message)


# Singletons
manager = ConnectionManager()
delivery_manager = DeliveryTrackingManager()
fleet_manager = FleetManager()


# ─── TOKEN VALIDATION ─────────────────────────────────

def _validate_ws_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


def _load_user(db: Session, payload: dict) -> Optional[User]:
    """
    Resolve the token's subject to a live, active user row.

    Role is read from the database rather than the token so that deactivating or
    demoting someone takes effect immediately, instead of when their JWT expires.
    """
    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        return None
    user = (
        db.query(User)
        .options(noload(User.orders))  # only id/role/is_active are needed here
        .filter(User.id == user_id)
        .first()
    )
    if not user or not user.is_active:
        return None
    return user


def _authorise_order_access(db: Session, user: User, order_id: int) -> Optional[Order]:
    """
    Load an order if this user may observe its delivery, else None.

    The rule itself lives in `app.core.auth.can_observe_delivery`, shared with
    the REST location endpoint so the two cannot answer differently.

    This is the check whose absence let any logged-in account watch any
    stranger's rider by editing the order id in the URL.
    """
    order = (
        db.query(Order)
        .options(
            # `rider` is read after the session closes, so it must be loaded
            # here. The rest of the graph is not used by any socket, and
            # `Order`'s relationships all default to lazy="selectin" — without
            # these the authorisation of a single connection pulls the
            # customer's whole order history and every order's items and events.
            selectinload(Order.rider),
            noload(Order.items),
            noload(Order.events),
            noload(Order.baker),
            noload(Order.user),
        )
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        return None
    return order if can_observe_delivery(user, order) else None


async def _reject(websocket: WebSocket, code: int, reason: str):
    """Close before accept — the handshake fails and no frames are exchanged."""
    await websocket.close(code=code, reason=reason)


async def _authorise_socket(token: Optional[str], order_id: Optional[int] = None):
    """
    Shared connect-time authorisation.

    Returns (user, order, error) where error is a (code, reason) pair. The
    database work runs in a thread: it is blocking, and a slow or unreachable
    database would otherwise stall every other socket on this worker for the
    duration of the connection attempt.
    """
    return await run_in_threadpool(_authorise_socket_blocking, token, order_id)


def _authorise_socket_blocking(token: Optional[str], order_id: Optional[int] = None):
    """
    The blocking half of `_authorise_socket`.

    The session is closed before the socket starts streaming — holding one open
    for the life of a long-lived connection would exhaust the pool.
    """
    payload = _validate_ws_token(token)
    if not payload:
        return None, None, (WS_UNAUTHENTICATED, "Authentication required")

    db = SessionLocal()
    try:
        user = _load_user(db, payload)
        if not user:
            return None, None, (WS_UNAUTHENTICATED, "Authentication required")

        if order_id is None:
            db.expunge_all()
            return user, None, None

        order = _authorise_order_access(db, user, order_id)
        if not order:
            # Deliberately indistinguishable from "no such order": telling an
            # attacker which order ids exist is itself a leak.
            return None, None, (WS_FORBIDDEN, "Not authorised for this delivery")

        # Detach everything at once so the loaded attributes stay readable after
        # the session closes. Expunging instance-by-instance is wrong here: when
        # a rider connects, `user` and `order.rider` are the same object.
        db.expunge_all()
        return user, order, None
    finally:
        db.close()


# ─── WS 1: ORDER STATUS UPDATES ──────────────────────

@router.websocket("/ws/orders")
async def websocket_orders(websocket: WebSocket, token: str = Query(None)):
    """
    Live order status updates for the internal dashboards.

    This feed is unscoped — `manager.broadcast` sends every order's events, with
    totals and status transitions, to everyone connected. That is fine for staff
    who can already see all orders through the admin and baker APIs, but it must
    not reach customers, who would otherwise receive every other customer's
    order activity. Restricted to staff until per-user scoping exists; no
    customer-facing client uses this endpoint.
    """
    user, _, error = await _authorise_socket(token)
    if error:
        await _reject(websocket, *error)
        return

    if user.role not in (UserRole.ADMIN, UserRole.BAKER, UserRole.RIDER):
        await _reject(websocket, WS_FORBIDDEN, "Staff access required")
        return

    await manager.connect(websocket, is_admin=user.role == UserRole.ADMIN)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except DISCONNECTED:
        pass
    finally:
        manager.disconnect(websocket)


# ─── WS 2: RIDER SENDS GPS ───────────────────────────

@router.websocket("/ws/rider/{order_id}")
async def websocket_rider_gps(websocket: WebSocket, order_id: int, token: str = Query(None)):
    """
    Rider connects here and sends GPS coordinates.
    Message format: {"lat": 26.8467, "lng": 80.9462}

    The server:
    1. Verifies this rider is the one the order is assigned to
    2. Stores position in Redis
    3. Calculates ETA
    4. Broadcasts to customers watching this order and to the admin fleet
    """
    user, order, error = await _authorise_socket(token, order_id)
    if error:
        await _reject(websocket, *error)
        return

    if user.role not in (UserRole.RIDER, UserRole.ADMIN):
        await _reject(websocket, WS_FORBIDDEN, "Only riders can send GPS")
        return

    # A finished delivery must not accept new positions — otherwise a rider whose
    # socket outlives the order can rewrite Redis keys that cleanup just removed.
    if order.status != OrderStatus.OUT_FOR_DELIVERY:
        await _reject(websocket, WS_FORBIDDEN, "Order is not out for delivery")
        return

    # Order/rider context is captured once here so the per-ping path stays free
    # of database queries no matter how fast the device reports.
    rider_name = order.rider.name if order.rider else None
    rider_id = order.assigned_rider_id

    await websocket.accept()
    socket_state = rider_sockets.add(order_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                if raw == "ping":
                    await websocket.send_text("pong")
                else:
                    await websocket.send_json({"error": "Invalid JSON"})
                continue

            if not isinstance(data, dict):
                await websocket.send_json({"error": "Expected a JSON object"})
                continue

            lat = data.get("lat")
            lng = data.get("lng")

            if lat is None or lng is None:
                await websocket.send_json({"error": "lat and lng required"})
                continue

            # Reject anything that is not a real finite number before it reaches
            # Redis: booleans, strings and NaN would all otherwise be stored and
            # then break every consumer of the stream.
            try:
                lat = float(lat)
                lng = float(lng)
            except (TypeError, ValueError):
                await websocket.send_json({"error": "Invalid coordinates"})
                continue

            if isinstance(data.get("lat"), bool) or isinstance(data.get("lng"), bool):
                await websocket.send_json({"error": "Invalid coordinates"})
                continue

            if lat != lat or lng != lng or abs(lat) == float("inf") or abs(lng) == float("inf"):
                await websocket.send_json({"error": "Invalid coordinates"})
                continue

            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                await websocket.send_json({"error": "Invalid coordinates"})
                continue

            # This delivery was handed to someone else (or completed) while the
            # frame was in flight. Writing it now would restore this rider's
            # position on an order that is no longer theirs.
            if socket_state["revoked"]:
                break

            # One bad ping, a Redis blip or a slow subscriber must not drop this
            # rider's socket — that would stop tracking for a live delivery.
            try:
                result = update_rider_location(order_id, lat, lng)

                await websocket.send_json({
                    "status": "ok",
                    "eta_minutes": result.get("eta_minutes"),
                })

                location_data = get_rider_location(order_id)
                if location_data:
                    # Customers watching this specific order (unchanged path).
                    await delivery_manager.broadcast_location(order_id, location_data)
                    # Admin fleet — the same Redis read, fanned out once more.
                    await fleet_manager.broadcast_location(order_id, {
                        "type": "location_update",
                        **location_data,
                        "rider_id": rider_id,
                        "rider_name": rider_name,
                        "tracking_state": "live",
                    })
            except DISCONNECTED:
                # The socket went away mid-fan-out (the rider hung up, or the
                # lifecycle closed it). Let the outer handler clean up rather
                # than logging it as a fan-out failure.
                raise
            except Exception as e:
                logger.error("[WS/rider] order %s: %s", order_id, e)

    except DISCONNECTED:
        pass
    finally:
        rider_sockets.remove(order_id, websocket)


# ─── WS 3: CUSTOMER WATCHES DELIVERY ─────────────────

@router.websocket("/ws/track/{order_id}")
async def websocket_track_delivery(websocket: WebSocket, order_id: int, token: str = Query(None)):
    """
    Watch a rider's live location for one order.
    Receives GPS updates + ETA whenever the rider sends a new position.
    Also sends the current position immediately on connect.

    Restricted to the customer who placed the order, the rider carrying it, and
    admins.
    """
    _, _, error = await _authorise_socket(token, order_id)
    if error:
        await _reject(websocket, *error)
        return

    await delivery_manager.add_watcher(order_id, websocket)

    # Send current position immediately on connect
    try:
        current = get_rider_location(order_id)
        if current:
            await websocket.send_json(current)
    except Exception as e:
        logger.error("[WS/track] initial position for order %s: %s", order_id, e)

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except DISCONNECTED:
        pass
    finally:
        delivery_manager.remove_watcher(order_id, websocket)


# ─── WS 4: ADMIN WATCHES THE WHOLE FLEET ─────────────

@router.websocket("/ws/delivery/admin")
async def websocket_fleet(websocket: WebSocket, token: str = Query(None)):
    """
    One admin subscription covering every active delivery.

    On connect the server pushes a `snapshot` event containing the current
    active deliveries and rider roster, so a client that drops and reconnects
    resynchronises from the socket itself with no extra request and no gap.
    """
    user, _, error = await _authorise_socket(token)
    if error:
        await _reject(websocket, *error)
        return

    if user.role != UserRole.ADMIN:
        await _reject(websocket, WS_FORBIDDEN, "Admin access required")
        return

    await fleet_manager.connect(websocket)

    try:
        await _send_snapshot(websocket)
    except Exception as e:
        logger.error("[WS/fleet] snapshot failed: %s", e)

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "resync":
                # Client-driven resynchronisation, e.g. on tab refocus.
                await _send_snapshot(websocket)
    except DISCONNECTED:
        pass
    finally:
        fleet_manager.disconnect(websocket)


async def _send_snapshot(websocket: WebSocket):
    """
    Build a snapshot off the event loop and send it.

    `build_snapshot` issues blocking database and Redis calls. Running them
    inline would stall every other socket on the worker for the duration, and
    `resync` is client-triggered — so the cost has to sit in a thread.
    """
    snapshot = await run_in_threadpool(_build_fleet_snapshot)

    # The database read above happened in another thread, so an order may have
    # been delivered while it was in flight. Drop anything the lifecycle has
    # since marked completed, otherwise this snapshot would resurrect it on
    # every admin's map.
    snapshot["deliveries"] = [
        d for d in snapshot["deliveries"] if not fleet_manager.is_completed(d["order_id"])
    ]
    fleet_manager.sync_active(
        d["order_id"] for d in snapshot["deliveries"]
        if d["order_status"] == OrderStatus.OUT_FOR_DELIVERY.value
    )
    await websocket.send_json(jsonable_encoder({"type": "snapshot", **snapshot}))


def _build_fleet_snapshot() -> dict:
    """Blocking snapshot build — always called via the threadpool."""
    from app.services.delivery_tracking import redis_available
    from app.services.fleet_tracking import build_snapshot

    db = SessionLocal()
    try:
        snapshot = build_snapshot(db)
    finally:
        db.close()
    return {**snapshot, "live_tracking_available": redis_available()}


# ─── RIDER SOCKET REGISTRY ───────────────────────────

class RiderSocketRegistry:
    """
    Rider GPS sockets by order, so the order lifecycle can shut them down.

    When a delivery completes or is handed to a different rider, the original
    rider's device may still hold an open socket and keep pushing positions.

    Closing alone is not enough: the close is dispatched onto the event loop
    from a request thread, so a ping already in flight can still be written
    after the reassignment cleared the Redis keys — restoring the previous
    rider's position under the new rider's name. Each connection therefore
    carries a small revocation flag that is set *before* the close, and which
    the GPS loop checks before every write. A new socket for the same order
    gets its own flag and is unaffected.
    """

    def __init__(self):
        self.sockets: Dict[int, List[tuple[WebSocket, dict]]] = {}

    def add(self, order_id: int, websocket: WebSocket) -> dict:
        """Register a socket and return its revocation flag."""
        state = {"revoked": False}
        self.sockets.setdefault(order_id, []).append((websocket, state))
        return state

    def remove(self, order_id: int, websocket: WebSocket):
        entries = self.sockets.get(order_id)
        if not entries:
            return
        self.sockets[order_id] = [e for e in entries if e[0] is not websocket]
        if not self.sockets[order_id]:
            del self.sockets[order_id]

    async def close_order(self, order_id: int):
        for ws, state in list(self.sockets.get(order_id, [])):
            # Revoke first: even if the close loses the race with an in-flight
            # frame, the handler will refuse to write it.
            state["revoked"] = True
            try:
                await ws.close()
            except Exception:
                pass
            self.remove(order_id, ws)


rider_sockets = RiderSocketRegistry()
