"""
The two switches the shop throws when it cannot cope.

DELIVERY OFF
------------
Collection keeps working; delivery is refused. The refusal lives in the order
service rather than only in the checkout UI, because the checkout POSTs a
fulfilment string: a Delivery button that is merely hidden stops nobody with a
stale tab open, and stops nothing at all coming in over the API or the bot.

NO SAME DAY
-----------
A date, not a flag. While today is on or before it, the earliest a customer
may ask for is tomorrow. A date expires by itself, which matters because the
reason for setting it — the kitchen is swamped this week — expires too, and a
flag somebody has to remember to turn off is how a shop ends up refusing
same-day orders in December because of a Tuesday in November.

Both are read from one place, app.services.store_hours, because the website,
the WhatsApp bot and the API all have to give the same answer. The bot
deciding its own earliest date is a mistake this codebase has already made
once: it offered a slot the API then refused with a 422.
"""

from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.site_settings import SiteSettings
from app.services.store_hours import (
    IST,
    DELIVERY_ENABLED_KEY,
    SAME_DAY_BLOCK_KEY,
    check_delivery_lead_time,
    delivery_enabled,
    earliest_delivery_time,
    same_day_blocked,
    slots_for_date,
)


def _set(db, key, value):
    row = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    if row:
        row.value = value
    else:
        db.add(SiteSettings(key=key, value=value))
    db.commit()


@pytest.fixture
def clean_settings(db):
    """Leave the two keys exactly as they were found."""
    before = {
        k: (db.query(SiteSettings).filter(SiteSettings.key == k).first() or None)
        for k in (DELIVERY_ENABLED_KEY, SAME_DAY_BLOCK_KEY)
    }
    before = {k: (v.value if v else None) for k, v in before.items()}
    yield
    for key, value in before.items():
        row = db.query(SiteSettings).filter(SiteSettings.key == key).first()
        if value is None:
            if row:
                db.delete(row)
        else:
            if row:
                row.value = value
            else:
                db.add(SiteSettings(key=key, value=value))
    db.commit()


# ── Delivery on and off ───────────────────────────────────────────────────


def test_a_shop_with_no_setting_at_all_is_delivering(db, clean_settings):
    """
    The default has to be "delivering".

    Absent means the shop behaved this way before the switch existed, so
    deploying the switch cannot silently stop delivery on a live bakery.
    """
    row = db.query(SiteSettings).filter(SiteSettings.key == DELIVERY_ENABLED_KEY).first()
    if row:
        db.delete(row)
        db.commit()
    assert delivery_enabled(db) is True


def test_false_is_the_only_thing_that_turns_delivery_off(db, clean_settings):
    _set(db, DELIVERY_ENABLED_KEY, "false")
    assert delivery_enabled(db) is False

    # Anything else is read as on, rather than as "I do not understand, so I
    # will stop taking orders".
    for value in ("true", "TRUE", "yes", "", "   "):
        _set(db, DELIVERY_ENABLED_KEY, value)
        assert delivery_enabled(db) is True, value


def test_case_and_whitespace_do_not_keep_delivery_running(db, clean_settings):
    """The admin writes "false"; nobody should have to match it exactly."""
    for value in ("False", "  FALSE  ", "fAlSe"):
        _set(db, DELIVERY_ENABLED_KEY, value)
        assert delivery_enabled(db) is False, value


# ── No same day ───────────────────────────────────────────────────────────


def test_no_block_means_the_ordinary_five_hour_lead(db, clean_settings):
    row = db.query(SiteSettings).filter(SiteSettings.key == SAME_DAY_BLOCK_KEY).first()
    if row:
        db.delete(row)
        db.commit()

    now = datetime(2026, 11, 6, 9, 0, tzinfo=IST)
    assert earliest_delivery_time(now, db) == now + timedelta(hours=5)


def test_a_block_set_for_today_pushes_the_earliest_into_tomorrow(db, clean_settings):
    today = date(2026, 11, 6)
    _set(db, SAME_DAY_BLOCK_KEY, today.isoformat())

    now = datetime(2026, 11, 6, 9, 0, tzinfo=IST)
    earliest = earliest_delivery_time(now, db)

    assert earliest.date() == date(2026, 11, 7)
    # And not merely five hours later, which would still have been today.
    assert earliest > now + timedelta(hours=5)


def test_the_block_expires_on_its_own(db, clean_settings):
    """
    The whole reason it is a date.

    Nobody has to remember to switch it back off, so the shop cannot end up
    refusing same-day orders weeks after the day it was busy.
    """
    _set(db, SAME_DAY_BLOCK_KEY, "2026-11-06")

    assert same_day_blocked(db, date(2026, 11, 6)) is True
    assert same_day_blocked(db, date(2026, 11, 7)) is False
    assert same_day_blocked(db, date(2026, 12, 25)) is False


def test_an_unparseable_date_does_not_stop_the_shop_selling(db, clean_settings):
    """A typo in a setting must not take same-day ordering down with it."""
    _set(db, SAME_DAY_BLOCK_KEY, "next tuesday")
    assert same_day_blocked(db, date(2026, 11, 6)) is False


def test_a_time_today_is_refused_while_the_block_stands(db, clean_settings):
    _set(db, SAME_DAY_BLOCK_KEY, date.today().isoformat())

    later_today = datetime.now(IST).replace(hour=23, minute=0, second=0, microsecond=0)
    with pytest.raises(HTTPException) as refused:
        check_delivery_lead_time(later_today, db)

    assert refused.value.status_code == 422
    assert "tomorrow" in refused.value.detail.lower()


def test_tomorrow_is_accepted_while_the_block_stands(db, clean_settings):
    _set(db, SAME_DAY_BLOCK_KEY, date.today().isoformat())

    tomorrow = (datetime.now(IST) + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0)
    check_delivery_lead_time(tomorrow, db)  # does not raise


def test_the_bot_and_the_website_are_offered_the_same_days(db, clean_settings):
    """
    slots_for_date is where both of them get their answer, so blocking today
    has to empty today's list rather than leaving the bot offering a slot the
    API would then refuse.
    """
    today = date(2026, 11, 6)
    now = datetime(2026, 11, 6, 9, 0, tzinfo=IST)

    row = db.query(SiteSettings).filter(SiteSettings.key == SAME_DAY_BLOCK_KEY).first()
    if row:
        db.delete(row)
        db.commit()
    assert slots_for_date(db, today, now), "today should be bookable with no block"

    _set(db, SAME_DAY_BLOCK_KEY, today.isoformat())
    assert slots_for_date(db, today, now) == []
    assert slots_for_date(db, date(2026, 11, 7), now), "tomorrow stays bookable"


# ── The switch has to be readable, or the UI cannot show it ───────────


def test_checkout_can_read_the_delivery_switch_without_logging_in(client, db, clean_settings):
    """
    The gap this closes.

    Checkout greys the Delivery button out by reading this key, and the
    reader is an allow-list: a key that is not on it answers exactly as a key
    that does not exist. delivery_enabled was not on it, so the fetch 404'd,
    the page read that as "delivering" - which is the right default for a
    missing setting - and left Delivery selectable while the order service
    refused every delivery order.

    The customer would have chosen Delivery, typed an address, picked a slot
    and been refused at the end. Failing silently in the wrong direction is
    what makes this worth a test rather than a comment.
    """
    _set(db, DELIVERY_ENABLED_KEY, "false")

    response = client.get("/settings/public/delivery_enabled")

    assert response.status_code == 200
    assert str(response.json()["value"]).lower() == "false"


def test_the_owners_private_number_is_still_not_public(client):
    """
    The allow-list exists because /settings/public once returned the whole
    table, brand_phone included. Adding a key to it must not become a habit.
    """
    assert client.get("/settings/public/brand_phone").status_code == 404
