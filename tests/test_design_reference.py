"""
The picture of the cake the customer wants, from the builder to the kitchen.

WHAT THERE WAS BEFORE
---------------------
No way for a customer to attach a picture to an order. Not a narrow way, not
an awkward way — none. POST /orders/{id}/attachments/photo was
require_role(ADMIN, BAKER), and the only customer-facing upload in the app
belonged to enquiries, which never become orders.

So Build a Cake recorded the reference the customer chose by writing its
FILENAME into the cake's `message` — the text piped on in icing, capped at 60
characters. A reference called "cake.jpg" asked the baker to write "Design
reference supplied: cake.jpg" on the cake. One off a phone, named "WhatsApp
Image 2026-11-13 at 9.15.55 PM.jpeg", took the field to 71 characters and the
order could not be placed at all. A customer gave up at checkout and said so
on WhatsApp; that is the order this work comes from.
tests/test_cake_message_limit.py pins the half of the fix that stopped the
filename going anywhere near the icing. This file pins the half that makes the
PICTURE arrive.

HOW IT WORKS
------------
Two steps, because the picture is chosen minutes before the order exists, on a
different page, with a payment gateway in between — and a File cannot be put
in localStorage, so it cannot simply be carried.

  1. POST /uploads/design-reference stages the file and returns its URL. Open,
     because step three of five on the builder page is no place for a login.
  2. The URL travels in the cart and POST /orders claims it onto the order as
     a photo in its brief, which is the list the kitchen already reads.

And afterwards, the customer can add a picture to their own order from their
own order page, for the "here, this is what I meant" that until now happened
on WhatsApp and reached the order as nothing at all.
"""

import io

import pytest

from app.models.order import OrderStatus
from app.models.order_attachment import AttachmentKind, OrderAttachment
from app.models.pricing import DesignRule, RushRule, SizeRule
from app.models.product import Product

from tests.conftest import auth, make_order


# A real PNG, and a real JPEG. save_image sniffs the leading bytes and takes
# the extension from them, never from the filename, so a text file named .png
# is refused — these have to be the real thing or they fail for the wrong
# reason.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
WEBM = bytes.fromhex("1a45dfa3") + b"\x00" * 40

# The filename from the real failure. It is here to make sure nothing on this
# path ever cares how long a filename is again.
PHONE_FILENAME = "WhatsApp Image 2026-11-13 at 9.15.55 PM.jpeg"

# A well-formed reference URL whose file was never written.
MISSING_REFERENCE = "/media/ref_" + "ab" * 12 + "_cdef0123.jpg"


def _file(data=PNG, name="reference.png", kind="image/png"):
    return {"file": (name, io.BytesIO(data), kind)}


def _stage(client, **kw):
    """Upload a reference the way the builder does, and return its URL."""
    res = client.post("/uploads/design-reference", files=_file(**kw))
    assert res.status_code == 200, res.text
    return res.json()["url"]


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


def _order_body(product_id, reference_url=None, message=""):
    customization = {
        "size": "1kg", "flavor": "", "design": "Basic Cream Finish",
        "addons": [], "rush": "Standard (24hr+)", "message": message,
    }
    if reference_url is not None:
        customization["reference_url"] = reference_url
    return {
        "fulfilment": "PICKUP",
        "payment_method": "ONLINE",
        "delivery_address": "Flat 1, Lucknow",
        "items": [{"product_id": product_id, "quantity": 1,
                   "customization": customization}],
    }


def _of_kind(db, order_id, kind):
    return db.query(OrderAttachment).filter(
        OrderAttachment.order_id == order_id,
        OrderAttachment.kind == kind,
    ).all()


def _photos(db, order_id):
    return _of_kind(db, order_id, AttachmentKind.PHOTO)


# ─── STAGING, BEFORE THE ORDER EXISTS ─────────────────


def test_a_reference_can_be_staged_without_signing_in(client):
    """
    The builder is open to anybody and the picture is picked at step three of
    five. Requiring a token here would mean either losing the picture or
    stopping the sale to ask for a password.
    """
    res = client.post("/uploads/design-reference", files=_file())
    assert res.status_code == 200, res.text
    assert res.json()["url"].startswith("/media/ref_")


def test_the_staged_name_is_unguessable_and_says_nothing_about_the_file(client):
    """
    /media is served without authentication — the brief's pictures always have
    been — so the URL is the only thing protecting a photograph off somebody's
    phone. 32 hex characters of randomness, and nothing taken from the name the
    browser sent.
    """
    url = _stage(client, name=PHONE_FILENAME, data=JPEG, kind="image/jpeg")
    name = url.rsplit("/", 1)[-1]

    assert name.endswith(".jpg")          # from the bytes, not the ".jpeg" name
    assert "WhatsApp" not in name
    stem = name[len("ref_"):].rsplit(".", 1)[0]
    assert len(stem.replace("_", "")) == 32

    assert _stage(client, name=PHONE_FILENAME, data=JPEG, kind="image/jpeg") != url


def test_a_file_that_is_not_an_image_is_refused(client):
    """
    UPLOAD_DIR is served by StaticFiles, which types a file by its extension,
    so a ".png" full of markup would come back from /media as a live document
    on the API's own origin. The bytes have to be an image.
    """
    res = client.post("/uploads/design-reference",
                      files=_file(data=b"<html>not a picture</html>"))
    assert res.status_code == 400, res.text


# ─── CLAIMING IT ONTO THE ORDER ───────────────────────


def test_the_picture_reaches_the_order(client, db, customer, cake):
    """The whole point. It arrives as a photo in the brief the kitchen reads."""
    url = _stage(client)

    res = client.post("/orders", json=_order_body(cake.id, url), headers=auth(customer))
    assert res.status_code == 201, res.text

    (photo,) = _photos(db, res.json()["id"])
    assert photo.file_url == url
    assert cake.name in (photo.body or "")     # which cake it is a reference for
    assert photo.author_id == customer.id
    assert photo.author_role == "customer"


def test_the_brief_goes_to_the_kitchen_with_it(client, customer, admin, cake):
    """
    A row in a table nobody reads is not an attached picture. The admin and
    baker screens read the order, so the picture has to be on the order.
    """
    url = _stage(client)
    order_id = client.post("/orders", json=_order_body(cake.id, url),
                           headers=auth(customer)).json()["id"]

    res = client.get(f"/orders/{order_id}", headers=auth(admin))
    assert res.status_code == 200, res.text
    assert [a["file_url"] for a in res.json()["attachments"]] == [url]


def test_the_cake_message_is_left_alone(client, customer, cake):
    """
    The reference travels in its own field. Nothing about it is written into
    `message`, which is the text piped onto the cake in icing — that is what
    this whole path exists to stop doing.
    """
    url = _stage(client, name=PHONE_FILENAME, data=JPEG, kind="image/jpeg")

    res = client.post("/orders", json=_order_body(cake.id, url), headers=auth(customer))
    assert res.status_code == 201, res.text

    (item,) = res.json()["items"]
    assert item["customization"]["message"] == ""
    assert "WhatsApp" not in str(item["customization"])


@pytest.mark.parametrize("bad", [
    "/media/product_3_aaaaaaaa.jpg",                      # a real file, not a reference
    "/media/ref_aaaaaaaaaaaaaaaaaaaaaaaa_bbbbbbbb.svg",   # not an image we serve
    "/media/../../etc/passwd",
    "https://example.invalid/cake.jpg",
    "not a url at all",
])
def test_a_reference_url_we_did_not_write_is_refused(client, customer, cake, bad):
    """
    The claim takes a URL, so the shape of what it accepts is the whole gate.
    Only a name this app wrote can be claimed: nothing else already on disk, no
    path climbing out of UPLOAD_DIR, and nothing hosted elsewhere being passed
    off as a reference.
    """
    res = client.post("/orders", json=_order_body(cake.id, bad), headers=auth(customer))
    assert res.status_code == 422, res.text


def test_a_missing_picture_does_not_cost_the_order(client, db, customer, cake):
    """
    The direction that matters, and the lesson this work came from: an order
    was lost over a photograph once. A well-formed URL whose file is not there
    leaves a note asking staff to chase the picture, and the order is placed.
    """
    res = client.post("/orders", json=_order_body(cake.id, MISSING_REFERENCE),
                      headers=auth(customer))
    assert res.status_code == 201, res.text
    order_id = res.json()["id"]

    assert _photos(db, order_id) == []
    (note,) = _of_kind(db, order_id, AttachmentKind.NOTE)
    assert "did not reach us" in note.body
    assert "WhatsApp" in note.body


def test_an_order_with_no_reference_gets_no_attachment(client, db, customer, cake):
    """Most cakes have no reference. Nothing should appear for them."""
    order_id = client.post("/orders", json=_order_body(cake.id),
                           headers=auth(customer)).json()["id"]

    assert db.query(OrderAttachment).filter(
        OrderAttachment.order_id == order_id).all() == []


def test_two_cakes_with_two_references_keep_them_apart(client, db, customer, cake):
    """
    An order of two cakes with two pictures is two pictures that have to be
    told apart, which is why the caption names the cake.
    """
    other = Product(name="Chocolate Base Cake", category="chocolate-base",
                    base_price=2500.0, is_available=True)
    db.add(other)
    db.commit()
    db.refresh(other)

    first, second = _stage(client), _stage(client)
    body = _order_body(cake.id, first)
    body["items"].append(_order_body(other.id, second)["items"][0])

    res = client.post("/orders", json=body, headers=auth(customer))
    assert res.status_code == 201, res.text

    captions = {p.file_url: p.body for p in _photos(db, res.json()["id"])}
    assert len(captions) == 2
    assert cake.name in captions[first]
    assert other.name in captions[second]


# ─── THE CUSTOMER'S OWN ORDER PAGE ────────────────────


def test_the_customer_can_send_a_picture_for_their_own_order(client, db, customer):
    """
    The other half: the order is placed, and then they want to show us what
    they meant. This is the request that was require_role(ADMIN, BAKER), so the
    answer used to be "send it on WhatsApp" and the order carried nothing.
    """
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    res = client.post(f"/orders/{order.id}/attachments/photo",
                      files=_file(), data={"caption": "like this, but blue"},
                      headers=auth(customer))
    assert res.status_code == 201, res.text
    assert res.json()["body"] == "like this, but blue"
    assert res.json()["author_role"] == "customer"


def test_an_uncaptioned_customer_picture_still_says_who_sent_it(client, db, customer):
    """
    Otherwise it is an unexplained photograph in among the kitchen's own notes,
    and whoever is baking cannot tell whether it came from the office or from
    the person buying the cake.
    """
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    res = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                      headers=auth(customer))
    assert res.status_code == 201, res.text
    assert res.json()["body"] == "Sent by the customer"


def test_the_customer_sees_their_own_pictures_and_nothing_else(client, db, customer, admin):
    """
    Their order page has to show what they have attached. It must not become a
    window onto what the kitchen says about their cake.
    """
    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    client.post(f"/orders/{order.id}/attachments/note",
                json={"body": "Quote them 1500 if they ask"}, headers=auth(admin))
    client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                data={"caption": "the office's own reference"}, headers=auth(admin))
    mine = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                       headers=auth(customer)).json()

    res = client.get(f"/orders/{order.id}/attachments", headers=auth(customer))
    assert res.status_code == 200, res.text
    assert [a["id"] for a in res.json()] == [mine["id"]]
    assert "1500" not in res.text


def test_nobody_can_send_a_picture_for_somebody_elses_order(
    client, db, customer, other_customer,
):
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    res = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                      headers=auth(other_customer))
    assert res.status_code == 403, res.text


def test_a_customer_cannot_leave_a_note_or_a_voice_note(client, db, customer):
    """
    They can send a picture; they are not in the kitchen's conversation. Those
    two routes keep their staff-only dependency, which is also what stops a
    customer reaching the brief through them.
    """
    order = make_order(db, customer, status=OrderStatus.RECEIVED)

    assert client.post(f"/orders/{order.id}/attachments/note",
                       json={"body": "make it bigger"},
                       headers=auth(customer)).status_code == 403
    assert client.post(f"/orders/{order.id}/attachments/voice",
                       files={"file": ("n.webm", io.BytesIO(WEBM), "audio/webm")},
                       headers=auth(customer)).status_code == 403


@pytest.mark.parametrize("status", [
    OrderStatus.IN_PRODUCTION, OrderStatus.PACKAGED,
    OrderStatus.DELIVERED, OrderStatus.CANCELLED,
])
def test_the_window_closes_when_the_kitchen_starts(client, db, customer, status):
    """
    A picture arriving after the cake is on the bench is not a reference, it is
    a change to something already being made — which is a conversation, and the
    reply says so rather than accepting it silently.
    """
    order = make_order(db, customer, status=status)

    res = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                      headers=auth(customer))
    assert res.status_code == 409, res.text
    assert "WhatsApp" in res.json()["detail"]


def test_staff_can_still_add_a_picture_at_any_stage(client, db, admin, customer):
    """The window is the customer's, not the kitchen's."""
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY)

    assert client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                       headers=auth(admin)).status_code == 201


def test_a_customer_cannot_fill_the_disk_with_one_order(client, db, customer):
    """
    Staff have no limit, because four photos off a customer's WhatsApp is the
    normal case. An open-ended number of uploads against a row anybody can
    create is something else.
    """
    from app.api.routes.orders import MAX_CUSTOMER_PHOTOS

    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    for _ in range(MAX_CUSTOMER_PHOTOS):
        assert client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                           headers=auth(customer)).status_code == 201

    res = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                      headers=auth(customer))
    assert res.status_code == 400, res.text
    assert len(_photos(db, order.id)) == MAX_CUSTOMER_PHOTOS


# ─── TAKING ONE BACK ──────────────────────────────────


def test_the_customer_can_remove_a_picture_they_sent_by_mistake(client, db, customer):
    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    sent = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                       headers=auth(customer)).json()

    res = client.delete(f"/orders/{order.id}/attachments/{sent['id']}",
                        headers=auth(customer))
    assert res.status_code == 204, res.text
    assert _photos(db, order.id) == []


def test_the_customer_cannot_remove_the_kitchens_pictures(client, db, customer, admin):
    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    theirs = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                         headers=auth(admin)).json()

    res = client.delete(f"/orders/{order.id}/attachments/{theirs['id']}",
                        headers=auth(customer))
    assert res.status_code == 403, res.text
    assert db.query(OrderAttachment).filter(
        OrderAttachment.id == theirs["id"]).first() is not None


def test_the_customer_cannot_delete_the_note_about_a_missing_picture(
    client, db, customer, cake,
):
    """
    That note is written in the customer's name, so "only what you left
    yourself" would otherwise let them remove the one thing telling the kitchen
    their picture never arrived.
    """
    order_id = client.post("/orders", json=_order_body(cake.id, MISSING_REFERENCE),
                           headers=auth(customer)).json()["id"]
    (note,) = _of_kind(db, order_id, AttachmentKind.NOTE)

    res = client.delete(f"/orders/{order_id}/attachments/{note.id}",
                        headers=auth(customer))
    assert res.status_code == 404, res.text
    assert db.query(OrderAttachment).filter(
        OrderAttachment.id == note.id).first() is not None


def test_a_picture_cannot_be_withdrawn_once_the_cake_is_being_made(client, db, customer):
    """
    The brief is the record of what was asked for while the cake was made. By
    then it is not the customer's to edit.
    """
    order = make_order(db, customer, status=OrderStatus.RECEIVED)
    sent = client.post(f"/orders/{order.id}/attachments/photo", files=_file(),
                       headers=auth(customer)).json()

    order.status = OrderStatus.IN_PRODUCTION
    db.commit()

    res = client.delete(f"/orders/{order.id}/attachments/{sent['id']}",
                        headers=auth(customer))
    assert res.status_code == 409, res.text
    assert len(_photos(db, order.id)) == 1
