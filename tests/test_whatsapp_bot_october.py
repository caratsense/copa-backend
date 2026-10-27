"""
What the colleague's screenshot showed the bot doing, and what it does now.

Four separate failures in one short conversation, all pinned here:

  1. the first message became the customer's NAME, because the website's
     WhatsApp links open with a sentence already typed
  2. "cake above 3.5kg" at an idle conversation started an order instead of
     passing it to a person
  3. that order listed the whole catalogue - sixty-two numbered lines
  4. four of those lines were the hidden per-kg pricing rows
"""

from unittest.mock import patch

import pytest

from app.api.routes import webhook
from app.models.menu_section import MenuSection
from app.models.product import PRICING_ROW_TAG, Product
from app.services import wa_customer_flow as flow


PHONE = "919000000999"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """No Redis: the in-memory stores, emptied between tests."""
    monkeypatch.setattr(flow, "_client", None)
    monkeypatch.setattr(flow, "_get_redis", lambda: None)
    flow._memory.clear()
    flow._escalated_memory.clear()
    webhook._signup_memory.clear()
    yield
    flow._memory.clear()
    flow._escalated_memory.clear()
    webhook._signup_memory.clear()


def _catalogue(db):
    """Two categories, one of which holds a pricing row."""
    cakes = MenuSection(name="Chocolate Celebration Cakes", sort_order=1)
    breads = MenuSection(name="Breads & Bun Collection", sort_order=2)
    db.add_all([cakes, breads])
    db.flush()
    rows = [
        Product(name="Tiramisu Cake", category="cakes", base_price=2400.0,
                pricing_unit="kg", section_id=cakes.id, is_available=True, tags=[]),
        Product(name="Belgian Chocolate Cake", category="cakes", base_price=2200.0,
                pricing_unit="kg", section_id=cakes.id, is_available=True, tags=[]),
        # The thing the website hides and the bot was still selling.
        Product(name="Chocolate Base Cake", category="cakes", base_price=2200.0,
                pricing_unit="kg", section_id=cakes.id, is_available=True,
                tags=[PRICING_ROW_TAG]),
        Product(name="Pav", category="breads", base_price=35.0,
                pricing_unit="fixed", section_id=breads.id, is_available=True, tags=[]),
    ]
    db.add_all(rows)
    db.commit()
    return rows


# ─── 1. THE SENTENCE THAT BECAME A NAME ───────────────


@pytest.mark.parametrize("opening", [
    "Hi Cake O'Clock — I'd like to ask about Wedding.",
    "Hi Cake O'Clock - I'd like to ask about a grazing table.",
    "do you have 5kg cakes?",
    "hello",
    "Price list please",
])
def test_an_opening_message_is_never_taken_as_the_name(db, opening):
    reply = webhook._handle_new_user(db, PHONE, opening)

    assert "what should we call you" in reply.lower()
    # Nothing has been stored under that sentence...
    assert webhook._signup_get(PHONE)["step"] == "NAME"
    # ...and the bot has not greeted them by it.
    assert opening.strip(".") not in reply


def test_the_name_is_asked_for_and_then_read(db):
    webhook._handle_new_user(db, PHONE, "Hi, I want to order a cake")
    reply = webhook._handle_new_user(db, PHONE, "Priya")

    assert "Thanks, Priya" in reply
    assert "YES" in reply
    pending = webhook._signup_get(PHONE)
    assert pending["step"] == "CONSENT"
    assert pending["name"] == "Priya"
    # What they came for is kept, not spent on the greeting.
    assert pending["asked"] == "Hi, I want to order a cake"


def test_yes_at_the_name_step_is_not_a_name(db):
    """The old flow registered the consent reply itself as the customer."""
    webhook._handle_new_user(db, PHONE, "hi")
    reply = webhook._handle_new_user(db, PHONE, "YES")

    assert "just your name" in reply.lower()
    assert webhook._signup_get(PHONE)["step"] == "NAME"


def test_nobody_is_trapped_on_the_name_question(db):
    webhook._handle_new_user(db, PHONE, "hi")
    webhook._handle_new_user(db, PHONE, "I want a cake")      # first refusal
    reply = webhook._handle_new_user(db, PHONE, "for tomorrow")  # second

    assert "YES" in reply                                      # moved on anyway
    assert webhook._signup_get(PHONE)["step"] == "CONSENT"


def test_skip_is_honoured(db):
    webhook._handle_new_user(db, PHONE, "hi")
    reply = webhook._handle_new_user(db, PHONE, "skip")

    assert "YES" in reply
    assert webhook._signup_get(PHONE)["name"] == "WhatsApp Customer"


def test_a_wedding_enquiry_from_a_new_number_reaches_a_person(db):
    """
    The client's rule applies to the first message too. A brand new number
    asking about a wedding used to get nothing but "share your name".
    """
    from app.models.site_settings import SiteSettings
    db.add(SiteSettings(key="brand_phone", value="+919554444462"))
    db.commit()

    reply = webhook._handle_new_user(db, PHONE, "Hi — I'd like to ask about Wedding.")

    assert "9554444462" in reply.replace(" ", "")
    assert "what should we call you" in reply.lower()


def test_the_opening_question_is_answered_once_they_are_in(db):
    _catalogue(db)
    webhook._handle_new_user(db, PHONE, "what time do you open?")
    webhook._handle_new_user(db, PHONE, "Priya")

    with patch.object(flow, "parse_message",
                      return_value={"action": "CONVERSATIONAL", "reply": "We open at 9."}):
        reply = webhook._handle_new_user(db, PHONE, "YES")

    assert "Welcome, Priya" in reply
    assert "We open at 9." in reply


def test_a_name_is_not_answered_back_as_a_question(db):
    """The stored opening is only replayed when it was not the name itself."""
    webhook._handle_new_user(db, PHONE, "Priya")      # they led with their name
    reply = webhook._handle_new_user(db, PHONE, "Priya")

    assert "Thanks, Priya" in reply


# ─── 2. THE OVERSIZE QUESTION THAT STARTED AN ORDER ───


def test_an_oversize_cake_at_an_idle_conversation_is_passed_on(db):
    _catalogue(db)

    with patch.object(flow, "parse_message", return_value={"action": "START_ORDER"}):
        reply = flow._reply_to_customer(PHONE, "cake above 3.5 kg", None)

    assert "quoted by hand" in reply
    # Not the catalogue.
    assert "Tiramisu Cake" not in reply
    assert "Select" not in reply


# ─── 3. SIXTY-TWO LINES ───────────────────────────────


def test_an_order_starts_with_the_categories(db):
    _catalogue(db)

    with patch.object(flow, "parse_message", return_value={"action": "START_ORDER"}):
        reply = flow._reply_to_customer(PHONE, "I want to order", None)

    assert "Chocolate Celebration Cakes (2)" in reply
    assert "Breads & Bun Collection (1)" in reply
    assert "Tiramisu Cake" not in reply           # products come after
    assert flow._get_state(PHONE)["step"] == "SELECT_CATEGORY"


def test_a_category_opens_its_own_products(db):
    _catalogue(db)
    flow._set_state(PHONE, {
        "step": "SELECT_CATEGORY",
        "categories": ["Chocolate Celebration Cakes", "Breads & Bun Collection"],
    })

    with patch.object(flow, "parse_message", return_value={"action": "SELECT_OPTION", "value": 1}):
        reply = flow._reply_to_customer(PHONE, "1", None)

    assert "Tiramisu Cake" in reply
    assert "Pav" not in reply
    state = flow._get_state(PHONE)
    assert state["step"] == "SELECT_PRODUCT"
    assert [p["name"] for p in state["products"]] == ["Belgian Chocolate Cake", "Tiramisu Cake"]


def test_naming_a_cake_skips_the_category_step(db):
    _catalogue(db)
    flow._set_state(PHONE, {"step": "SELECT_CATEGORY",
                            "categories": ["Chocolate Celebration Cakes"]})

    with patch.object(flow, "parse_message", return_value={"action": "UNKNOWN"}):
        reply = flow._reply_to_customer(PHONE, "Tiramisu Cake", None)

    assert "Select size" in reply
    assert flow._get_state(PHONE)["product"]["name"] == "Tiramisu Cake"


def test_zero_goes_back_to_the_categories(db):
    rows = _catalogue(db)
    flow._set_state(PHONE, {
        "step": "SELECT_PRODUCT",
        "products": [{"id": rows[0].id, "name": "Tiramisu Cake", "base_price": 2400.0,
                      "pricing_unit": "kg", "options": []}],
        "category": "Chocolate Celebration Cakes",
    })

    with patch.object(flow, "parse_message", return_value={"action": "UNKNOWN"}):
        reply = flow._reply_to_customer(PHONE, "0", None)

    assert "What are you looking for?" in reply
    assert flow._get_state(PHONE)["step"] == "SELECT_CATEGORY"


@pytest.mark.parametrize("step,state", [
    ("SELECT_CATEGORY", {"categories": ["Chocolate Celebration Cakes"]}),
    ("SELECT_PRODUCT", {"products": [], "category": "Chocolate Celebration Cakes"}),
])
def test_cancel_works_at_the_steps_that_tell_you_to_use_it(db, step, state):
    """
    Every step answers "pick one of these" when it cannot read the message,
    and *cancel* was reaching that answer rather than the cancel branch - the
    word the bot's own reply tells people to send to get out.
    """
    _catalogue(db)
    flow._set_state(PHONE, {"step": step, **state})

    reply = flow._reply_to_customer(PHONE, "cancel", None)

    assert "cancelled" in reply.lower()
    assert flow._get_state(PHONE)["step"] == "IDLE"


def test_the_menu_command_lists_categories_not_every_cake(db):
    _catalogue(db)

    with patch.object(flow, "parse_message", return_value={"action": "VIEW_MENU"}):
        reply = flow._reply_to_customer(PHONE, "menu", None)

    assert "Chocolate Celebration Cakes (2)" in reply
    assert "Tiramisu Cake" not in reply


# ─── 4. THE PRICING ROWS THE WEBSITE HIDES ────────────


def test_a_pricing_row_is_not_offered_for_sale(db):
    _catalogue(db)

    names = [p["name"] for p in flow._catalogue(db)]

    assert "Chocolate Base Cake" not in names
    assert "Tiramisu Cake" in names


def test_a_hidden_category_is_not_sold_by_the_bot(db):
    """
    The website hides a category by deactivating its section. The bot read
    products directly and kept selling them, so the two disagreed about what
    was on the menu.
    """
    from app.models.menu_section import MenuSection

    _catalogue(db)
    cakes = db.query(MenuSection).filter(
        MenuSection.name == "Chocolate Celebration Cakes").first()
    assert "Tiramisu Cake" in [p["name"] for p in flow._catalogue(db)]

    cakes.is_active = False
    db.commit()

    names = [p["name"] for p in flow._catalogue(db)]
    assert "Tiramisu Cake" not in names
    assert "Pav" in names, "an unrelated category went with it"


def test_a_conversation_cannot_outlive_the_menu_it_started_with(db):
    """
    The product list used to be cached inside the conversation, which lives
    an hour. A cake repriced mid-conversation was still orderable at the old
    price; a deploy that changed the menu kept serving the old one.
    """
    rows = _catalogue(db)
    tiramisu = next(r for r in rows if r.name == "Tiramisu Cake")
    flow._set_state(PHONE, {
        "step": "SELECT_PRODUCT",
        "category": "Chocolate Celebration Cakes",
        "products": [{"id": tiramisu.id, "name": "Tiramisu Cake", "base_price": 1.0,
                      "pricing_unit": "kg", "options": []}],
    })

    tiramisu.base_price = 2500.0
    db.commit()

    with patch.object(flow, "parse_message", return_value={"action": "UNKNOWN"}):
        reply = flow._reply_to_customer(PHONE, "Tiramisu Cake", None)

    # The live price, not the Rs 1 frozen into the conversation.
    assert "2,500" in reply

    # And "1" reaches the live category, which holds both cakes, rather than
    # the single-item list the conversation was carrying.
    flow._set_state(PHONE, {
        "step": "SELECT_PRODUCT",
        "category": "Chocolate Celebration Cakes",
        "products": [{"id": tiramisu.id, "name": "Tiramisu Cake", "base_price": 1.0,
                      "pricing_unit": "kg", "options": []}],
    })
    with patch.object(flow, "parse_message", return_value={"action": "UNKNOWN"}):
        by_number = flow._reply_to_customer(PHONE, "1", None)
    assert "Belgian Chocolate Cake" in by_number


def test_a_product_with_no_price_yet_is_not_offered_either(db):
    from app.models.product import PLACEHOLDER_PRICE_TAG
    _catalogue(db)
    db.add(Product(name="Mystery Cake", category="cakes", base_price=1.0,
                   pricing_unit="kg", is_available=True,
                   tags=[PLACEHOLDER_PRICE_TAG]))
    db.commit()

    assert "Mystery Cake" not in [p["name"] for p in flow._catalogue(db)]
