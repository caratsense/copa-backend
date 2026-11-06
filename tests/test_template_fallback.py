"""
A template Meta will not send becomes a plain message, rather than silence.

WHAT WENT WRONG
---------------
Six templates had never been created in the Meta console and had been failing
since the day they were written:

    admin_proof_ready, order_ready_for_pickup, order_collected,
    rider_new_delivery, order_not_collected, order_not_collected_admin

Meta answers "(#132001) Template name does not exist in the translation", the
error goes to the log, and the customer is told nothing at all. Thirteen
messages in production had gone that way - including every single
order_ready_for_pickup, which is THE message for a shop taking collection
only.

WHAT THIS IS AND IS NOT
-----------------------
It is a floor, not a fix. A plain message only reaches somebody who has
written to the business in the last 24 hours, so the templates still have to
be approved. It does mean the common case - a customer who ordered an hour
ago - hears something instead of nothing.
"""

import pytest

from app.services.wa_templates import TEMPLATES, fallback_for
from app.services.whatsapp_sender import send_template

FAILING_IN_PRODUCTION = (
    "admin_proof_ready",
    "order_ready_for_pickup",
    "order_collected",
    "rider_new_delivery",
    "order_not_collected",
    "order_not_collected_admin",
)


def test_every_template_that_failed_in_production_now_has_a_fallback():
    missing = [k for k in FAILING_IN_PRODUCTION if not TEMPLATES[k].fallback]
    assert not missing, f"no plain-text version for: {missing}"


def test_a_fallback_fills_its_slots_from_the_same_parameters():
    text = fallback_for("order_ready_for_pickup",
                        ["Priya", 42, "Padma iccha apartment, G1", "today 4-8 PM"])
    assert "Priya" in text and "#42" in text
    assert "Padma iccha apartment, G1" in text
    assert "{" not in text, "an unfilled slot leaked into the message"


def test_a_template_with_no_fallback_sends_nothing_rather_than_a_stub():
    assert fallback_for("order_confirmation", ["x"]) == ""
    assert fallback_for("not_a_template_at_all", []) == ""


def test_too_few_parameters_sends_nothing_rather_than_a_gap():
    """A message with a hole in it is worse than the log entry it replaces."""
    assert fallback_for("order_ready_for_pickup", ["Priya"]) == ""


def test_a_refused_template_goes_out_as_text(sent_messages, monkeypatch):
    """The whole point: Meta says no, the customer still hears."""
    from app.services import whatsapp_sender

    def refuse_templates(payload):
        if payload.get("type") == "template":
            return {"error": {"code": 132001,
                              "message": "(#132001) Template name does not exist"}}
        sent_messages.append(payload)
        return {"messages": [{"id": "wamid.TEXT"}]}

    monkeypatch.setattr(whatsapp_sender, "_send", refuse_templates)

    send_template("919999999999", "order_ready_for_pickup",
                  ["Priya", 42, "Padma iccha apartment, G1", "today 4-8 PM"])

    assert len(sent_messages) == 1
    assert sent_messages[0]["type"] == "text"
    assert "ready to collect" in sent_messages[0]["text"]["body"]


def test_an_accepted_template_does_not_also_send_text(sent_messages):
    """The fallback must not double up when the template worked."""
    send_template("919999999999", "order_ready_for_pickup",
                  ["Priya", 42, "Padma iccha apartment, G1", "today 4-8 PM"])

    assert [m["type"] for m in sent_messages] == ["template"]
