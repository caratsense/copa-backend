"""
Nothing is sold twice, and a cake can be out without disappearing.

WHAT WAS WRONG
--------------
One count existed in the whole system, on add-on rules, and create_order moved
it like this:

    if addon and addon.stock is not None:
        addon.stock = max(0, addon.stock - item_data.quantity)

No check. Two customers could both buy the last topper and the number simply
floored at zero; the second one paid for a thing that was not there and
nothing anywhere said so. The matching restore ran only on cancellation and
knew only about add-ons.

Products had `is_available`, which is a different switch: it takes the product
off the menu. So the only way to say "we have run out of brownies" removed
brownies from the catalogue altogether, and the customer who had the page
bookmarked found nothing and no explanation. Extras — balloons, gift wrap —
had no count at all and could be sold forever.

WHAT THESE PIN DOWN
-------------------
That the last unit cannot be sold twice; that a cake can be marked sold out
with no count anywhere near it and STAYS on the menu, flagged; that the two
ways of being out and the one way of being hidden stay three different things;
and that every movement leaves a line in the ledger with an order number or a
person on it.

Route-level wherever the transaction matters. A service test cannot show that
a refused second line gives the first line's units back, because the rollback
belongs to the request.
"""

import pytest

from app.models.extra import Extra
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.pricing import AddonRule, DesignRule, RushRule, SizeRule
from app.models.product import PRICING_ROW_TAG, Product
from app.models.stock_movement import StockMovement
from app.models.user import UserRole
from app.schemas import ItemCustomization, OrderCreate, OrderItemCreate, StatusUpdate
from app.services import stock_service
from app.services.order_service import create_order, update_order_status

from tests.conftest import auth, make_user


# ── Scenery ──────────────────────────────────────────────────────────────


@pytest.fixture
def shop(db):
    """
    A counter with countable things on it and a made-to-order cake beside them.

    Brownies are counted because brownies are counted. The cake is not, which
    is the normal case and the one a count must never be forced onto.
    """
    brownie = Product(name="Walnut Brownie", category="brownies", base_price=120.0,
                      is_available=True, pricing_unit="fixed", stock=2)
    cake = Product(name="Red Velvet Cake", category="premium", base_price=1200.0,
                   is_available=True)
    withdrawn = Product(name="Summer Mango Cake", category="seasonal",
                        base_price=900.0, is_available=False)
    base_row = Product(name="Vanilla Base Cake", category="pricing",
                       base_price=800.0, is_available=True, tags=[PRICING_ROW_TAG])
    db.add_all([
        brownie, cake, withdrawn, base_row,
        SizeRule(name="1kg", multiplier=1.0, is_active=True),
        DesignRule(name="Basic Cream Finish", cost=0.0, is_active=True),
        RushRule(name="Standard (24hr+)", cost=0.0, is_active=True),
        AddonRule(name="Photo Topper", cost=200.0, stock=1, is_active=True),
        AddonRule(name="Candles", cost=50.0, stock=None, is_active=True),
        Extra(name="Balloon Bundle", price=250.0, stock=1, is_active=True),
        Extra(name="Gift Wrap", price=80.0, stock=None, is_active=True),
    ])
    db.commit()
    for row in (brownie, cake, withdrawn, base_row):
        db.refresh(row)
    return {"brownie": brownie, "cake": cake, "withdrawn": withdrawn,
            "base_row": base_row}


def _order(product_id, quantity=1, addons=(), extras=()):
    return {
        "payment_method": "ONLINE",
        "fulfilment": "PICKUP",
        "delivery_address": "Flat 1, Lucknow",
        "items": [{
            "product_id": product_id,
            "quantity": quantity,
            "customization": {
                "size": "1kg", "flavor": "", "design": "Basic Cream Finish",
                "addons": list(addons), "rush": "Standard (24hr+)",
            },
        }],
        "extras": list(extras),
    }


def _place(db, customer, product, quantity=1, addons=(), extras=()):
    """Straight through the service, for the cases that are not about HTTP."""
    return create_order(db, OrderCreate(
        user_id=customer.id,
        payment_method="COD",
        fulfilment="PICKUP",
        delivery_address="Flat 1, Lucknow",
        extras=list(extras),
        items=[OrderItemCreate(
            product_id=product.id,
            quantity=quantity,
            customization=ItemCustomization(
                size="1kg", flavor="", design="Basic Cream Finish",
                addons=list(addons), rush="Standard (24hr+)",
            ),
        )],
    ))


def _level(db, model, name):
    return db.query(model).filter(model.name == name).first().stock


# ── The last one cannot be sold twice ────────────────────────────────────


def test_the_last_brownie_cannot_be_sold_twice(client, db, customer, shop):
    """
    The headline. Two orders, two brownies, three requests.

    The old decrement was read-modify-write with no comparison in it, so this
    was not merely possible under load — it was the documented behaviour of
    `max(0, stock - qty)`.
    """
    headers = auth(customer)
    first = client.post("/orders", json=_order(shop["brownie"].id, 2), headers=headers)
    assert first.status_code == 201, first.text

    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 0

    second = client.post("/orders", json=_order(shop["brownie"].id, 1), headers=headers)
    assert second.status_code == 400, "sold a brownie that did not exist"
    assert "sold out" in second.json()["detail"].lower()

    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 0, "count went negative"


def test_asking_for_more_than_is_left_says_how_many_there_are(client, customer, shop):
    """
    "Out of stock" sends somebody back to a cart of five things with no idea
    which one to change. The message names the item and both numbers.
    """
    response = client.post(
        "/orders", json=_order(shop["brownie"].id, 5), headers=auth(customer),
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "Walnut Brownie" in detail
    assert "2" in detail and "5" in detail, detail


def test_a_refused_line_does_not_eat_the_line_before_it(client, db, customer, shop):
    """
    An order is all or nothing.

    Two lines: a brownie there is stock for, then a topper there is not. The
    reservation for the first happens before the second is even looked at, so
    without the request rolling back as a whole the shop would lose a brownie
    to an order that was never placed.
    """
    body = _order(shop["brownie"].id, 1, addons=("Photo Topper",))
    body["items"].append({
        "product_id": shop["cake"].id,
        "quantity": 1,
        "customization": {
            "size": "1kg", "flavor": "", "design": "Basic Cream Finish",
            # The topper's stock is 1, and the line above has already taken it.
            "addons": ["Photo Topper"], "rush": "Standard (24hr+)",
        },
    })

    refused = client.post("/orders", json=body, headers=auth(customer))
    assert refused.status_code == 400, refused.text

    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 2, "brownie eaten by a failed order"
    assert _level(db, AddonRule, "Photo Topper") == 1, "topper eaten by a failed order"
    assert db.query(Order).count() == 0, "a refused order was written anyway"


def test_an_uncounted_product_never_blocks_anything(client, db, customer, shop):
    """
    stock IS NULL means not counted, and most of a made-to-order catalogue is
    not. Twenty cakes in one order is a catering job, not an error.
    """
    response = client.post(
        "/orders", json=_order(shop["cake"].id, 20), headers=auth(customer),
    )
    assert response.status_code == 201, response.text
    db.expire_all()
    assert _level(db, Product, "Red Velvet Cake") is None, "a count appeared out of nowhere"


def test_extras_run_out_too(client, db, customer, shop):
    """Balloons are a physical thing in a cupboard. There was one."""
    balloons = db.query(Extra).filter(Extra.name == "Balloon Bundle").first()
    headers = auth(customer)

    first = client.post(
        "/orders", json=_order(shop["cake"].id, 1, extras=[balloons.id]), headers=headers,
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/orders", json=_order(shop["cake"].id, 1, extras=[balloons.id]), headers=headers,
    )
    assert second.status_code == 400
    assert "Balloon Bundle" in second.json()["detail"]


# ── The switch, for the things nobody counts ─────────────────────────────


def test_a_cake_can_be_sold_out_with_no_count_at_all(client, db, customer, shop, admin):
    """
    The case that forced this to be two columns rather than one.

    A cake does not run out, because it has not been made yet. What happens is
    that the kitchen cannot do it today, and nobody is going to maintain a
    number for that.
    """
    cake = shop["cake"]
    marked = client.patch(
        f"/admin/inventory/product/{cake.id}/sold-out",
        json={"sold_out": True}, headers=auth(admin),
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["is_sold_out"] is True

    refused = client.post("/orders", json=_order(cake.id, 1), headers=auth(customer))
    assert refused.status_code == 400
    assert "sold out" in refused.json()["detail"].lower()

    db.expire_all()
    assert db.query(Product).filter(Product.id == cake.id).first().stock is None, \
        "marking a cake sold out invented a count for it"


def test_the_switch_survives_a_restock(client, db, admin, shop):
    """
    More cream arriving does not mean she is ready to take the order again.
    Only she knows when it is over, so only she clears it.
    """
    brownie = shop["brownie"]
    headers = auth(admin)
    client.patch(f"/admin/inventory/product/{brownie.id}/sold-out",
                 json={"sold_out": True}, headers=headers)
    client.post(f"/admin/inventory/product/{brownie.id}/adjust",
                json={"delta": 12, "reason": "RESTOCK"}, headers=headers)

    db.expire_all()
    row = db.query(Product).filter(Product.id == brownie.id).first()
    assert row.stock == 14
    assert row.is_sold_out is True, "a delivery put a cake back on sale by itself"


def test_a_count_reaching_zero_is_sold_out_without_any_switch(db, shop):
    """The two routes meet at one answer, and that answer is derived."""
    brownie = shop["brownie"]
    brownie.stock = 0
    db.commit()
    db.refresh(brownie)
    assert brownie.sold_out is False, "nobody set the switch"
    assert brownie.is_sold_out is True


# ── Sold out is not hidden, and hidden is not sold out ───────────────────


def test_a_sold_out_cake_stays_on_the_menu(client, db, admin, shop):
    """
    The point of the whole feature, from the customer's side.

    "Sold out" tells somebody the shop normally has this and will again. A
    product that has silently disappeared tells them the page is broken, and
    they cannot tell which has happened.
    """
    cake = shop["cake"]
    client.patch(f"/admin/inventory/product/{cake.id}/sold-out",
                 json={"sold_out": True}, headers=auth(admin))

    listed = client.get("/products").json()
    mine = [p for p in listed if p["id"] == cake.id]
    assert mine, "a sold-out cake fell off the menu"
    assert mine[0]["is_sold_out"] is True
    assert mine[0]["is_available"] is True, "sold out quietly became unavailable"

    sections = client.get("/menu/sections").json()
    in_sections = [
        p for sec in sections for p in sec["products"] if p["id"] == cake.id
    ]
    assert in_sections, "a sold-out cake fell off /menu/sections"
    assert in_sections[0]["is_sold_out"] is True


def test_a_withdrawn_product_is_still_hidden(client, shop):
    """
    is_available is a different switch and has to keep meaning what it meant.
    Out of season is not the same as out of stock.
    """
    listed = client.get("/products").json()
    assert not [p for p in listed if p["id"] == shop["withdrawn"].id]


def test_the_bot_does_not_offer_a_sold_out_cake(client, db, admin, shop):
    """
    A WhatsApp list row cannot be greyed out — every row it shows is a row the
    customer can tap. Tapping a sold-out one would price a cake the shop has
    just said it cannot make and fail at the end of the conversation.
    """
    from app.services.wa_customer_flow import _catalogue

    cake = shop["cake"]
    assert any(p["id"] == cake.id for p in _catalogue(db)), "precondition"

    client.patch(f"/admin/inventory/product/{cake.id}/sold-out",
                 json={"sold_out": True}, headers=auth(admin))
    db.expire_all()
    assert not any(p["id"] == cake.id for p in _catalogue(db))


# ── Giving it back ───────────────────────────────────────────────────────


def test_cancelling_returns_all_three_kinds(db, customer, shop):
    """
    The old restore knew only about add-ons. Products and extras are counted
    now, and a cancellation that knows about one of three quietly eats the
    other two.
    """
    balloons = db.query(Extra).filter(Extra.name == "Balloon Bundle").first()
    # One of each: the topper's stock is 1, so a quantity of 2 would need two
    # toppers and be refused before there was anything to cancel.
    order = _place(db, customer, shop["brownie"], quantity=1,
                   addons=("Photo Topper",), extras=[balloons.id])
    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 1
    assert _level(db, AddonRule, "Photo Topper") == 0
    assert _level(db, Extra, "Balloon Bundle") == 0

    update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 2
    assert _level(db, AddonRule, "Photo Topper") == 1
    assert _level(db, Extra, "Balloon Bundle") == 1


def test_a_release_cannot_run_twice(db, customer, shop):
    """
    Called again — by a future code path, not by the lifecycle, which makes
    CANCELLED terminal — it must not inflate the count past what is real.
    """
    order = _place(db, customer, shop["brownie"], quantity=1)
    update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 2

    stock_service.release_order(db, order)
    db.commit()
    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 2, "stock inflated past its real level"


def test_a_completed_sale_keeps_its_units(db, customer, shop):
    """Only a cancellation releases. A sale consumes."""
    order = _place(db, customer, shop["brownie"], quantity=1)
    for status in ("CONFIRMED", "ASSIGNED", "IN_PRODUCTION", "AWAITING_APPROVAL",
                   "PACKAGED", "READY_FOR_PICKUP", "COLLECTED"):
        try:
            update_order_status(db, order.id, StatusUpdate(status=status))
        except Exception:
            break
    db.expire_all()
    assert _level(db, Product, "Walnut Brownie") == 1


# ── The ledger ───────────────────────────────────────────────────────────


def test_a_sale_writes_its_order_number_into_the_ledger(db, customer, shop):
    """
    "Where did the other four go" is the question a count cannot answer, and
    it is the question somebody always asks.
    """
    order = _place(db, customer, shop["brownie"], quantity=2)
    rows = db.query(StockMovement).filter(
        StockMovement.reason == stock_service.REASON_SOLD,
    ).all()
    assert len(rows) == 1
    (row,) = rows
    assert row.kind == stock_service.KIND_PRODUCT
    assert row.item_name == "Walnut Brownie"
    assert row.delta == -2
    assert row.balance == 0
    assert row.order_id == order.id
    assert row.user_id is None, "a sale is not something a person did by hand"


def test_a_hand_adjustment_records_who_and_why(client, db, admin, shop):
    """
    The reason is required because the ledger is the point. "12 to 8" answers
    nothing; "four went in the bin" is what makes wastage visible next month.
    """
    response = client.post(
        f"/admin/inventory/product/{shop['brownie'].id}/adjust",
        json={"delta": -1, "reason": "WASTE", "note": "dropped the tray"},
        headers=auth(admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["stock"] == 1

    row = db.query(StockMovement).filter(
        StockMovement.reason == stock_service.REASON_WASTE,
    ).one()
    assert row.delta == -1
    assert row.user_id == admin.id
    assert row.note == "dropped the tray"
    assert row.order_id is None


def test_an_unknown_reason_is_refused(client, admin, shop):
    """A person cannot write SOLD into the ledger — an order causes that."""
    response = client.post(
        f"/admin/inventory/product/{shop['brownie'].id}/adjust",
        json={"delta": -1, "reason": "SOLD"}, headers=auth(admin),
    )
    assert response.status_code == 400
    assert "RESTOCK" in response.json()["detail"]


def test_the_ledger_keeps_the_name_it_had(db, customer, shop):
    """
    A product can be renamed or deleted, and a ledger that reads "deleted item
    14: -2" has stopped being a ledger.
    """
    _place(db, customer, shop["brownie"], quantity=1)
    shop["brownie"].name = "Double Walnut Brownie"
    db.commit()

    row = db.query(StockMovement).filter(
        StockMovement.reason == stock_service.REASON_SOLD,
    ).one()
    assert row.item_name == "Walnut Brownie"


def test_stopping_the_count_is_not_recorded_as_a_loss(client, db, admin, shop):
    """
    Nothing left the shelf — the shop stopped counting. A delta of -2 here
    would read in the ledger as two brownies being thrown out.
    """
    response = client.patch(
        f"/admin/inventory/product/{shop['brownie'].id}/level",
        json={"stock": None}, headers=auth(admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["stock"] is None
    assert response.json()["is_sold_out"] is False, "uncounted read as sold out"

    row = db.query(StockMovement).filter(
        StockMovement.reason == stock_service.REASON_TRACKING_OFF,
    ).one()
    assert row.delta == 0


# ── What an unpaid attempt is holding ────────────────────────────────────


def test_an_unpaid_attempt_shows_what_it_is_holding(client, db, customer, shop, admin):
    """
    The two abandoned PayU attempts that sat at the top of the order queue in
    red were holding their units and would until somebody cancelled them.
    Nothing expires them, so this number is what makes a squat visible instead
    of leaving a count that looks wrong for no reason.
    """
    placed = client.post(
        "/orders", json=_order(shop["brownie"].id, 1), headers=auth(customer),
    )
    assert placed.status_code == 201
    order_id = placed.json()["id"]

    snapshot = client.get("/admin/inventory", headers=auth(admin)).json()
    brownie = next(i for i in snapshot if i["id"] == shop["brownie"].id
                   and i["kind"] == "product")
    assert brownie["stock"] == 1
    assert brownie["held_by_unpaid"] == 1
    assert brownie["held_by_orders"] == 1
    assert brownie["held_first_order_id"] == order_id


def test_paying_stops_it_being_a_hold(client, db, customer, shop, admin):
    """Once the money is in, the units are sold rather than squatted on."""
    placed = client.post(
        "/orders", json=_order(shop["brownie"].id, 1), headers=auth(customer),
    )
    order = db.query(Order).filter(Order.id == placed.json()["id"]).first()
    order.payment_status = PaymentStatus.PAID
    db.commit()

    snapshot = client.get("/admin/inventory", headers=auth(admin)).json()
    brownie = next(i for i in snapshot if i["id"] == shop["brownie"].id
                   and i["kind"] == "product")
    assert brownie["held_by_unpaid"] == 0


# ── The screen's own list ────────────────────────────────────────────────


def test_the_snapshot_holds_all_three_kinds(client, admin, shop):
    """
    One list, because "what am I out of" does not care whether the answer is a
    cake, a balloon or a topper. It cared only because the counts used to live
    in three places, one of them a tab of the product editor.
    """
    snapshot = client.get("/admin/inventory", headers=auth(admin)).json()
    kinds = {i["kind"] for i in snapshot}
    assert kinds == {"product", "extra", "addon"}


def test_out_of_stock_sorts_above_everything(client, db, admin, shop):
    """
    This order IS the answer to the question the screen asks. A sort control
    would only let somebody hide the thing they most need to see.
    """
    topper = db.query(AddonRule).filter(AddonRule.name == "Photo Topper").first()
    stock_service.set_level(db, stock_service.KIND_ADDON, topper.id, 0)

    snapshot = client.get("/admin/inventory", headers=auth(admin)).json()
    assert snapshot[0]["is_sold_out"] is True
    # Nearly out before plenty left, and uncounted rows last.
    buckets = [
        0 if i["is_sold_out"] else 1 if i["is_low"] else 2 if i["is_counted"] else 3
        for i in snapshot
    ]
    assert buckets == sorted(buckets), buckets


def test_the_builders_price_rows_are_not_stock(client, admin, shop):
    """
    "Vanilla Base Cake" is a per-kg rate the Build a Cake page quotes from,
    not a thing on a shelf. Counting it is meaningless and marking it sold out
    would take the whole custom-cake flow down.
    """
    snapshot = client.get("/admin/inventory", headers=auth(admin)).json()
    assert not [i for i in snapshot if i["id"] == shop["base_row"].id
                and i["kind"] == "product"]


def test_inventory_is_admin_only(client, customer, shop):
    """Stock levels are the shop's business, not a customer's."""
    assert client.get("/admin/inventory").status_code in (401, 403)
    assert client.get("/admin/inventory", headers=auth(customer)).status_code == 403
    assert client.patch(
        f"/admin/inventory/product/{shop['brownie'].id}/level",
        json={"stock": 99}, headers=auth(customer),
    ).status_code == 403


def test_an_unknown_kind_says_which_ones_exist(client, admin):
    """'sizes' and 'flavors' are rules, not stock. The message has to say so."""
    response = client.patch(
        "/admin/inventory/flavour/1/level", json={"stock": 1}, headers=auth(admin),
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "product" in detail and "extra" in detail and "addon" in detail


def test_a_negative_count_is_refused(client, admin, shop):
    """There is no such shelf."""
    response = client.patch(
        f"/admin/inventory/product/{shop['brownie'].id}/level",
        json={"stock": -5}, headers=auth(admin),
    )
    assert response.status_code == 422


def test_adjusting_something_uncounted_says_to_set_a_count_first(client, admin, shop):
    """
    A cake has no number to add six to. The refusal has to say what to do
    instead, or it is the Assign Baker dead end again.
    """
    response = client.post(
        f"/admin/inventory/product/{shop['cake'].id}/adjust",
        json={"delta": 6, "reason": "RESTOCK"}, headers=auth(admin),
    )
    assert response.status_code == 400
    assert "set a count" in response.json()["detail"].lower()
