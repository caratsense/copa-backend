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

from fastapi import HTTPException

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


# The least notice the kitchen will take for anything at all.
#
# A cake off the menu can go out the same day — Shriya's example: ordered at
# 2 PM, deliverable from 7 PM. A custom cake needs longer, and the checkout
# page enforces that separately, because only the client knows which lines are
# custom. This is the floor that applies to every order however it arrives,
# and its job is to refuse a delivery time in the past or minutes away.
#
# It refuses rather than adjusts. schedule_order_delivery used to silently
# move a past time to the next available slot, so "today, 2 PM" chosen at 6 PM
# became an order confirmed for a time the customer never picked — after the
# checkout page had shown them 2 PM.
MIN_LEAD_HOURS = 5


def earliest_delivery_time(now: datetime | None = None) -> datetime:
    """The soonest a customer may ask for, in IST."""
    base = (now or datetime.now(IST)).astimezone(IST)
    return base + timedelta(hours=MIN_LEAD_HOURS)


def check_delivery_lead_time(requested: datetime | None) -> None:
    """Refuse a delivery time the kitchen cannot meet."""
    if requested is None:
        return
    moment = requested if requested.tzinfo else requested.replace(tzinfo=IST)
    if moment.astimezone(IST) < earliest_delivery_time():
        raise HTTPException(
            status_code=422,
            detail=(
                f"We need about {MIN_LEAD_HOURS} hours to bake and decorate. "
                "Please choose a later delivery time."
            ),
        )


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


# ── The dates and slots a customer may actually choose ──
#
# Derived here, from MIN_LEAD_HOURS and the shop's own opening hours, because
# the WhatsApp bot was deciding this for itself and getting a different answer
# from the website. The bot's earliest date was a hardcoded "Tomorrow", so a
# cake the website would sell for collection at 7 PM today could not be ordered
# for today at all over WhatsApp, and its three slots were offered whatever the
# hour - "Morning (8 AM - 12 PM)" was still on the menu at 9 PM for the
# following day only because the date had been pushed out a day to begin with.
#
# Named windows rather than a free time entry: a chat cannot validate "half
# seven-ish", and the kitchen works in halves of the day anyway. Each carries
# the hour the order is actually written down as.
DELIVERY_SLOTS = (
    {"key": 1, "label": "Morning (8 AM – 12 PM)", "hour": 10},
    {"key": 2, "label": "Afternoon (12 PM – 4 PM)", "hour": 14},
    {"key": 3, "label": "Evening (4 PM – 8 PM)", "hour": 18},
)

# How far ahead an order may be placed. Seven days matched the bot's old clamp.
MAX_DAYS_AHEAD = 7


def slots_for_date(db: Session, day: date, now: datetime | None = None) -> list[dict]:
    """
    The slots still bookable on `day`, soonest first.

    A slot survives two tests: its hour falls inside the shop's opening hours,
    and it is at least MIN_LEAD_HOURS away. The second is what lets today be
    offered at all, and what stops it being offered once it is too late - the
    same rule `check_delivery_lead_time` enforces when the order is submitted,
    so the bot cannot offer a time the API will then refuse with a 422.
    """
    opens, closes = _get_hours(db)
    earliest = earliest_delivery_time(now)

    available = []
    for slot in DELIVERY_SLOTS:
        at = datetime.combine(day, time(slot["hour"], 0), tzinfo=IST)
        if not (opens <= at.time() <= closes):
            continue
        if at < earliest:
            continue
        available.append({**slot, "at": at})
    return available


def delivery_date_options(db: Session, now: datetime | None = None) -> list[dict]:
    """
    The days a customer may choose, soonest first, each with its own slots.

    A day with no bookable slot left is not offered. Today therefore drops off
    the list by itself late in the evening instead of needing a rule of its own.
    """
    base = (now or datetime.now(IST)).astimezone(IST)
    options = []
    for offset in range(MAX_DAYS_AHEAD + 1):
        day = (base + timedelta(days=offset)).date()
        slots = slots_for_date(db, day, now=base)
        if not slots:
            continue
        if offset == 0:
            label = f"Today ({day.strftime('%d %b')})"
        elif offset == 1:
            label = f"Tomorrow ({day.strftime('%d %b')})"
        else:
            label = day.strftime("%a %d %b")
        options.append({"date": day, "label": label, "slots": slots})
    return options


# ── Where to collect a self-pickup order ──────────────
#
# Not a hardcoded address. The shop's own address is the shop's to set, it is
# the kind of thing that changes, and a wrong address printed confidently on a
# collection slip is worse than no address at all. So it is a site setting,
# editable in the admin panel, and every caller degrades to the phone number
# when it has not been filled in yet.
PICKUP_ADDRESS_KEYS = ("pickup_address", "store_address", "brand_address")


def pickup_details(db: Session) -> dict:
    """
    What a customer collecting an order needs to be told.

    Returns the address (or None), the hours they can come in, and a phone
    number to call. `line` is the ready-made sentence for WhatsApp and for the
    order page, so the two cannot drift apart.
    """
    address = _setting(db, PICKUP_ADDRESS_KEYS)
    phone = _setting(db, ("brand_phone",))
    opens, closes = _get_hours(db)
    hours = f"{opens.strftime('%I:%M %p').lstrip('0')} to {closes.strftime('%I:%M %p').lstrip('0')}"

    if address:
        line = f"Collect from {address} — open {hours}."
    elif phone:
        # No address on file. Say so plainly and give them a human, rather
        # than inventing a location or pretending there is nothing to collect.
        line = f"Please call {phone} for directions to the shop — open {hours}."
    else:
        line = f"The shop is open {hours}."

    return {"address": address, "phone": phone, "hours": hours, "line": line}
