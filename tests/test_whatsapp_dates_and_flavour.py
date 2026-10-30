"""
What the bot asks for, and what it then sends to the pricing engine.

Three separate faults, all found by one ₹1 test cake ordered over WhatsApp:

  * the order died at *confirm* with "Unknown flavour: 'Classic'", because
    skipping the flavour step left a hardcoded default in the order payload
    that had never been reachable before;
  * the message on the cake stopped being asked, because the skip jumped to
    the address and took the message step with it;
  * the dates were "Tomorrow / Day after tomorrow / 3 days from now", naming
    no actual date and ruling out same-day while the website sold it.
"""

from datetime import datetime, timedelta

import pytest

from app.models.product import Product
from app.services.store_hours import (
    IST, MIN_LEAD_HOURS, delivery_date_options, slots_for_date,
)


# ─── THE CRASH ────────────────────────────────────────

def test_a_fixed_price_cake_is_ordered_with_no_flavour(db, customer):
    """
    The exact payload the bot builds, through the real engine.

    "Classic" is not a FlavorRule and never was. It sat as a default that
    nothing could reach while every customer was asked to pick a flavour, and
    went live the moment fixed-price products skipped that step.
    """
    from app.schemas import ItemCustomization
    from app.services.pricing_engine import calculate_item_price

    product = Product(name="Internal Test Cake", category="internal",
                      base_price=1.0, is_customizable=False,
                      is_available=True, pricing_unit="piece")
    db.add(product)
    db.commit()
    db.refresh(product)

    # Blank, not a name. This is what the engine documents as "not chosen".
    price = calculate_item_price(
        db=db, product=product,
        customization=ItemCustomization(size="", flavor=""),
        quantity=1,
    )
    assert price.line_total == 1.0


def test_an_invented_flavour_is_still_refused(db, customer):
    """The fix is to send nothing, not to make the engine lenient."""
    from fastapi import HTTPException

    from app.schemas import ItemCustomization
    from app.services.pricing_engine import calculate_item_price

    product = Product(name="Internal Test Cake", category="internal",
                      base_price=1.0, is_customizable=False,
                      is_available=True, pricing_unit="piece")
    db.add(product)
    db.commit()
    db.refresh(product)

    with pytest.raises(HTTPException) as e:
        calculate_item_price(
            db=db, product=product,
            customization=ItemCustomization(size="", flavor="Classic"),
            quantity=1,
        )
    assert e.value.status_code == 400


# ─── THE DATES ────────────────────────────────────────

def test_today_is_offered_when_there_is_still_time(db):
    """
    Ordered first thing, collectable the same evening. The bot's earliest was
    a hardcoded "Tomorrow", so this was impossible over WhatsApp while the
    website allowed it.
    """
    morning = datetime.now(IST).replace(hour=8, minute=0, second=0, microsecond=0)
    options = delivery_date_options(db, now=morning)

    assert options, "no dates offered at all"
    assert options[0]["date"] == morning.date()
    assert "Today" in options[0]["label"]


def test_today_drops_off_once_it_is_too_late(db):
    """Nine at night: every slot today is inside the kitchen's notice."""
    late = datetime.now(IST).replace(hour=21, minute=0, second=0, microsecond=0)
    options = delivery_date_options(db, now=late)

    assert all(o["date"] != late.date() for o in options)


def test_every_date_names_a_real_date(db):
    morning = datetime.now(IST).replace(hour=8, minute=0, second=0, microsecond=0)
    for option in delivery_date_options(db, now=morning):
        # "Today (30 Oct)", "Tomorrow (31 Oct)", "Sat 01 Nov" — never a bare
        # "3 days from now", which named nothing the customer could check.
        assert any(ch.isdigit() for ch in option["label"]), option["label"]


def test_no_slot_is_offered_inside_the_lead_time(db):
    """Whatever the bot offers, the API must accept — the same rule, once."""
    now = datetime.now(IST).replace(hour=11, minute=0, second=0, microsecond=0)
    earliest = now + timedelta(hours=MIN_LEAD_HOURS)

    for option in delivery_date_options(db, now=now):
        for slot in slots_for_date(db, option["date"], now=now):
            assert slot["at"] >= earliest, (
                f"{slot['label']} on {option['date']} is sooner than the "
                f"{MIN_LEAD_HOURS}h the kitchen needs"
            )


def test_a_slot_the_bot_offers_passes_the_api_check(db):
    """
    The two used different rules, so the bot could offer a time the order
    endpoint then rejected with a 422.
    """
    from app.services.store_hours import check_delivery_lead_time

    # The real clock on both sides. Pinning a fake hour here would compare
    # slots built at 9 AM against a lead time measured from now, which fails
    # for a reason that has nothing to do with the code under test.
    options = delivery_date_options(db)
    assert options

    for option in options[:2]:
        for slot in slots_for_date(db, option["date"]):
            # Raises if the API would refuse it.
            check_delivery_lead_time(slot["at"])
