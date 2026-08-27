"""
Store Hours Service
====================
Manages bakery operating hours and off-hours order scheduling.

DEFAULT HOURS: 8:00 AM to 10:00 PM (IST)
Admin can change these via site settings.

LOGIC:
- During hours: orders processed normally (ASAP)
- Outside hours: order accepted but delivery_time set to next opening
- Customer always sees clear messaging about when to expect delivery
- Baker/rider assignment is SKIPPED for off-hours orders
  (they get assigned when the store opens)

SITE SETTINGS USED:
  store_hours_open: "08:00"   (24hr format)
  store_hours_close: "22:00"
  store_timezone: "Asia/Kolkata"
"""

from datetime import datetime, time, timedelta, timezone, date
import json

import redis
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.site_settings import SiteSettings

settings = get_settings()

# Defaults
DEFAULT_OPEN = "08:00"
DEFAULT_CLOSE = "22:00"
IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def to_ist(dt):
    """
    Render a stored timestamp in the timezone the bakery actually works in.

    delivery_time is a timezone-aware column, so Postgres returns it in UTC.
    Formatting that directly shows the wrong clock time to everyone reading it
    - a 4:00 PM slot appears as 10:30 AM. Naive values are assumed to already
    be IST, which is what the API accepts from the checkout form.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


# The admin settings page wrote store_open_time / store_close_time while this
# module only ever read store_hours_open / store_hours_close, so changing the
# opening hours in the admin panel silently did nothing at all. The canonical
# keys are the store_hours_* pair; the older names are still read as a fallback
# so a deployment that had set them does not lose the value.
OPEN_KEYS = ("store_hours_open", "store_open_time")
CLOSE_KEYS = ("store_hours_close", "store_close_time")


def _setting(db: Session, keys: tuple[str, ...]) -> str | None:
    """First non-empty value among `keys`, in priority order."""
    for key in keys:
        row = db.query(SiteSettings).filter(SiteSettings.key == key).first()
        if row and (row.value or "").strip():
            return row.value.strip()
    return None


def _get_hours(db: Session) -> tuple[time, time]:
    """Get store open/close times from site settings or defaults."""
    open_str = _setting(db, OPEN_KEYS) or DEFAULT_OPEN
    close_str = _setting(db, CLOSE_KEYS) or DEFAULT_CLOSE

    try:
        open_time = time.fromisoformat(open_str)
    except ValueError:
        open_time = time(8, 0)

    try:
        close_time = time.fromisoformat(close_str)
    except ValueError:
        close_time = time(22, 0)

    return open_time, close_time


def _within_hours(current, open_time, close_time) -> bool:
    """
    Is `current` inside the trading window?

    Handles a window that wraps past midnight. A plain
    `open <= t <= close` is FALSE at every hour of the day when the closing
    time sorts before the opening time - entering 08:00 to 05:00 (meaning
    5 PM, or a genuine overnight shift) silently marked the store closed
    around the clock, which stopped every order from being auto-assigned to
    a baker.
    """
    if open_time <= close_time:
        return open_time <= current <= close_time
    # Wraps midnight: open until close the following morning.
    return current >= open_time or current <= close_time


def is_store_open(db: Session) -> dict:
    """
    Check if the store is currently open.
    Returns: {"is_open": bool, "current_time": str, "opens_at": str, "closes_at": str, "message": str}
    """
    now_ist = datetime.now(IST)
    current_time = now_ist.time()
    open_time, close_time = _get_hours(db)

    # Check manual override
    override = db.query(SiteSettings).filter(SiteSettings.key == "store_open").first()
    if override and override.value.lower() == "false":
        return {
            "is_open": False,
            "current_time": now_ist.strftime("%I:%M %p"),
            "opens_at": open_time.strftime("%I:%M %p"),
            "closes_at": close_time.strftime("%I:%M %p"),
            "message": "Store is currently closed by admin.",
        }

    is_open = _within_hours(current_time, open_time, close_time)

    if is_open:
        message = f"We're open! Orders are being processed. Closes at {close_time.strftime('%I:%M %p')}."
    else:
        message = f"We're closed right now. Your order will be confirmed tomorrow at {open_time.strftime('%I:%M %p')}."

    return {
        "is_open": is_open,
        "current_time": now_ist.strftime("%I:%M %p"),
        "opens_at": open_time.strftime("%I:%M %p"),
        "closes_at": close_time.strftime("%I:%M %p"),
        "message": message,
    }


def get_next_available_time(db: Session) -> datetime:
    """
    Get the next available time for order processing.
    If store is open → now.
    If store is closed → next day's opening time.
    """
    now_ist = datetime.now(IST)
    current_time = now_ist.time()
    open_time, close_time = _get_hours(db)

    if _within_hours(current_time, open_time, close_time):
        return now_ist
    elif current_time < open_time:
        # Before opening today — schedule for today's opening
        return now_ist.replace(hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0)
    else:
        # After closing — schedule for tomorrow's opening
        tomorrow = now_ist + timedelta(days=1)
        return tomorrow.replace(hour=open_time.hour, minute=open_time.minute, second=0, microsecond=0)


def schedule_order_delivery(db: Session, requested_delivery_time: datetime | None) -> dict:
    """
    Determine the actual delivery scheduling for an order.
    
    Returns:
        {
            "delivery_time": datetime,    # when the order will be delivered
            "is_scheduled": bool,         # True if pushed to future
            "is_off_hours": bool,         # True if ordered outside hours
            "message": str,               # customer-facing message
        }
    """
    now_ist = datetime.now(IST)
    open_time, close_time = _get_hours(db)
    current_time = now_ist.time()
    is_open = _within_hours(current_time, open_time, close_time)

    # Customer requested a specific future time
    if requested_delivery_time:
        # Make timezone-aware if naive (assume IST)
        if requested_delivery_time.tzinfo is None:
            requested_delivery_time = requested_delivery_time.replace(tzinfo=IST)
        # If requested time is in the past, bump to next available
        if requested_delivery_time < now_ist:
            requested_delivery_time = get_next_available_time(db)

        return {
            "delivery_time": requested_delivery_time,
            "is_scheduled": True,
            "is_off_hours": not is_open,
            "message": f"Scheduled for delivery at {requested_delivery_time.strftime('%d %b, %I:%M %p')}.",
        }

    # No specific time requested — ASAP or next morning
    if is_open:
        return {
            "delivery_time": now_ist,
            "is_scheduled": False,
            "is_off_hours": False,
            "message": "Your order is being processed now!",
        }
    else:
        next_open = get_next_available_time(db)
        return {
            "delivery_time": next_open,
            "is_scheduled": True,
            "is_off_hours": True,
            "message": f"Order received! It will be confirmed tomorrow at {next_open.strftime('%I:%M %p')}.",
        }
