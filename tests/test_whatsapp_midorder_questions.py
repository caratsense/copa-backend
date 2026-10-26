"""
What happens when a customer says something mid-order that isn't an answer.

From a live test on the owner's phone: the bot was part-way through an order
and the next message was "cake above 3.5kg". The delivery-address step takes
whatever arrives, so that sentence became the delivery address, rode through
area/date/time, and arrived in the order summary as:

    Delivery: cake with 3.5 kg
    Total: ₹2,400          (a 1kg cake, for someone asking about 5kg+)

Three separate failures, pinned here:

  1. a question about a cake the shop cannot price must be answered, not
     stored — whatever step the conversation is on
  2. an order nobody has touched for twenty minutes is not still in progress
  3. the address step must not accept something the model read as a question
"""

import time
from unittest.mock import patch

from app.models.user import UserRole
from app.services import wa_customer_flow as flow
from app.services import wa_escalation

from tests.conftest import make_user

import pytest


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """No Redis: the in-memory store, emptied between tests."""
    monkeypatch.setattr(flow, "_client", None)
    monkeypatch.setattr(flow, "_get_redis", lambda: None)
    flow._memory.clear()
    flow._escalated_memory.clear()
    yield
    flow._memory.clear()
    flow._escalated_memory.clear()


PHONE = "919000000123"


def _mid_order(step="DELIVERY_ADDRESS", **extra):
    """Put a conversation part-way through an order, as of now."""
    flow._set_state(PHONE, {
        "step": step,
        "product": {"id": 1, "name": "Belgian Chocolate Cake", "base_price": 2400.0,
                    "pricing_unit": "kg", "options": []},
        "size": {"id": 2, "name": "1kg", "multiplier": 1.0},
        "flavor": {"id": 3, "name": "Vanilla Pineapple", "extra_cost": 0.0},
        **extra,
    })


# ─── 1. THE QUESTION THAT BECAME AN ADDRESS ───────────


@pytest.mark.parametrize("message", [
    "cake above 3.5kg",
    "Cake above 3.5 kg",
    "do you make 5kg cakes",
    "I want a 10 kg cake",
])
def test_an_oversized_cake_is_answered_not_stored_as_the_address(db, message):
    _mid_order()

    reply = flow._reply_to_customer(PHONE, message, None)

    assert "quoted by hand" in reply
    # The address must not have been accepted...
    assert flow._get_state(PHONE).get("address") is None
    # ...and the conversation must not have moved on to picking an area.
    assert flow._get_state(PHONE)["step"] == "DELIVERY_ADDRESS"


def test_the_order_is_not_thrown_away_by_the_interruption(db):
    _mid_order()

    reply = flow._reply_to_customer(PHONE, "what about a 6kg cake?", None)

    state = flow._get_state(PHONE)
    assert state["product"]["name"] == "Belgian Chocolate Cake"
    assert "still here" in reply
    assert "delivery address" in reply


def test_a_complaint_mid_order_reaches_a_person(db):
    _mid_order(step="SELECT_DATE")

    reply = flow._reply_to_customer(PHONE, "my last cake arrived damaged", None)

    assert "sorry" in reply.lower()
    assert "delivery date" in reply          # put back where they were


def test_asking_for_a_person_mid_order_is_honoured(db):
    _mid_order(step="SELECT_AREA")

    reply = flow._reply_to_customer(PHONE, "can I talk to someone", None)

    assert "get back to you" in reply.lower()


def test_an_ordinary_answer_is_still_taken_as_the_address(db):
    """The interception must not break ordering."""
    _mid_order()

    with patch.object(flow, "parse_message", return_value={"action": "SET_ADDRESS"}):
        flow._reply_to_customer(PHONE, "12 Hazratganj, near the clock tower", None)

    assert flow._get_state(PHONE)["address"] == "12 Hazratganj, near the clock tower"


def test_a_festive_box_is_not_intercepted(db):
    """
    The hampers are orderable, so "Full House" mid-order is an ANSWER, not a
    question needing a person. Only the three that cannot be answers interrupt.
    """
    assert wa_escalation.reason_to_interrupt("Full House please") is None
    assert wa_escalation.reason_to_interrupt("do you have hampers") is None
    assert wa_escalation.reason_to_interrupt("a 5kg cake") is not None


# ─── 2. A CONVERSATION NOBODY IS HAVING ANY MORE ──────


def test_an_abandoned_order_does_not_swallow_a_later_question(db):
    _mid_order()
    # Twenty minutes and a second later, the customer asks something new.
    stale = dict(flow._memory[PHONE])
    stale["touched_at"] = time.time() - (flow.STALE_AFTER_SECONDS + 1)
    flow._memory[PHONE] = stale

    with patch.object(flow, "parse_message",
                      return_value={"action": "CONVERSATIONAL", "reply": "We open at 9."}):
        reply = flow._reply_to_customer(PHONE, "what time do you open?", None)

    assert "We open at 9." in reply
    assert flow._get_state(PHONE).get("address") is None


def test_an_order_in_progress_is_not_dropped_too_early(db):
    _mid_order()
    recent = dict(flow._memory[PHONE])
    recent["touched_at"] = time.time() - 60      # a minute ago
    flow._memory[PHONE] = recent

    with patch.object(flow, "parse_message", return_value={"action": "SET_ADDRESS"}):
        flow._reply_to_customer(PHONE, "14 Gomti Nagar", None)

    assert flow._get_state(PHONE)["address"] == "14 Gomti Nagar"


def test_state_written_before_this_change_still_resumes(db):
    """No touched_at (an older conversation in Redis) must not be dropped."""
    flow._memory[PHONE] = {"step": "DELIVERY_ADDRESS", "product": {"name": "X"}}

    assert flow._is_stale(flow._memory[PHONE]) is False


# ─── 3. A QUESTION IS NOT AN ADDRESS ──────────────────


def test_a_question_at_the_address_step_is_answered_and_re_asked(db):
    _mid_order()

    with patch.object(flow, "parse_message",
                      return_value={"action": "CONVERSATIONAL",
                                    "reply": "Yes, we deliver across Lucknow."}):
        reply = flow._reply_to_customer(PHONE, "do you deliver to Aliganj?", None)

    assert "Yes, we deliver across Lucknow." in reply
    assert "your delivery address" in reply       # put back where they were
    assert flow._get_state(PHONE).get("address") is None


def test_pickup_still_works_at_the_address_step(db):
    _mid_order()

    with patch.object(flow, "parse_message", return_value={"action": "UNKNOWN"}):
        flow._reply_to_customer(PHONE, "pickup", None)

    assert flow._get_state(PHONE)["address"] == "Self Pickup"


# ─── 4. STAFF GET THE ANSWER, NOT THE OWNER'S NUMBER ──


def test_staff_asking_mid_order_are_not_given_the_contact(db):
    admin = make_user(db, "Shriya", UserRole.ADMIN)
    _mid_order()

    reply = flow._reply_to_customer(PHONE, "cake above 3.5kg", admin)

    assert "quoted by hand" in reply
    assert "9554444462" not in reply.replace(" ", "")
