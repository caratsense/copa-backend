"""
Event System — every state change produces an event.

Events are:
1. Stored in the order_events table (permanent audit trail)
2. Pushed to a Redis queue for async processing (notifications, analytics, etc.)

To add a new event type:
1. Just emit it — event_type is a free-form string, no enum needed.
2. If you want a worker to react to it, add a handler in app/workers/event_worker.py
"""

import json
import redis
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.event import OrderEvent

settings = get_settings()

REDIS_QUEUE = "order_events"


def _get_redis():
    """Lazy Redis connection."""
    try:
        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def emit_event(
    db: Session,
    order_id: int,
    event_type: str,
    payload: dict | None = None,
) -> OrderEvent:
    """Create an event in DB and push it to Redis."""

    payload = payload or {}

    # 1. Save to database
    event = OrderEvent(
        order_id=order_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(event)
    db.flush()  # get the id without committing (caller controls the transaction)

    # 2. Push to Redis (best-effort — don't fail the request if Redis is down)
    try:
        r = _get_redis()
        if r:
            message = json.dumps({
                "event_id": event.id,
                "order_id": order_id,
                "event_type": event_type,
                "payload": payload,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            r.lpush(REDIS_QUEUE, message)
    except Exception as e:
        # Log but don't crash — the DB record is the source of truth
        print(f"[EventService] Redis push failed: {e}")

    return event
