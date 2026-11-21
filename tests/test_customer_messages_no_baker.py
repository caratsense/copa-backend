"""
What the customer is told, at a shop with no baker and a cake to collect.

TWO THINGS THAT WERE WRONG
--------------------------
The bot's "where is my order" reply named a baker. ASSIGNED read "Assigned to
baker" — and there is no baker; the shop makes these itself. It was describing
a person who does not exist to the one audience that cannot check.

The same reply covered eight of the thirteen statuses. The five it missed were
RECEIVED, QC and every collection state, so a customer asking about a pickup
order got the raw enum back — "Status: READY_FOR_PICKUP" — and PACKAGED told
somebody walking in to fetch their cake it was "Ready for delivery".

WHAT IS PINNED HERE
-------------------
That every status has words of its own, that none of the words promise a
baker, and that a collection is never described as a delivery. Not the exact
phrasing — that is allowed to change — but the properties that made the old
map wrong.
"""

import pytest

from app.models.order import OrderStatus, PaymentStatus
from app.models.user import UserRole
from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus
from app.services import wa_customer_flow as flow
from app.services.wa_notifications import customer_status_phrase, items_summary
from tests.conftest import auth, make_order, make_user



def _set(db, key, value):
    """Write a site setting, for the switches these tests flip."""
    from app.models.site_settings import SiteSettings

    row = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    if row:
        row.value = value
    else:
        db.add(SiteSettings(key=key, value=value))
    db.commit()


@pytest.fixture
def clean_delivery(db):
    """Leave the delivery switch exactly as it was found."""
    from app.models.site_settings import SiteSettings
    from app.services.store_hours import DELIVERY_ENABLED_KEY

    row = db.query(SiteSettings).filter(SiteSettings.key == DELIVERY_ENABLED_KEY).first()
    before = row.value if row else None
    yield
    row = db.query(SiteSettings).filter(SiteSettings.key == DELIVERY_ENABLED_KEY).first()
    if before is None:
        if row:
            db.delete(row)
    elif row:
        row.value = before
    else:
        db.add(SiteSettings(key=DELIVERY_ENABLED_KEY, value=before))
    db.commit()


class _Order:
    """Just enough order for the phrasing, which reads two fields."""

    def __init__(self, status, pickup):
        self.status = type("S", (), {"value": status})()
        self.is_pickup = pickup


# ── Nothing promises a baker ─────────────────────────────────────────────


@pytest.mark.parametrize("pickup", [True, False])
def test_no_status_tells_the_customer_about_a_baker(pickup):
    """
    The shop has none. Every one of these is read by a customer, so a word
    about a baker is a claim about staff the shop does not employ — and when
    it does employ one, the customer still never meets them.
    """
    for status in OrderStatus:
        phrase = customer_status_phrase(_Order(status.value, pickup))
        assert "baker" not in phrase.lower(), f"{status.value}: {phrase!r}"


def test_every_status_has_words_of_its_own():
    """
    No raw enum ever reaches a customer.

    The old map held eight of thirteen and fell through to `.value`, so the
    five it missed were shown as ALL_CAPS_WITH_UNDERSCORES.
    """
    for status in OrderStatus:
        for pickup in (True, False):
            phrase = customer_status_phrase(_Order(status.value, pickup))
            assert phrase, status.value
            assert phrase != status.value, f"{status.value} falls through raw"
            assert "_" not in phrase, f"{status.value}: {phrase!r}"


def test_a_status_nobody_has_added_yet_still_reads_as_english():
    """A new status must degrade to words, not to SOMETHING_LIKE_THIS."""
    phrase = customer_status_phrase(_Order("PUT_ON_HOLD", False))
    assert "_" not in phrase
    assert phrase[0].isupper()


# ── A collection is not a delivery ───────────────────────────────────────


def test_a_collection_is_never_described_as_a_delivery():
    """
    The specific lie: PACKAGED said "Ready for delivery" to someone who had
    given no address and was walking in for it.
    """
    for status in ("PACKAGED", "READY_FOR_PICKUP", "COLLECTED", "NOT_COLLECTED"):
        phrase = customer_status_phrase(_Order(status, True)).lower()
        assert "deliver" not in phrase, f"{status}: {phrase!r}"


def test_a_delivery_is_not_described_as_a_collection():
    for status in ("PACKAGED", "OUT_FOR_DELIVERY", "DELIVERED"):
        phrase = customer_status_phrase(_Order(status, False)).lower()
        assert "collect" not in phrase, f"{status}: {phrase!r}"


# ── The bot's own reply ──────────────────────────────────────────────────


@pytest.fixture
def pickup_order(db, customer):
    from app.models.order_item import OrderItem
    from app.models.product import Product

    product = Product(name="Chocolate Walnut Brownies", category="bakes",
                      base_price=450.0, is_available=True)
    db.add(product)
    db.commit()

    order = make_order(db, customer, status=OrderStatus.READY_FOR_PICKUP,
                       fulfilment="PICKUP")
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PAID
    # No size and no flavour — a brownie has neither, which is what the bot's
    # hand-rolled items line turned into a bare space.
    db.add(OrderItem(order_id=order.id, product_id=product.id, quantity=2,
                     customization={}, price=900.0, price_breakdown={}))
    db.commit()
    db.refresh(order)
    return order


def test_the_items_line_names_the_cake_rather_than_printing_a_space(db, pickup_order):
    summary = items_summary(pickup_order)
    assert "Chocolate Walnut Brownies" in summary
    assert summary.strip() == summary
    assert "  " not in summary


def test_asking_the_bot_where_a_collection_order_is(db, pickup_order, customer):
    """
    The reply the customer reads, end to end through the flow.

    Checked for what it must not say as much as what it must: the old one
    named a baker and printed READY_FOR_PICKUP at them.
    """
    # Driven the way a customer drives it: "2" is Track an order, then the
    # number. Both legs go through the real parser, so this breaks if the
    # tracking path moves rather than only if the wording changes.
    flow._reply_to_customer(customer.phone, "2", customer)
    reply = flow._reply_to_customer(customer.phone, str(pickup_order.id), customer)

    assert f"#{pickup_order.id}" in reply
    assert "Ready to collect" in reply
    assert "READY_FOR_PICKUP" not in reply
    assert "baker" not in reply.lower()
    assert "Collection" in reply and "Delivery:" not in reply
    assert "Chocolate Walnut Brownies" in reply


# ── The outbox says why, truthfully ──────────────────────────────────────


def test_an_in_house_order_does_not_log_a_broken_baker_account(client, db, admin):
    """
    "No phone number on record" reads as a staff account somebody has to go
    and fix. There is no account — the shop is making the cake itself — and a
    column of red against every order it makes is how a real failure gets
    missed among them.
    """
    buyer = make_user(db, "Walk-in Customer", UserRole.CUSTOMER)
    order = make_order(db, buyer, status=OrderStatus.CONFIRMED,
                       fulfilment="PICKUP", with_proof=False)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PAID
    db.commit()

    client.patch(f"/orders/{order.id}/status", json={"status": "ASSIGNED"},
                 headers=auth(admin))

    rows = (db.query(WhatsAppMessage)
              .filter(WhatsAppMessage.order_id == order.id,
                      WhatsAppMessage.template_key == "baker_new_order")
              .all())
    assert rows, "the attempt should still be recorded"
    for row in rows:
        assert row.status == WhatsAppMessageStatus.SKIPPED
        assert "in-house" in (row.last_error or ""), row.last_error
        assert "phone number" not in (row.last_error or "")


def test_the_customer_hears_nothing_about_the_kitchen_arrangements(client, db, admin):
    """
    Who is making the cake is the shop's business, not the customer's.

    This used to assert that the customer hears *nothing at all* between
    "confirmed" and "ready". That stopped being true on purpose: the silence
    on a cake ordered days ahead was long enough to worry people, so
    order_in_production now tells them it is being made. What must still hold
    — and what this file exists for — is that nothing the customer is sent
    names a baker or implies there is one, because at this shop there isn't.

    ASSIGNED itself stays silent. "A baker has it" is a fact about the
    kitchen rota and means nothing to the person waiting.
    """
    buyer = make_user(db, "Quiet Customer", UserRole.CUSTOMER)
    order = make_order(db, buyer, status=OrderStatus.CONFIRMED,
                       fulfilment="PICKUP", with_proof=False)
    order.payment_method = "ONLINE"
    order.payment_status = PaymentStatus.PAID
    db.commit()

    for status in ("ASSIGNED", "IN_PRODUCTION"):
        client.patch(f"/orders/{order.id}/status", json={"status": status},
                     headers=auth(admin))

    to_customer = (db.query(WhatsAppMessage)
                     .filter(WhatsAppMessage.order_id == order.id,
                             WhatsAppMessage.recipient_role == "customer")
                     .all())

    # Being handed to the kitchen is not news anybody is waiting for.
    assert not [r for r in to_customer if r.event_type == OrderStatus.ASSIGNED.value], \
        [r.template_key for r in to_customer]

    # Whatever they are told, no part of it is about a baker.
    for row in to_customer:
        words = " ".join(str(p) for p in (row.payload or [])).lower()
        assert "baker" not in words, (row.template_key, row.payload)
        assert "assigned" not in words, (row.template_key, row.payload)


# ── A collection is not a delivery, in the words of the bot ──────────────


def test_the_date_question_matches_how_the_cake_is_coming(db, clean_delivery):
    """
    The shop is collection-only, and it asked every customer to pick a
    "Delivery date". The dates are identical either way; only the word for
    what happens on them is wrong.
    """
    collection, _ = flow._date_prompt(db, collecting=True)
    delivery, _ = flow._date_prompt(db, collecting=False)

    assert "Collection date" in collection
    assert "Delivery date" not in collection
    assert "Delivery date" in delivery


def test_closing_delivery_switches_the_question_on_its_own(db, clean_delivery):
    """
    Eight call sites reach the date step. The flag is read off the
    conversation inside _go_to_dates rather than passed in by each of them,
    because one caller forgetting it is how the wording drifts back.
    """
    from app.services.store_hours import DELIVERY_ENABLED_KEY

    _set(db, DELIVERY_ENABLED_KEY, "false")
    assert "Collection date" in flow._go_to_dates(db, "919999000001", {})

    _set(db, DELIVERY_ENABLED_KEY, "true")
    assert "Delivery date" in flow._go_to_dates(db, "919999000002", {})
    # ...and an address of Self Pickup is a collection even with delivery open.
    assert "Collection date" in flow._go_to_dates(
        db, "919999000003", {"address": "Self Pickup"})


def test_the_summary_says_which_one_it_is():
    """It read "Delivery: Self Pickup" — a label contradicted by its value."""
    assert flow._fulfilment_line("Self Pickup") == "Collection: from the shop\n"
    assert flow._fulfilment_line("pickup") == "Collection: from the shop\n"
    assert flow._fulfilment_line("12 Hazratganj, Lucknow") == "Delivery: 12 Hazratganj, Lucknow\n"
    # Nothing recorded is not a collection; it is a delivery with no address,
    # which is a different problem and should not be quietly relabelled.
    assert flow._fulfilment_line(None).startswith("Delivery:")
