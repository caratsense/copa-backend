"""
Customer-facing WhatsApp flow: ownership, honest pricing, honest payment state.

Scope is safety only — the conversational behaviour itself is unchanged.
"""

from app.models.delivery import DeliveryZone
from app.models.order import OrderStatus, PaymentStatus
from app.models.pricing import FlavorRule, SizeRule
from app.models.product import Product
from app.models.user import UserRole
from app.services import wa_customer_flow as wcf
from app.services.wa_customer_flow import handle_customer_message

from tests.conftest import make_order, make_user

import pytest


def order_id(db):
    """The id of the most recently created order."""
    from app.models.order import Order
    return db.query(Order).order_by(Order.id.desc()).first().id


@pytest.fixture(autouse=True)
def clean_chat_state(fake_redis):
    """The flow keeps conversation state in Redis; start each test fresh."""
    yield


@pytest.fixture
def catalogue(db):
    product = Product(name="Premium Cake", category="premium",
                      base_price=1000.0, is_available=True)
    db.add(product)
    db.add(SizeRule(name="1kg", multiplier=1.0, is_active=True))
    db.add(FlavorRule(name="Belgian Chocolate", extra_cost=2000.0, is_active=True))
    db.add(DeliveryZone(area_name="Gomti Nagar", charge=150.0,
                        estimated_time=60, is_active=True))
    db.commit()
    db.refresh(product)
    return product


# ─── OWNERSHIP ───────────────────────────────────────

def test_customer_cannot_read_another_customers_order(db):
    """
    Order status used to be looked up by id alone, so any WhatsApp number could
    read a stranger's items, amount and status by guessing a number.
    """
    owner = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    rival = make_user(db, "Rival", UserRole.CUSTOMER, phone="+919777777777")
    order = make_order(db, owner, OrderStatus.OUT_FOR_DELIVERY)

    wcf._set_state("919777777777", {"step": "AWAITING_STATUS_ID"})
    reply = handle_customer_message("919777777777", str(order.id), rival)

    assert f"*Order #{order.id}*" not in reply
    assert "Amount" not in reply
    assert "couldn't find" in reply.lower()


def test_customer_can_read_their_own_order(db):
    owner = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = make_order(db, owner, OrderStatus.OUT_FOR_DELIVERY)

    wcf._set_state("919222222222", {"step": "AWAITING_STATUS_ID"})
    reply = handle_customer_message("919222222222", str(order.id), owner)

    assert f"*Order #{order.id}*" in reply
    assert "Amount" in reply


def test_missing_and_forbidden_orders_are_indistinguishable(db):
    """Otherwise the reply confirms which order numbers exist."""
    owner = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    rival = make_user(db, "Rival", UserRole.CUSTOMER, phone="+919777777777")
    real = make_order(db, owner, OrderStatus.CONFIRMED)

    wcf._set_state("919777777777", {"step": "AWAITING_STATUS_ID"})
    forbidden = handle_customer_message("919777777777", str(real.id), rival)

    wcf._set_state("919777777777", {"step": "AWAITING_STATUS_ID"})
    missing = handle_customer_message("919777777777", "999999", rival)

    assert forbidden.replace(str(real.id), "N") == missing.replace("999999", "N")


# ─── HONEST PAYMENT STATE ────────────────────────────

def test_whatsapp_order_is_not_presented_as_paid(db, catalogue, wa_secret):
    """
    The flow used to reply "Order #N — Confirmed" with an amount and no payment
    step at all, which reads as settled while nothing had been collected.
    """
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3000,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    reply = handle_customer_message("919222222222", "CONFIRM", user)

    assert "Payment: pending" in reply
    assert "Confirmed" not in reply
    # order_id is the parameter the orders page actually reads (it also accepts
    # `success`, which PayU uses on return). `order` was silently ignored, so
    # the customer landed on a bare list with nothing highlighted.
    assert f"/orders?order_id={order_id(db)}" in reply, "no handoff to the real payment flow"


def test_whatsapp_order_is_priced_by_the_pricing_engine(db, catalogue, wa_secret):
    """The quoted amount must be the order's real total, not the chat state's."""
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 1,                      # deliberately wrong chat-state total
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    reply = handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    # 1000 base + 2000 flavour + 150 delivery
    assert order.total_price == 3150.0
    assert "3,150.00" in reply, f"quoted the chat state instead of the order: {reply!r}"
    assert "Rs 1.00" not in reply


def test_whatsapp_order_charges_delivery(db, catalogue, wa_secret):
    """delivery_zone was never passed, so every WhatsApp order shipped free."""
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3000,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.delivery_charge == 150.0


def test_whatsapp_order_does_not_enter_production_unpaid(db, catalogue, wa_secret):
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    make_user(db, "Baker", UserRole.BAKER)
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3150,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.payment_status == PaymentStatus.PENDING
    assert order.status == OrderStatus.CONFIRMED
    assert order.assigned_baker_id is None, "unpaid WhatsApp order went to a baker"


def test_admin_without_opt_in_is_not_messaged_by_the_whatsapp_flow(db, catalogue, wa_secret):
    """The flow used to call notify_admin_new_order directly, skipping consent."""
    from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus

    admin = make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    admin.whatsapp_opt_in = False
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": catalogue.id, "name": "Premium Cake"},
        "size": {"name": "1kg"},
        "flavor": {"name": "Belgian Chocolate"},
        "total": 3150,
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_zone": "Gomti Nagar",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    admin_rows = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.recipient_role == "admin").all()
    assert all(m.status == WhatsAppMessageStatus.SKIPPED for m in admin_rows), \
        "messaged an admin who never opted in"


# ─── PRICING UNITS ───────────────────────────────────
# A fixed-price product - a brownie, a pack of six buns - is not sold by weight.
# The flow used to print "/kg" against every price, demand a size for every
# product, and work its own total out by multiplying by the chosen size.


@pytest.fixture
def brownie(db):
    """A fixed-price product alongside the per-kg cake in `catalogue`."""
    product = Product(name="Chocolate Walnut Brownie", category="brownies",
                      base_price=400.0, pricing_unit="fixed",
                      is_customizable=False, is_available=True)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def test_menu_shows_per_kg_only_for_per_kg_products(db, catalogue, brownie):
    reply = handle_customer_message("919222222222", "menu", None)

    assert "Premium Cake — ₹1,000/kg" in reply
    assert "Chocolate Walnut Brownie — ₹400" in reply
    assert "₹400/kg" not in reply, "quoted a brownie by the kilo"


def _choose(phone: str, product) -> str:
    """
    Start an order and pick `product` from the numbered list.

    By number rather than by name: name matching is the language model's job
    and it is stubbed out here, so the deterministic parser only understands
    the option number.
    """
    handle_customer_message(phone, "order", None)
    listed = wcf._get_state(phone)["products"]
    index = next(i for i, p in enumerate(listed, 1) if p["id"] == product.id)
    return handle_customer_message(phone, str(index), None)


def test_selecting_a_per_kg_cake_still_asks_for_a_size(db, catalogue, brownie):
    """The existing cake flow must be untouched."""
    reply = _choose("919222222222", catalogue)

    assert "Select size:" in reply
    assert "1kg" in reply
    assert wcf._get_state("919222222222")["step"] == "SELECT_SIZE"


def test_selecting_a_fixed_price_product_skips_the_size_step(db, catalogue, brownie):
    reply = _choose("919222222222", brownie)

    assert "Select size:" not in reply, "asked how many kilos of brownie"
    assert "Select flavor:" in reply
    state = wcf._get_state("919222222222")
    assert state["step"] == "SELECT_FLAVOR"
    assert state["size"] is None
    assert "/kg" not in reply


def test_fixed_price_summary_is_not_multiplied_by_a_size(db, catalogue, brownie):
    """A size in the state must not reach the multiplier for a fixed product."""
    wcf._set_state("919222222222", {
        "step": "SELECT_TIME",
        "product": {"id": brownie.id, "name": brownie.name,
                    "base_price": 400.0, "pricing_unit": "fixed"},
        "size": None,
        "flavor": {"name": "Belgian Chocolate", "extra_cost": 2000.0},
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_date": "2026-12-25",
    })
    reply = handle_customer_message("919222222222", "2", None)

    # 400 fixed + 2000 flavour. Never 400 x any multiplier.
    assert "*Total: ₹2,400*" in reply
    assert "Size:" not in reply, "invented a weight for a fixed-price item"


def test_a_stale_size_cannot_put_a_weight_back_on_a_fixed_price_item(db, brownie):
    """Defence in depth: the flow never sets one, but state can outlive a change."""
    db.add(SizeRule(name="5kg", multiplier=5.0, is_active=True))
    db.commit()

    wcf._set_state("919222222222", {
        "step": "SELECT_TIME",
        "product": {"id": brownie.id, "name": brownie.name,
                    "base_price": 400.0, "pricing_unit": "fixed"},
        "size": {"name": "5kg", "multiplier": 5.0},
        "flavor": {},
        "address": "Self Pickup",
        "delivery_date": "2026-12-25",
    })
    reply = handle_customer_message("919222222222", "2", None)

    assert "*Total: ₹400*" in reply, "a stale size multiplied a fixed-price item"
    assert "Size:" not in reply


def test_summary_total_matches_what_the_order_is_charged(db, catalogue, wa_secret):
    """
    The summary worked the total out as (base + flavour) x multiplier, but the
    engine multiplies only the base and adds the flavour after. A 2kg Rs 1,000
    cake with a Rs 2,000 flavour was quoted Rs 6,000 and billed Rs 4,000.
    """
    from app.models.order import Order

    db.add(SizeRule(name="2kg", multiplier=2.0, is_active=True))
    db.commit()

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    state = {
        "product": {"id": catalogue.id, "name": "Premium Cake",
                    "base_price": 1000.0, "pricing_unit": "kg"},
        "size": {"name": "2kg", "multiplier": 2.0},
        "flavor": {"name": "Belgian Chocolate", "extra_cost": 2000.0},
        "address": "Flat 1, Gomti Nagar, Lucknow",
        "delivery_date": "2026-12-25",
    }
    wcf._set_state("919222222222", {**state, "step": "SELECT_TIME"})
    summary = handle_customer_message("919222222222", "2", None)
    assert "*Total: ₹4,000*" in summary
    assert "6,000" not in summary

    handle_customer_message("919222222222", "CONFIRM", user)
    order = db.query(Order).order_by(Order.id.desc()).first()
    # The summary excludes delivery, which is quoted from the real order.
    assert order.subtotal == 4000.0


def test_fixed_price_order_line_carries_no_size(db, brownie, wa_secret):
    """"1kg" used to be written onto every order line, brownies included."""
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    wcf._set_state("919222222222", {
        "step": "CONFIRM",
        "product": {"id": brownie.id, "name": brownie.name,
                    "base_price": 400.0, "pricing_unit": "fixed"},
        "size": None,
        "flavor": {"name": ""},
        "address": "Self Pickup",
        "delivery_date": "2026-12-25",
        "time_hours": 14,
    })
    handle_customer_message("919222222222", "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.items[0].customization["size"] == ""
    assert order.total_price == 400.0, "a fixed-price brownie was multiplied"


def test_notification_text_does_not_invent_a_size(db, brownie):
    """_items_str defaulted a missing size to "1kg"."""
    from app.services.wa_notifications import _items_str
    from app.models.order import Order
    from app.models.order_item import OrderItem

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    order = Order(user_id=user.id, subtotal=400.0, total_price=400.0)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=brownie.id, quantity=2,
                     customization={"size": "", "flavor": ""}, price=800.0))
    db.commit()
    db.refresh(order)

    assert _items_str(order) == "Chocolate Walnut Brownie x2"


# ─── PRODUCT-SPECIFIC SIZES ──────────────────────────
# Some products sell in their own sizes rather than the global six: a cake that
# starts at 700g, one not sold under 1kg, a 1.3kg chiffon priced outright, and
# tea cakes sold as a loaf or a round at two unrelated prices. WhatsApp must
# offer those and only those, and must not invent a "1kg".

from app.models.product_option import ProductOption


def _with_options(db, name, price, unit, options):
    product = Product(name=name, category="test", base_price=price,
                      pricing_unit=unit, is_customizable=False, is_available=True)
    db.add(product)
    db.commit()
    db.refresh(product)
    for i, (label, kwargs) in enumerate(options, start=1):
        db.add(ProductOption(product_id=product.id, label=label, sort_order=i, **kwargs))
    db.commit()
    db.refresh(product)
    return product


def _reply(phone, text):
    return handle_customer_message(phone, text, None)


PHONE = "919222222222"


@pytest.fixture
def blueberry(db):
    return _with_options(db, "Blueberry Lemon Curd Cake", 2300.0, "kg", [
        ("700g", {"multiplier": 0.7}), ("1kg", {"multiplier": 1.0}),
        ("1.5kg", {"multiplier": 1.5}), ("2kg", {"multiplier": 2.0})])


@pytest.fixture
def coffee_cake(db):
    return _with_options(db, "Belgian Chocolate Coffee Cake With Cinnamon Roll",
                         2500.0, "kg", [
        ("1kg", {"multiplier": 1.0}), ("1.5kg", {"multiplier": 1.5}),
        ("2kg", {"multiplier": 2.0})])


@pytest.fixture
def tea_cake(db):
    return _with_options(db, "Orange Cardamom Crumble", 800.0, "fixed", [
        ("500g loaf", {"price": 800.0}), ("1kg round", {"price": 1700.0})])


def test_blueberry_offers_700g_and_never_500g(db, catalogue, blueberry):
    reply = _choose(PHONE, blueberry)

    assert "700g" in reply
    assert "500g" not in reply, "offered a weight this cake is not sold in"
    assert wcf._get_state(PHONE)["step"] == "SELECT_SIZE"
    assert [s["name"] for s in wcf._get_state(PHONE)["sizes"]] == \
        ["700g", "1kg", "1.5kg", "2kg"]


def test_coffee_cake_offers_1kg_and_never_500g(db, catalogue, coffee_cake):
    reply = _choose(PHONE, coffee_cake)

    assert "1kg" in reply and "1.5kg" in reply and "2kg" in reply
    assert "500g" not in reply
    assert [s["name"] for s in wcf._get_state(PHONE)["sizes"]] == ["1kg", "1.5kg", "2kg"]


def test_the_global_size_list_is_not_shown_for_an_option_product(db, catalogue, blueberry):
    """`catalogue` seeds a global 1kg SizeRule; it must not leak in."""
    # Deliberately not a weight: "5kg" would be a substring of the "1.5kg"
    # option and the assertion would pass or fail for the wrong reason.
    db.add(SizeRule(name="Party Platter", multiplier=5.0, is_active=True))
    db.commit()

    reply = _choose(PHONE, blueberry)

    assert "Party Platter" not in reply, "a global SizeRule leaked into a product's own list"
    # The offered list is exactly the product's own options, nothing appended.
    assert [s["name"] for s in wcf._get_state(PHONE)["sizes"]] == \
        ["700g", "1kg", "1.5kg", "2kg"]


def test_chiffon_offers_its_single_option_and_is_not_quoted_per_kg(db, catalogue):
    chiffon = _with_options(db, "Chiffon Fresh Fruit Milk Cake", 2400.0, "kg", [
        ("1.3kg", {"price": 2400.0})])

    reply = _choose(PHONE, chiffon)

    assert "1.3kg" in reply
    assert "/kg" not in reply, "quoted a 1.3kg cake as a per-kg price"
    assert "₹2,400" in reply


def test_tea_cake_shows_both_shapes_with_their_prices(db, catalogue, tea_cake):
    reply = _choose(PHONE, tea_cake)

    assert "500g loaf" in reply and "₹800" in reply
    assert "1kg round" in reply and "₹1,700" in reply
    assert "from ₹800" in reply, "a two-price product needs a 'from' headline"


def test_picking_an_option_carries_its_label_into_the_conversation(db, catalogue, blueberry):
    _choose(PHONE, blueberry)
    reply = _reply(PHONE, "1")          # 700g

    assert "Size: *700g*" in reply
    assert wcf._get_state(PHONE)["size"]["name"] == "700g"
    assert wcf._get_state(PHONE)["step"] == "SELECT_FLAVOR"


def test_an_unmatched_size_is_refused_rather_than_defaulted(db, catalogue, blueberry):
    _choose(PHONE, blueberry)
    reply = _reply(PHONE, "9")

    assert "Please select a size" in reply
    assert wcf._get_state(PHONE)["step"] == "SELECT_SIZE", "moved on without a size"
    assert "size" not in wcf._get_state(PHONE)


# ─── EXISTING BEHAVIOUR IS UNCHANGED ─────────────────

def test_a_normal_per_kg_product_still_uses_the_global_sizes(db, catalogue):
    """`catalogue`'s Premium Cake has no options."""
    reply = _choose(PHONE, catalogue)

    assert "Select size:" in reply
    assert [s["name"] for s in wcf._get_state(PHONE)["sizes"]] == ["1kg"]
    assert wcf._get_state(PHONE)["sizes"][0]["multiplier"] == 1.0


def test_a_fixed_product_with_no_options_still_skips_the_size_step(db, catalogue, brownie):
    reply = _choose(PHONE, brownie)

    assert "Select size:" not in reply
    assert "Select flavor:" in reply
    assert wcf._get_state(PHONE)["size"] is None


# ─── THE CHOICE REACHES PRICING AND THE ORDER ────────

def test_the_summary_quotes_the_option_price_from_the_pricing_engine(db, catalogue, tea_cake):
    _choose(PHONE, tea_cake)
    _reply(PHONE, "2")                  # 1kg round, Rs 1,700
    state = wcf._get_state(PHONE)
    wcf._set_state(PHONE, {**state, "step": "SELECT_TIME", "flavor": {},
                           "address": "Self Pickup", "delivery_date": "2026-12-25"})

    summary = _reply(PHONE, "2")

    assert "*Total: ₹1,700*" in summary
    assert "Size: 1kg round" in summary


def test_almond_with_and_without_egg_quote_different_prices(db, catalogue):
    with_egg = _with_options(db, "Almond Tea Cake - With Egg", 880.0, "fixed", [
        ("500g loaf", {"price": 880.0}), ("1kg round", {"price": 1850.0})])
    without = _with_options(db, "Almond Tea Cake - Without Egg", 850.0, "fixed", [
        ("500g loaf", {"price": 850.0}), ("1kg round", {"price": 1820.0})])

    quotes = {}
    for product in (with_egg, without):
        wcf._clear_state(PHONE)
        _choose(PHONE, product)
        _reply(PHONE, "2")              # 1kg round
        state = wcf._get_state(PHONE)
        wcf._set_state(PHONE, {**state, "step": "SELECT_TIME", "flavor": {},
                               "address": "Self Pickup", "delivery_date": "2026-12-25"})
        quotes[product.name] = _reply(PHONE, "2")

    assert "*Total: ₹1,850*" in quotes["Almond Tea Cake - With Egg"]
    assert "*Total: ₹1,820*" in quotes["Almond Tea Cake - Without Egg"]


def test_the_chosen_option_reaches_the_order_with_no_fake_1kg(db, catalogue, tea_cake, wa_secret):
    from app.models.order import Order

    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    user.whatsapp_opt_in = True
    db.commit()

    _choose(PHONE, tea_cake)
    _reply(PHONE, "1")                  # 500g loaf
    state = wcf._get_state(PHONE)
    assert state["size"]["name"] == "500g loaf"
    wcf._set_state(PHONE, {**state, "step": "CONFIRM", "flavor": {"name": ""},
                           "address": "Self Pickup", "delivery_date": "2026-12-25",
                           "time_hours": 14})

    handle_customer_message(PHONE, "CONFIRM", user)

    order = db.query(Order).order_by(Order.id.desc()).first()
    assert order.items[0].customization["size"] == "500g loaf"
    assert order.items[0].customization["size"] != "1kg"
    assert order.total_price == 800.0
    assert order.items[0].price_breakdown["option_label"] == "500g loaf"


def test_no_step_of_the_flow_invents_a_1kg_for_an_option_product(db, catalogue, tea_cake):
    """A fixed-price tea cake must never acquire a weight it is not sold by."""
    replies = [_choose(PHONE, tea_cake)]
    replies.append(_reply(PHONE, "1"))
    state = wcf._get_state(PHONE)
    wcf._set_state(PHONE, {**state, "step": "SELECT_TIME", "flavor": {},
                           "address": "Self Pickup", "delivery_date": "2026-12-25"})
    replies.append(_reply(PHONE, "2"))

    assert not any("1kg" in r and "1kg round" not in r for r in replies)
