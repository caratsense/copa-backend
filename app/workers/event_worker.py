"""
Event Worker — consumes order events from the Redis queue.

Run as:
    python -m app.workers.event_worker
    # or via Docker Compose (already configured as 'worker' service)

RESPONSIBILITIES:
1. Log the event stream (audit / debugging)
2. Run the morning queue processor at store opening

It does NOT send WhatsApp messages — see the note above the handlers.

To add a new reaction to an event:
1. Add a handler function below
2. Register it in the HANDLERS dict
"""

import json
import time
import redis
from app.config import get_settings
from app.db import SessionLocal
from app.models.order import Order, OrderStatus

settings = get_settings()
REDIS_QUEUE = "order_events"


# ─── EVENT HANDLERS ───────────────────────────────────

#
# NOTE ON WHATSAPP: this worker deliberately does NOT send WhatsApp messages.
#
# There used to be two independent notification pipelines firing on the same
# status change — this worker (via app/services/whatsapp.py) and
# dispatch_notifications() (via app/services/whatsapp_sender.py). They defined
# templates with overlapping names but different parameter lists, so customers
# received two messages per event and `order_cancelled` rendered garbage
# ("Your order #Priya has been cancelled. Refund: ₹47").
#
# dispatch_notifications() is now the single owner of outbound WhatsApp. This
# worker only logs and does non-messaging side work.
#

def handle_order_created(event: dict):
    order_id = event["order_id"]
    total = event["payload"].get("total_price", 0)
    print(f"[Worker] New order #{order_id} - total {total}")


def handle_status_changed(event: dict):
    order_id = event["order_id"]
    old = event["payload"].get("from", "")
    new = event["payload"].get("to", "")
    print(f"[Worker] Order #{order_id}: {old} -> {new}")


def handle_baker_assigned(event: dict):
    order_id = event["order_id"]
    baker_name = event["payload"].get("baker_name", "Unknown")
    print(f"[Worker] Order #{order_id} assigned to baker: {baker_name}")


def handle_rider_assigned(event: dict):
    order_id = event["order_id"]
    rider_name = event["payload"].get("rider_name", "Unknown")
    self_assigned = event["payload"].get("method", "") == "self_assigned"
    print(f"[Worker] Order #{order_id} rider {'self-assigned' if self_assigned else 'assigned'}: {rider_name}")


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

            # Run once per day during the 8 AM hour.
            # The marker is set BEFORE the work so a failure can't spin this
            # loop for the rest of the hour.
            if current_hour == 8 and last_morning_check != today_key:
                last_morning_check = today_key
                try:
                    count = process_morning_queue()
                    if count > 0:
                        print(f"[Scheduler] Morning queue: assigned {count} orders to bakers")
                except Exception as e:
                    print(f"[Scheduler] Morning queue failed: {e}")

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
