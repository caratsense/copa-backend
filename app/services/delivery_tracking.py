"""
Delivery Tracking Service
==========================
Uses Redis for real-time GPS storage (no DB writes for every ping).
GPS data flows: Rider phone → WebSocket → Redis → Customer WebSocket

Architecture:
- Redis key per active delivery: "delivery:{order_id}" → JSON with lat, lng, timestamp
- Redis key for delivery metadata: "delivery:{order_id}:meta" → pickup/dropoff coords, start time
- Data expires after 24 hours automatically (TTL)

ETA calculation:
- Haversine distance from rider's current position to dropoff
- Average speed derived from last N GPS points
- Fallback to zone's estimated_time if not enough data
"""

import json
import math
import time
from datetime import datetime, timezone
from typing import Optional

import redis

from app.config import get_settings

settings = get_settings()

DELIVERY_TTL = 86400  # 24 hours
SPEED_HISTORY_SIZE = 10  # keep last N points for speed calculation


def _get_redis():
    try:
        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two GPS points in kilometers."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def start_delivery_tracking(
    order_id: int,
    rider_id: int,
    pickup_lat: float,
    pickup_lng: float,
    dropoff_lat: float,
    dropoff_lng: float,
) -> dict:
    """Initialize tracking for a delivery. Called when order goes OUT_FOR_DELIVERY."""
    r = _get_redis()
    if not r:
        return {"error": "Redis not available"}

    meta = {
        "order_id": order_id,
        "rider_id": rider_id,
        "pickup_lat": pickup_lat,
        "pickup_lng": pickup_lng,
        "dropoff_lat": dropoff_lat,
        "dropoff_lng": dropoff_lng,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
    }

    r.setex(f"delivery:{order_id}:meta", DELIVERY_TTL, json.dumps(meta))
    r.delete(f"delivery:{order_id}:history")
    return meta


def update_rider_location(
    order_id: int,
    lat: float,
    lng: float,
) -> dict:
    """
    Called every 5-10 seconds from rider's WebSocket.
    Stores current position + appends to history for speed calculation.
    """
    r = _get_redis()
    if not r:
        return {"error": "Redis not available"}

    now = time.time()

    # Store current position
    current = {
        "lat": lat,
        "lng": lng,
        "timestamp": now,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    r.setex(f"delivery:{order_id}", DELIVERY_TTL, json.dumps(current))

    # Append to history (capped list for speed calculation)
    r.rpush(f"delivery:{order_id}:history", json.dumps({"lat": lat, "lng": lng, "t": now}))
    r.ltrim(f"delivery:{order_id}:history", -SPEED_HISTORY_SIZE, -1)
    r.expire(f"delivery:{order_id}:history", DELIVERY_TTL)

    # Calculate ETA
    eta = _calculate_eta(r, order_id, lat, lng)

    return {**current, "eta_minutes": eta}


def get_rider_location(order_id: int) -> Optional[dict]:
    """Get current rider position + ETA for a delivery."""
    r = _get_redis()
    if not r:
        return None

    current_raw = r.get(f"delivery:{order_id}")
    meta_raw = r.get(f"delivery:{order_id}:meta")

    if not current_raw:
        return None

    current = json.loads(current_raw)
    meta = json.loads(meta_raw) if meta_raw else {}

    eta = _calculate_eta(r, order_id, current["lat"], current["lng"])

    return {
        "order_id": order_id,
        "rider_lat": current["lat"],
        "rider_lng": current["lng"],
        "updated_at": current.get("updated_at"),
        "dropoff_lat": meta.get("dropoff_lat"),
        "dropoff_lng": meta.get("dropoff_lng"),
        "pickup_lat": meta.get("pickup_lat"),
        "pickup_lng": meta.get("pickup_lng"),
        "eta_minutes": eta,
        "status": meta.get("status", "unknown"),
    }


def stop_delivery_tracking(order_id: int):
    """Called when order is DELIVERED — cleans up Redis keys."""
    r = _get_redis()
    if not r:
        return

    # Update meta status
    meta_raw = r.get(f"delivery:{order_id}:meta")
    if meta_raw:
        meta = json.loads(meta_raw)
        meta["status"] = "completed"
        meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        r.setex(f"delivery:{order_id}:meta", 3600, json.dumps(meta))  # keep for 1hr after delivery

    r.delete(f"delivery:{order_id}")
    r.delete(f"delivery:{order_id}:history")


def _calculate_eta(r, order_id: int, current_lat: float, current_lng: float) -> Optional[float]:
    """
    Calculate ETA in minutes based on:
    1. Distance to dropoff (haversine)
    2. Average speed from recent GPS history
    3. Fallback: assume 20 km/h city speed
    """
    meta_raw = r.get(f"delivery:{order_id}:meta")
    if not meta_raw:
        return None

    meta = json.loads(meta_raw)
    dropoff_lat = meta.get("dropoff_lat")
    dropoff_lng = meta.get("dropoff_lng")

    if not dropoff_lat or not dropoff_lng:
        return None

    distance_km = _haversine_km(current_lat, current_lng, dropoff_lat, dropoff_lng)

    # Calculate average speed from history
    history_raw = r.lrange(f"delivery:{order_id}:history", 0, -1)
    avg_speed_kmh = 20.0  # default city speed

    if len(history_raw) >= 3:
        points = [json.loads(p) for p in history_raw]
        total_dist = 0.0
        total_time = 0.0
        for i in range(1, len(points)):
            d = _haversine_km(points[i-1]["lat"], points[i-1]["lng"],
                              points[i]["lat"], points[i]["lng"])
            t = points[i]["t"] - points[i-1]["t"]
            if t > 0:
                total_dist += d
                total_time += t

        if total_time > 0:
            calculated_speed = (total_dist / total_time) * 3600  # km/h
            if 2 < calculated_speed < 80:  # sanity check
                avg_speed_kmh = calculated_speed

    if avg_speed_kmh > 0:
        eta_hours = distance_km / avg_speed_kmh
        eta_minutes = round(eta_hours * 60, 1)
        return max(eta_minutes, 1.0)  # minimum 1 minute

    return None
