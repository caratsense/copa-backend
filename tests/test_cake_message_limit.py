"""
The cake message is what gets piped on in icing, and only that.

THE ORDER THAT WAS LOST
-----------------------
A customer asked for a birthday cake on WhatsApp, was sent to the site, built
one in Build a Cake and uploaded a design reference. The page recorded that
reference by writing its filename into the item's `message` field:

    message: `Design reference supplied: ${reference.name}`

`message` is capped at 60 characters, because it is the text written on the
cake. The prefix alone is 27. His picture came off a phone — the kind named
"WhatsApp Image 2026-11-13 at 9.15.55 PM.jpeg", 44 characters — so the field
arrived at 71 and the order was refused. What he saw was Pydantic's own
sentence, "String should have at most 60 characters", for a box that does not
exist on any screen: the page had assembled the text itself. He gave up and
said so on WhatsApp.

The shorter filenames were worse rather than better. "cake.jpg" fits inside
60, so the order went through and the baker's queue showed

    On the cake: "Design reference supplied: cake.jpg"

as the words to pipe onto it.

WHAT THESE PIN DOWN
-------------------
The limit itself, which is right and stays — and that it is reached only by a
customer writing a long message, never by the site putting something else in
the field. The frontend no longer sends a filename there at all; these are the
backend half, so a future caller that tries it is refused loudly instead of
quietly decorating a cake with a filename.
"""

import pytest

from app.models.order import OrderStatus
from app.models.pricing import DesignRule, RushRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.schemas import ItemCustomization

from tests.conftest import auth, make_user


# The exact string the build page used to assemble, and the two filenames that
# decide whether it fits. Kept verbatim so this test keeps describing the real
# failure rather than a tidied-up version of it.
OLD_PREFIX = "Design reference supplied: "
PHONE_FILENAME = "WhatsApp Image 2026-11-13 at 9.15.55 PM.jpeg"
SHORT_FILENAME = "cake.jpg"


@pytest.fixture
def cake(db):
    product = Product(name="Vanilla Base Cake", category="vanilla-base",
                      base_price=2250.0, is_available=True)
    db.add_all([
        product,
        SizeRule(name="1kg", multiplier=1.0, is_active=True),
        DesignRule(name="Basic Cream Finish", cost=0.0, is_active=True),
        RushRule(name="Standard (24hr+)", cost=0.0, is_active=True),
    ])
    db.commit()
    db.refresh(product)
    return product


def _order_body(product_id, message):
    return {
        "fulfilment": "PICKUP",
        "payment_method": "ONLINE",
        "delivery_address": "Flat 1, Lucknow",
        "items": [{
            "product_id": product_id,
            "quantity": 1,
            "customization": {
                "size": "1kg", "flavor": "", "design": "Basic Cream Finish",
                "addons": [], "rush": "Standard (24hr+)",
                "message": message,
            },
        }],
    }


# ── The limit ────────────────────────────────────────────────────────────


def test_the_prefix_and_a_phone_filename_do_not_fit(client, customer, cake):
    """
    The arithmetic that lost the order: 27 + 44 = 71.

    Asserted rather than assumed, because the whole failure turns on it and
    a kinder-looking prefix would hide it again.
    """
    assembled = OLD_PREFIX + PHONE_FILENAME
    assert len(assembled) > 60, len(assembled)

    response = client.post("/orders", json=_order_body(cake.id, assembled),
                           headers=auth(customer))
    assert response.status_code == 422, response.text


def test_a_short_filename_fitting_is_not_a_consolation(client, db, customer, cake):
    """
    The other half, and the worse one.

    "Design reference supplied: cake.jpg" is 35 characters, so it was accepted
    — and then shown to the baker as the words to write on the cake. Nothing
    in the API can tell that apart from a real message, which is exactly why
    the fix had to be that the page stops putting it there.
    """
    assembled = OLD_PREFIX + SHORT_FILENAME
    assert len(assembled) <= 60

    response = client.post("/orders", json=_order_body(cake.id, assembled),
                           headers=auth(customer))
    assert response.status_code == 201, response.text
    # It lands in the customisation verbatim, as a cake message.
    (item,) = response.json()["items"]
    assert item["customization"]["message"] == assembled


def test_a_real_message_of_sixty_characters_is_accepted(client, customer, cake):
    """The limit is a limit, not an obstacle. Sixty is a usable cake message."""
    message = "Happy 30th Birthday Priya, with all our love from everyone!"
    assert len(message) <= 60, len(message)

    response = client.post("/orders", json=_order_body(cake.id, message),
                           headers=auth(customer))
    assert response.status_code == 201, response.text
    (item,) = response.json()["items"]
    assert item["customization"]["message"] == message


def test_sixty_one_characters_is_refused(client, customer, cake):
    """The boundary, named."""
    assert client.post("/orders", json=_order_body(cake.id, "x" * 60),
                       headers=auth(customer)).status_code == 201
    assert client.post("/orders", json=_order_body(cake.id, "x" * 61),
                       headers=auth(customer)).status_code == 422


def test_an_empty_message_is_fine(client, customer, cake):
    """
    Most cakes have no writing on them, and Build a Cake now sends "" rather
    than a filename. If this ever required a message the builder would be
    unusable.
    """
    response = client.post("/orders", json=_order_body(cake.id, ""),
                           headers=auth(customer))
    assert response.status_code == 201, response.text


def test_the_schema_default_is_blank(client):
    """
    Omitting the message entirely is the same as sending nothing. The builder
    relies on this: it no longer sets the field at all when there is no
    message to write.
    """
    assert ItemCustomization().message == ""


# ── What the customer is told ────────────────────────────────────────────


def test_the_refusal_says_which_field_and_what_the_limit_is(client, customer, cake):
    """
    The frontend turns this into a sentence, and it can only do that if the
    response says where the problem is. It needs `loc` ending in "message"
    and `ctx.max_length`; without them the customer gets a generic apology.
    """
    response = client.post(
        "/orders", json=_order_body(cake.id, OLD_PREFIX + PHONE_FILENAME),
        headers=auth(customer),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail

    offending = [e for e in detail if e.get("loc", [])[-1] == "message"]
    assert offending, detail
    assert offending[0].get("ctx", {}).get("max_length") == 60, offending[0]
