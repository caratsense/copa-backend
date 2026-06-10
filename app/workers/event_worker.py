"""
Event Worker — consumes events from Redis queue + sends WhatsApp notifications.

Run as:
    python -m app.workers.event_worker
    # or via Docker Compose (already configured as 'worker' service)

FLOW:
1. Order status changes → event saved to DB + pushed to Redis
2. This worker picks up the event from Redis
3. Looks up customer phone number from DB
4. Sends appropriate WhatsApp notification

To add a new notification:
1. Add a handler function below
2. Register it in HANDLERS dict
3. Create the matching template in WhatsApp Manager
"""

import json
import time
import redis
from app.config import get_settings
from app.db import SessionLocal
from app.models.order import Order
from app.models.user import User

settings = get_settings()
REDIS_QUEUE = "order_events"


def _get_customer_phone(order_id: int) -> str | None:
    """Look up the customer's phone number for an order."""
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            return None
        user = db.query(User).filter(User.id == order.user_id).first()
        return user.phone if user else None
    finally:
        db.close()


def _get_order_total(order_id: int) -> float:
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        return order.total_price if order else 0.0
    finally:
        db.close()


def _get_staff_phone(user_id: int) -> str | None:
    """Look up a baker/rider's phone number by user ID."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        return user.phone if user else None
    finally:
        db.close()


def _get_order_summary(order_id: int) -> str:
    """Short summary of order items for WhatsApp message."""
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order or not order.items:
            return "Order details"
        items = []
        for item in order.items:
            custom = item.customization or {}
            size = custom.get("size", "")
            flavor = custom.get("flavor", "")
            items.append(f"{size} {flavor}".strip())
        return ", ".join(items) if items else "Order details"
    finally:
        db.close()


def _get_delivery_address(order_id: int) -> str:
    """Get delivery address for rider notification."""
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        return order.delivery_address or "Address not set" if order else "Unknown"
    finally:
        db.close()


# ─── EVENT HANDLERS ───────────────────────────────────

def handle_order_created(event: dict):
    order_id = event["order_id"]
    total = event["payload"].get("total_price", 0)
    phone = _get_customer_phone(order_id)

    print(f"[Worker] New order #{order_id} — ₹{total}")

    if phone:
        from app.services.whatsapp import notify_order_confirmed
        notify_order_confirmed(phone, order_id, total)


def handle_status_changed(event: dict):
    order_id = event["order_id"]
    old = event["payload"].get("from", "")
    new = event["payload"].get("to", "")
    phone = _get_customer_phone(order_id)

    print(f"[Worker] Order #{order_id}: {old} → {new}")

    if not phone:
        return

    from app.services.whatsapp import (
        notify_order_confirmed,
        notify_order_preparing,
        notify_rider_on_way,
        notify_order_delivered,
        notify_order_cancelled,
    )

    # Send WhatsApp based on new status
    if new == "CONFIRMED":
        total = _get_order_total(order_id)
        notify_order_confirmed(phone, order_id, total)

    elif new == "IN_PRODUCTION":
        notify_order_preparing(phone, order_id)

    elif new == "OUT_FOR_DELIVERY":
        # Build tracking URL — your frontend will serve this page
        tracking_url = f"https://yourdomain.com/track/{order_id}"
        notify_rider_on_way(phone, tracking_url)

    elif new == "DELIVERED":
        notify_order_delivered(phone, order_id)

    elif new == "CANCELLED":
        total = _get_order_total(order_id)
        notify_order_cancelled(phone, order_id, total)


def handle_baker_assigned(event: dict):
    order_id = event["order_id"]
    baker_id = event["payload"].get("baker_id")
    baker_name = event["payload"].get("baker_name", "Unknown")
    print(f"[Worker] Order #{order_id} assigned to baker: {baker_name}")

    # Send WhatsApp to baker
    if baker_id:
        baker_phone = _get_staff_phone(baker_id)
        if baker_phone:
            from app.services.whatsapp import _send_template
            _send_template(baker_phone, "baker_order_assigned", [
                str(order_id), _get_order_summary(order_id)
            ])


def handle_rider_assigned(event: dict):
    order_id = event["order_id"]
    rider_id = event["payload"].get("rider_id")
    rider_name = event["payload"].get("rider_name", "Unknown")
    self_assigned = event["payload"].get("method", "") == "self_assigned"
    print(f"[Worker] Order #{order_id} rider {'self-assigned' if self_assigned else 'assigned'}: {rider_name}")

    # Send WhatsApp to rider
    if rider_id:
        rider_phone = _get_staff_phone(rider_id)
        if rider_phone:
            from app.services.whatsapp import _send_template
            _send_template(rider_phone, "rider_order_assigned", [
                str(order_id), _get_delivery_address(order_id)
            ])


def handle_payment_updated(event: dict):
    order_id = event["order_id"]
    new_status = event["payload"].get("to", "")
    print(f"[Worker] Order #{order_id} payment: {new_status}")


def handle_default(event: dict):
    print(f"[Worker] Unhandled event: {event['event_type']} for order #{event['order_id']}")


HANDLERS = {
    "ORDER_CREATED": handle_order_created,
    "STATUS_CHANGED": handle_status_changed,
    "BAKER_ASSIGNED": handle_baker_assigned,
    "RIDER_ASSIGNED": handle_rider_assigned,
    "PAYMENT_UPDATED": handle_payment_updated,
}


# ─── MORNING QUEUE PROCESSOR ──────────────────────────

def process_morning_queue():
    """
    Runs at store opening time.
    Finds all CONFIRMED orders without a baker assigned (night orders)
    and auto-assigns bakers to them.
    """
    from app.services.assignment_engine import auto_assign_baker
    from app.services.store_hours import is_store_open

    db = SessionLocal()
    try:
        # Check if store is open
        status = is_store_open(db)
        if not status["is_open"]:
            return 0

        # Find all CONFIRMED orders with no baker (queued night orders)
        queued_orders = (
            db.query(Order)
            .filter(
                Order.status == OrderStatus.CONFIRMED,
                Order.assigned_baker_id == None,
            )
            .order_by(Order.delivery_time.asc().nullslast(), Order.created_at.asc())
            .all()
        )

        if not queued_orders:
            return 0

        assigned = 0
        for order in queued_orders:
            try:
                auto_assign_baker(db, order.id)
                assigned += 1
                print(f"[Scheduler] Auto-assigned baker for queued order #{order.id}")
            except Exception as e:
                print(f"[Scheduler] Could not assign baker for order #{order.id}: {e}")
                break  # if no bakers available, stop trying

        return assigned

    finally:
        db.close()


# ─── MAIN LOOP ───────────────────────────────────────

def run_worker():
    print("[Worker] Starting event worker with WhatsApp notifications + morning scheduler...")
    print(f"[Worker] WhatsApp enabled: {getattr(settings, 'WHATSAPP_ENABLED', False)}")
    r = redis.from_url(settings.REDIS_URL, decode_responses=True)

    last_morning_check = None  # track so we only run once per opening

    while True:
        try:
            # ── Morning queue check (runs once when store opens) ──
            from datetime import datetime, timedelta, timezone
            IST = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(IST)
            today_key = now_ist.strftime("%Y-%m-%d")
            current_hour = now_ist.hour

            # Run between 8:00-8:05 AM, once per day
            if current_hour == 8 and last_morning_check != today_key:
                count = process_morning_queue()
                if count > 0:
                    print(f"[Scheduler] Morning queue: assigned {count} orders to bakers")
                last_morning_check = today_key

            # ── Process Redis events ──
            result = r.brpop(REDIS_QUEUE, timeout=5)
            if result is None:
                continue

            _, raw = result
            event = json.loads(raw)
            event_type = event.get("event_type", "UNKNOWN")

            handler = HANDLERS.get(event_type, handle_default)
            handler(event)

        except redis.ConnectionError:
            print("[Worker] Redis connection lost. Retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            print(f"[Worker] Error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    run_worker()
