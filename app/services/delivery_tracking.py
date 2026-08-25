"""
Delivery Tracking Service
==========================
Uses Redis for real-time GPS storage (no DB writes for every ping).
GPS data flows: Rider phone → WebSocket → Redis → Customer WebSocket
                                              └→ Admin fleet WebSocket

Architecture:
- Redis key per active delivery: "delivery:{order_id}" → JSON with lat, lng, timestamp
- Redis key for delivery metadata: "delivery:{order_id}:meta" → pickup/dropoff coords, start time
- Data expires after 24 hours automatically (TTL)

ETA calculation:
- Haversine distance from rider's current position to dropoff
- Average speed derived from last N GPS points
- Fallback to zone's estimated_time if not enough data

Redis is treated as *ephemeral* state: PostgreSQL remains the source of truth for
which orders are active and who is assigned to them. Every function here degrades
to None/{} when Redis is unreachable rather than raising, because a Redis outage
must not tear down a rider's GPS socket or 500 the dashboard.
"""

import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import redis

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

DELIVERY_TTL = 86400  # 24 hours
SPEED_HISTORY_SIZE = 10  # keep last N points for speed calculation

# A rider's device pushes a fix every few seconds. Anything older than this is
# reported as stale rather than presented as the rider's live position — the
# point is kept (it is still the last known truth), only its label changes.
# Single source of truth for staleness: the API serialises the derived state so
# backend and frontend can never disagree about it.
STALE_AFTER_SECONDS = 45


# Module-level client. `redis.from_url` builds a connection pool, so constructing
# one per call (as this module used to) created a new pool for every GPS ping.
_client: Optional[redis.Redis] = None


def _get_redis() -> Optional[redis.Redis]:
    global _client
    if _client is None:
        try:
            _client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        except Exception as e:  # malformed REDIS_URL — nothing to retry
            logger.error("[Tracking] Cannot build Redis client: %s", e)
            return None
    return _client


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
    pickup_lat: Optional[float] = None,
    pickup_lng: Optional[float] = None,
    dropoff_lat: Optional[float] = None,
    dropoff_lng: Optional[float] = None,
) -> dict:
    """
    Initialize tracking for a delivery. Called when order goes OUT_FOR_DELIVERY.

    Coordinates are optional: an order whose destination we cannot resolve is
    still trackable (the rider's own position is what matters), it just has no
    ETA. Storing a guessed dropoff would produce a confidently wrong ETA.
    """
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

    try:
        pipe = r.pipeline()
        pipe.setex(f"delivery:{order_id}:meta", DELIVERY_TTL, json.dumps(meta))
        pipe.delete(f"delivery:{order_id}:history")
        pipe.execute()
    except Exception as e:
        logger.error("[Tracking] start failed for order %s: %s", order_id, e)
        return {"error": "Redis not available"}

    return meta


def set_tracking_rider(order_id: int, rider_id: int) -> None:
    """
    Point an in-flight delivery's tracking metadata at a different rider.

    Called when an admin reassigns an order that is already OUT_FOR_DELIVERY, so
    the fleet view doesn't keep crediting the previous rider's position. The GPS
    history is dropped because it belongs to the previous rider's route and would
    otherwise poison the speed/ETA calculation.
    """
    r = _get_redis()
    if not r:
        return
    try:
        raw = r.get(f"delivery:{order_id}:meta")
        meta = json.loads(raw) if raw else None

        if meta is not None and meta.get("rider_id") == rider_id:
            return  # nothing actually changed

        pipe = r.pipeline()
        if meta is not None:
            meta["rider_id"] = rider_id
            pipe.setex(f"delivery:{order_id}:meta", DELIVERY_TTL, json.dumps(meta))
        # Clear the position even when there is no metadata to update. Missing
        # metadata (evicted, expired, or a delivery that predates automatic
        # tracking) must not leave the previous rider's last fix attached to
        # this order — that is precisely the stale association to avoid.
        pipe.delete(f"delivery:{order_id}")
        pipe.delete(f"delivery:{order_id}:history")
        pipe.execute()
    except Exception as e:
        logger.error("[Tracking] rider swap failed for order %s: %s", order_id, e)


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

    try:
        pipe = r.pipeline()
        pipe.setex(f"delivery:{order_id}", DELIVERY_TTL, json.dumps(current))
        # Append to history (capped list for speed calculation)
        pipe.rpush(f"delivery:{order_id}:history", json.dumps({"lat": lat, "lng": lng, "t": now}))
        pipe.ltrim(f"delivery:{order_id}:history", -SPEED_HISTORY_SIZE, -1)
        pipe.expire(f"delivery:{order_id}:history", DELIVERY_TTL)
        pipe.execute()

        # Calculate ETA
        eta = _calculate_eta(r, order_id, lat, lng)
    except Exception as e:
        logger.error("[Tracking] update failed for order %s: %s", order_id, e)
        return {"error": "Redis not available"}

    return {**current, "eta_minutes": eta}


def get_rider_location(order_id: int) -> Optional[dict]:
    """Get current rider position + ETA for a delivery."""
    r = _get_redis()
    if not r:
        return None

    try:
        current_raw, meta_raw = r.mget(
            f"delivery:{order_id}",
            f"delivery:{order_id}:meta",
        )

        if not current_raw:
            return None

        current = json.loads(current_raw)
        meta = json.loads(meta_raw) if meta_raw else {}

        eta = _calculate_eta(r, order_id, current["lat"], current["lng"])
    except Exception as e:
        logger.error("[Tracking] read failed for order %s: %s", order_id, e)
        return None

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
        "seconds_since_update": _age_seconds(current.get("timestamp")),
    }


def get_locations_bulk(order_ids: Iterable[int]) -> dict[int, dict]:
    """
    Fleet-scale read: current position + ETA for many orders in one round trip.

    Deliberately keyed off an explicit order id list (which the caller gets from
    PostgreSQL) rather than scanning Redis — no KEYS/SCAN in a request path, and
    a stray key for a completed order can never resurrect it in the fleet view.
    Orders with no GPS yet are simply absent from the result.
    """
    ids = list(dict.fromkeys(order_ids))  # de-dupe, preserve order
    if not ids:
        return {}

    r = _get_redis()
    if not r:
        return {}

    try:
        keys: list[str] = []
        for oid in ids:
            keys.append(f"delivery:{oid}")
            keys.append(f"delivery:{oid}:meta")
        raw = r.mget(keys)

        # History is only needed for orders that actually have a position.
        present = [
            (oid, raw[i * 2], raw[i * 2 + 1])
            for i, oid in enumerate(ids)
            if raw[i * 2]
        ]
        if not present:
            return {}

        pipe = r.pipeline()
        for oid, _, _ in present:
            pipe.lrange(f"delivery:{oid}:history", 0, -1)
        histories = pipe.execute()
    except Exception as e:
        logger.error("[Tracking] bulk read failed: %s", e)
        return {}

    out: dict[int, dict] = {}
    for (oid, current_raw, meta_raw), history_raw in zip(present, histories):
        # One malformed payload must not blank out the whole fleet.
        try:
            current = json.loads(current_raw)
            meta = json.loads(meta_raw) if meta_raw else {}
            eta = _eta_from_parts(
                current["lat"], current["lng"], meta, history_raw or []
            )
            out[oid] = {
                "order_id": oid,
                "rider_lat": current["lat"],
                "rider_lng": current["lng"],
                "updated_at": current.get("updated_at"),
                "dropoff_lat": meta.get("dropoff_lat"),
                "dropoff_lng": meta.get("dropoff_lng"),
                "pickup_lat": meta.get("pickup_lat"),
                "pickup_lng": meta.get("pickup_lng"),
                "eta_minutes": eta,
                "status": meta.get("status", "unknown"),
                "seconds_since_update": _age_seconds(current.get("timestamp")),
            }
        except Exception as e:
            logger.warning("[Tracking] skipping malformed payload for order %s: %s", oid, e)

    return out


def stop_delivery_tracking(order_id: int):
    """Called when order is DELIVERED — cleans up Redis keys. Idempotent."""
    r = _get_redis()
    if not r:
        return

    try:
        # Update meta status
        meta_raw = r.get(f"delivery:{order_id}:meta")
        pipe = r.pipeline()
        if meta_raw:
            meta = json.loads(meta_raw)
            meta["status"] = "completed"
            meta["completed_at"] = datetime.now(timezone.utc).isoformat()
            pipe.setex(f"delivery:{order_id}:meta", 3600, json.dumps(meta))  # keep for 1hr after delivery

        pipe.delete(f"delivery:{order_id}")
        pipe.delete(f"delivery:{order_id}:history")
        pipe.execute()
    except Exception as e:
        logger.error("[Tracking] stop failed for order %s: %s", order_id, e)


def is_stale(seconds_since_update: Optional[float]) -> bool:
    """Single definition of 'this position is no longer live'."""
    return seconds_since_update is None or seconds_since_update >= STALE_AFTER_SECONDS


def redis_available() -> bool:
    """
    Whether live positions can be served at all right now.

    Lets the dashboard say "orders are listed, positions are unavailable"
    instead of silently rendering every rider as awaiting GPS during an outage.
    """
    r = _get_redis()
    if not r:
        return False
    try:
        return bool(r.ping())
    except Exception:
        return False


def _age_seconds(timestamp: Optional[float]) -> Optional[float]:
    """Seconds since a stored GPS fix, clamped at 0 for minor clock skew."""
    if not timestamp:
        return None
    try:
        return max(0.0, round(time.time() - float(timestamp), 1))
    except (TypeError, ValueError):
        return None


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
    history_raw = r.lrange(f"delivery:{order_id}:history", 0, -1)
    return _eta_from_parts(current_lat, current_lng, meta, history_raw)


def _eta_from_parts(
    current_lat: float,
    current_lng: float,
    meta: dict,
    history_raw: list,
) -> Optional[float]:
    """ETA maths, split out so the bulk path can reuse it without re-reading Redis."""
    dropoff_lat = meta.get("dropoff_lat")
    dropoff_lng = meta.get("dropoff_lng")

    if dropoff_lat is None or dropoff_lng is None:
        return None

    distance_km = _haversine_km(current_lat, current_lng, dropoff_lat, dropoff_lng)

    # Calculate average speed from history
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
