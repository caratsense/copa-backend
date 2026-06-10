"""
WebSocket Endpoints
====================

1. ws://localhost:8000/ws/orders?token=<jwt>
   - All users: live order status updates
   - Admin: broadcasts for all orders

2. ws://localhost:8000/ws/rider/{order_id}?token=<jwt>
   - Rider sends GPS every 5-10 seconds
   - Only riders/admins can connect

3. ws://localhost:8000/ws/track/{order_id}?token=<jwt>
   - Customer receives rider's live GPS position + ETA
   - Auto-refreshes every 5 seconds from Redis

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
"""

import json
import asyncio
from typing import List, Dict, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import jwt, JWTError
from app.config import get_settings
from app.services.delivery_tracking import (
    update_rider_location,
    get_rider_location,
)

router = APIRouter(tags=["WebSocket"])
settings = get_settings()


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
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_to_admins(self, message: dict):
        dead = []
        for ws in self.admin_connections:
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
        for ws in self.watchers[order_id]:
            try:
                await ws.send_json(location_data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove_watcher(order_id, ws)


# Singletons
manager = ConnectionManager()
delivery_manager = DeliveryTrackingManager()


# ─── TOKEN VALIDATION ─────────────────────────────────

def _validate_ws_token(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


# ─── WS 1: ORDER STATUS UPDATES ──────────────────────

@router.websocket("/ws/orders")
async def websocket_orders(websocket: WebSocket, token: str = Query(None)):
    """Live order status updates for dashboard."""
    payload = _validate_ws_token(token)
    is_admin = payload.get("role") == "admin" if payload else False

    await manager.connect(websocket, is_admin=is_admin)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# ─── WS 2: RIDER SENDS GPS ───────────────────────────

@router.websocket("/ws/rider/{order_id}")
async def websocket_rider_gps(websocket: WebSocket, order_id: int, token: str = Query(None)):
    """
    Rider connects here and sends GPS coordinates.
    Message format: {"lat": 26.8467, "lng": 80.9462}

    The server:
    1. Stores position in Redis
    2. Calculates ETA
    3. Broadcasts to all customers watching this order
    """
    payload = _validate_ws_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Authentication required")
        return

    role = payload.get("role", "")
    if role not in ("rider", "admin"):
        await websocket.close(code=4003, reason="Only riders can send GPS")
        return

    await websocket.accept()

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
                lat = data.get("lat")
                lng = data.get("lng")

                if lat is None or lng is None:
                    await websocket.send_json({"error": "lat and lng required"})
                    continue

                # Validate coordinates
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    await websocket.send_json({"error": "Invalid coordinates"})
                    continue

                # Store in Redis + calculate ETA
                result = update_rider_location(order_id, lat, lng)

                # Acknowledge to rider
                await websocket.send_json({
                    "status": "ok",
                    "eta_minutes": result.get("eta_minutes"),
                })

                # Broadcast to all customers watching this order
                location_data = get_rider_location(order_id)
                if location_data:
                    await delivery_manager.broadcast_location(order_id, location_data)

            except json.JSONDecodeError:
                if raw == "ping":
                    await websocket.send_text("pong")
                else:
                    await websocket.send_json({"error": "Invalid JSON"})

    except WebSocketDisconnect:
        pass


# ─── WS 3: CUSTOMER WATCHES DELIVERY ─────────────────

@router.websocket("/ws/track/{order_id}")
async def websocket_track_delivery(websocket: WebSocket, order_id: int, token: str = Query(None)):
    """
    Customer connects here to watch rider's live location.
    Receives GPS updates + ETA whenever the rider sends a new position.
    Also sends the current position immediately on connect.
    """
    payload = _validate_ws_token(token)
    if not payload:
        await websocket.close(code=4001, reason="Authentication required")
        return

    await delivery_manager.add_watcher(order_id, websocket)

    # Send current position immediately on connect
    current = get_rider_location(order_id)
    if current:
        try:
            await websocket.send_json(current)
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        delivery_manager.remove_watcher(order_id, websocket)
