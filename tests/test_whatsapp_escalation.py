"""
Who gets the owner's number, and who does not.

The client's instruction was specific: share Shriya's contact for a cake over
3.5kg, the festive boxes, a grazing table, a wedding or bulk order, a
complaint, or somebody asking for a person — and "don't share it to everyone
that comes to the bot".

Two things have to hold for that to be true, and these tests hold both ends:

  * the number is NOT in the assistant's prompt, so the model cannot recite it
    to whoever asks — which is what it did before this change
  * it IS appended, by code, to the replies that have earned it

The second without the first is theatre: a prompt carrying the number will
hand it over the first time someone asks nicely.
"""

import time

from app.models.menu_section import MenuSection
from app.models.product import Product
from app.models.site_settings import SiteSettings
from app.services import wa_escalation
from app.services.gemini_parser import business_info

import pytest


SHOP_NUMBER = "+919554444462"


@pytest.fixture
def brand_phone(db):
    db.add(SiteSettings(key="brand_phone", value=SHOP_NUMBER))
    db.commit()
    return SHOP_NUMBER


@pytest.fixture
def seasonal(db):
    """The festive category, as the October cards define it."""
    section = MenuSection(name="Seasonal", sort_order=16, is_active=True)
    db.add(section)
    db.commit()
    db.refresh(section)
    db.add(Product(name="Queen Of Hearts", category="seasonal", base_price=1799.0,
                   pricing_unit="fixed", is_available=True, section_id=section.id))
    db.commit()
    return section


# ─── 1. THE PROMPT MUST NOT CARRY IT ──────────────────


def test_the_assistants_prompt_has_no_phone_number():
    """
    It used to. A model given a number recites it on request, and no
    instruction wrapped around it is a guarantee.
    """
    info = business_info()

    assert "9554444462" not in info.replace(" ", "")
    assert "955 444 4462" not in info


def test_the_assistant_is_told_it_has_no_number_to_give(db):
    assert "do not" in business_info().lower() or "not have a phone" in business_info().lower()


# ─── 2. WHAT EARNS IT ─────────────────────────────────


@pytest.mark.parametrize("message,expected", [
    ("I need a 5kg cake for Saturday", "a cake over 3.5kg"),
    ("do you make 10 kg cakes?", "a cake over 3.5kg"),
    ("4.5kgs chocolate please", "a cake over 3.5kg"),
    ("Can I order a grazing table for 40 people?", "a grazing table"),
    ("We need a wedding cake", "a wedding, corporate or bulk order"),
    ("corporate gifting for 50 staff", "a wedding, corporate or bulk order"),
    ("my cake arrived damaged", "a problem with an order"),
    ("I want a refund", "a problem with an order"),
    ("mera cake kharab tha", "a problem with an order"),
    ("can I talk to someone please", "a request to speak to someone"),
    ("Do you have diwali hampers?", "a festive hamper"),
])
def test_these_need_a_person(message, expected, db, seasonal):
    assert wa_escalation.reason_to_escalate(message, db) == expected


@pytest.mark.parametrize("message", [
    "hi",
    "what are your prices?",
    "I want a 1kg chocolate cake",
    "do you deliver to Gomti Nagar?",
    "2kg vanilla for tomorrow",
    "what flavours do you have",
    "what is your number",          # asking is not qualifying
    "give me your phone number",
    "3.5kg please",                 # the ceiling itself is still orderable
])
def test_these_do_not(message, db, seasonal):
    assert wa_escalation.reason_to_escalate(message, db) is None


def test_a_festive_box_by_name_needs_a_person(db, seasonal):
    """Read from the Seasonal category, so next season's boxes work too."""
    assert wa_escalation.reason_to_escalate(
        "how much is the Queen of Hearts?", db) == "one of our festive boxes"


def test_a_box_that_is_no_longer_seasonal_stops_escalating(db, seasonal):
    """The list is the category, not a constant, so taking it down is enough."""
    product = db.query(Product).filter(Product.name == "Queen Of Hearts").first()
    product.section_id = None
    db.commit()

    assert wa_escalation.reason_to_escalate("Queen of Hearts please", db) is None


# ─── 3. WHAT GOES OUT ─────────────────────────────────


def _sent():
    """A fresh in-memory window, so tests do not share one."""
    seen: dict = {}
    return (lambda phone: (time.time() - seen[phone]) < wa_escalation.REPEAT_AFTER_SECONDS
            if phone in seen else False,
            lambda phone: seen.__setitem__(phone, time.time()))


def test_the_number_is_appended_when_it_is_earned(db, brand_phone, seasonal):
    already, mark = _sent()

    out = wa_escalation.augment("919000000001", "I need a 6kg cake",
                                "Here's what we offer.", db, already, mark)

    assert "Here's what we offer." in out
    assert "9554444462" in out.replace(" ", "")
    assert "Shriya" in out


def test_an_ordinary_question_gets_the_plain_reply(db, brand_phone, seasonal):
    already, mark = _sent()

    out = wa_escalation.augment("919000000002", "what are your prices?",
                                "Cakes start at ₹2000/kg.", db, already, mark)

    assert out == "Cakes start at ₹2000/kg."
    assert "9554444462" not in out.replace(" ", "")


def test_asking_for_the_number_does_not_get_the_number(db, brand_phone, seasonal):
    """The whole point of the instruction: it goes out on need, not on demand."""
    already, mark = _sent()

    out = wa_escalation.augment("919000000003", "what is your contact number?",
                                "You can order on our website.", db, already, mark)

    assert "9554444462" not in out.replace(" ", "")


def test_it_is_not_repeated_in_every_reply(db, brand_phone, seasonal):
    """A back-and-forth about one 5kg cake should not recite it each time."""
    already, mark = _sent()

    first = wa_escalation.augment("919000000004", "5kg cake please", "Sure.", db, already, mark)
    second = wa_escalation.augment("919000000004", "and 6kg?", "Sure.", db, already, mark)

    assert "9554444462" in first.replace(" ", "")
    assert "9554444462" not in second.replace(" ", "")


def test_two_customers_do_not_share_a_window(db, brand_phone, seasonal):
    already, mark = _sent()

    wa_escalation.augment("919000000005", "wedding cake", "Sure.", db, already, mark)
    other = wa_escalation.augment("919000000006", "wedding cake", "Sure.", db, already, mark)

    assert "9554444462" in other.replace(" ", "")


def test_with_no_number_on_record_the_reply_is_unchanged(db, seasonal):
    """
    No brand phone and no admin: say nothing rather than promise a contact
    that does not exist.
    """
    already, mark = _sent()

    out = wa_escalation.augment("919000000007", "5kg cake", "Sure.", db, already, mark)

    assert out == "Sure."


def test_the_number_comes_from_the_database_not_the_code(db, seasonal):
    """Changing it in the admin screen changes what the bot gives out."""
    db.add(SiteSettings(key="brand_phone", value="+919812345678"))
    db.commit()
    already, mark = _sent()

    out = wa_escalation.augment("919000000008", "5kg cake", "Sure.", db, already, mark)

    assert "919812345678" in out.replace(" ", "")


def test_a_failure_never_costs_the_customer_their_reply(db, brand_phone, seasonal):
    """The contact is an addition; losing it must not lose the answer."""
    def explode(_phone):
        raise RuntimeError("redis is down")

    out = wa_escalation.augment("919000000009", "5kg cake", "Sure.", db, explode, explode)

    assert out == "Sure."
