"""
What the bot says to the people who run the shop.

Every message from a staff number that was not an exact command came back as
the command list — so "hi" from the owner's phone answered with a wall of
APPROVE/REJECT/PAID, and so did a real question. These tests pin the three
cases apart:

  a mistyped command  -> the grammar, never a guess at the order
  a greeting or help  -> who they are signed in as, plus the commands
  anything else       -> the assistant, same as any customer gets

The first of those is the one with teeth: commands move real cakes, so a verb
that did not parse must never be handed to a language model.
"""

from unittest.mock import patch

from app.models.user import UserRole
from app.services import wa_commands

from tests.conftest import make_user

import pytest


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


def _staff_reply(db, user, text):
    """Run the webhook's staff path and return what it would send."""
    from app.api.routes.webhook import _handle_staff
    return _handle_staff(db, user, text, "919554444462")


# ─── 1. COMMANDS STILL PARSE EXACTLY ──────────────────


def test_a_real_command_still_runs(db):
    admin = make_user(db, "Shriya", UserRole.ADMIN)

    reply = _staff_reply(db, admin, "ORDERS")

    # Whatever today's summary says, it is not the help text.
    assert "APPROVE <order no>" not in reply


@pytest.mark.parametrize("text", [
    "DONE 2 cakes for order 145",
    "APPROVE the one for tomorrow",
    "CANCEL because she changed her mind",
])
def test_a_mistyped_command_gets_the_grammar_not_the_assistant(db, text):
    """
    A verb that did not parse is a typo, not a question. It must never reach
    the model: an inferred "APPROVE 145" is indistinguishable from a typed one
    by the time it reaches the order service.
    """
    admin = make_user(db, "Shriya", UserRole.ADMIN)

    with patch("app.api.routes.webhook.handle_customer_message") as assistant:
        reply = _staff_reply(db, admin, text)

    assistant.assert_not_called()
    assert "Commands:" in reply


# ─── 2. A GREETING SAYS WHO THEY ARE ──────────────────


@pytest.mark.parametrize("text", ["hi", "Hello", "HEY", "namaste", "good morning"])
def test_a_greeting_answers_with_who_and_what(db, text):
    admin = make_user(db, "Shriya Mahendru", UserRole.ADMIN)

    reply = _staff_reply(db, admin, text)

    assert "Shriya" in reply
    assert "admin" in reply
    assert "APPROVE <order no>" in reply
    # The thing the old reply never said.
    assert "ask me anything" in reply.lower()


def test_asking_for_help_lists_the_commands(db):
    baker = make_user(db, "Rahul", UserRole.BAKER)

    reply = _staff_reply(db, baker, "help")

    assert "START <order no>" in reply
    assert "DONE <order no>" in reply


def test_each_role_sees_only_its_own_commands(db):
    rider = make_user(db, "Aman", UserRole.RIDER)

    reply = _staff_reply(db, rider, "hi")

    assert "PICKED <order no>" in reply
    assert "APPROVE" not in reply


# ─── 3. A QUESTION REACHES THE ASSISTANT ──────────────


@pytest.mark.parametrize("text", [
    "do you have 5kg cakes?",
    "what is the price of a chocolate cake",
    "Cake above 3.5 kg",
    "kya aaj delivery ho sakti hai",
])
def test_a_question_goes_to_the_assistant(db, text):
    """The case from the owner's own phone: a question, answered."""
    admin = make_user(db, "Shriya", UserRole.ADMIN)

    with patch("app.api.routes.webhook.handle_customer_message",
               return_value="a real answer") as assistant:
        reply = _staff_reply(db, admin, text)

    assistant.assert_called_once()
    assert reply == "a real answer"
    assert "Commands:" not in reply


def test_staff_are_not_handed_the_owners_number(db):
    """
    Escalation exists to put a customer in touch with Shriya. Sending it to
    Shriya — or to a baker — is noise at best and her own number at worst.
    """
    from app.services.wa_customer_flow import handle_customer_message

    admin = make_user(db, "Shriya", UserRole.ADMIN)
    with patch("app.services.wa_customer_flow._reply_to_customer",
               return_value="Sure."):
        with patch("app.services.wa_escalation.augment") as escalate:
            reply = handle_customer_message("919554444462", "I need a 6kg cake", admin)

    escalate.assert_not_called()
    assert reply == "Sure."


def test_a_customer_still_gets_the_escalation(db):
    """The staff exemption must not switch it off for everyone."""
    from app.services.wa_customer_flow import handle_customer_message

    customer = make_user(db, "Priya", UserRole.CUSTOMER)
    with patch("app.services.wa_customer_flow._reply_to_customer",
               return_value="Sure."):
        with patch("app.services.wa_escalation.augment",
                   return_value="Sure. + contact") as escalate:
            reply = handle_customer_message("919000000001", "I need a 6kg cake", customer)

    escalate.assert_called_once()
    assert reply == "Sure. + contact"


# ─── 4. THE HELPERS THEMSELVES ────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("DONE 145", True), ("done", True), ("QUEUE", True),
    ("do you deliver", False), ("hi", False), ("", False),
    ("startled by the price", False),   # not the verb START
])
def test_looks_like_command_reads_the_first_word_only(text, expected):
    assert wa_commands.looks_like_command(text, UserRole.BAKER) is expected
