"""
The shapes Meta accepts in a template's body parameters.

WHAT THIS COST
--------------
Meta validates every parameter and refuses the WHOLE message if one of them
is wrong. The recipient gets nothing and the only trace is a code in a log.

Two rider notifications were lost that way in production, both
"(#131008) Required parameter is missing", both because the maps link was an
empty string — the orders were Self Pickup and had no address to build a link
from. The rider was told nothing about a delivery that, separately, should
never have been theirs.

The other rule is newlines: a parameter may not contain one, nor a tab, nor a
long run of spaces. That one has not fired yet and is now a matter of time —
order notes became multi-line when a design reference started being written on
its own line, and the notes reach the baker as a parameter.

A dash in a message is a worse message. An empty one is no message at all.
"""

import pytest

from app.services.whatsapp_sender import EMPTY_PARAM, _body_component, _param


def _texts(params):
    (component,) = _body_component(params)
    assert component["type"] == "body"
    return [p["text"] for p in component["parameters"]]


# ─── THE FAILURE THAT HAPPENED ────────────────────────


def test_an_empty_parameter_becomes_a_dash():
    """
    The real payload, from the two rider notifications Meta refused: order 26,
    a customer, a phone, "Self Pickup" where the address goes, and nothing at
    all where the maps link goes.
    """
    sent = _texts(["26", "Dhruv Kaushik", "+917404954207", "Self Pickup", "", "1.00"])

    assert sent[4] == EMPTY_PARAM
    assert "" not in sent
    assert sent[:4] == ["26", "Dhruv Kaushik", "+917404954207", "Self Pickup"]


def test_none_is_treated_as_empty():
    """A missing value arrives as None as readily as "" — same outcome."""
    assert _texts(["7", None])[1] == EMPTY_PARAM


def test_whitespace_only_is_empty():
    assert _texts(["7", "   "])[1] == EMPTY_PARAM


# ─── THE ONE COMING ───────────────────────────────────


def test_a_multi_line_note_is_flattened():
    """
    What the baker's "special instructions" now looks like: the customer's own
    note, then the design reference on its own line. Sent as-is it would take
    the whole message down with it.
    """
    note = ("Please keep it chilled\n"
            "Design reference for Chocolate Base Cake: it did not upload.")

    (flat,) = _texts([note])
    assert "\n" not in flat
    assert flat.startswith("Please keep it chilled Design reference")


@pytest.mark.parametrize("raw", ["a\tb", "a\r\nb", "a    b", "a\n\n\nb"])
def test_every_kind_of_whitespace_collapses(raw):
    (flat,) = _texts([raw])
    assert flat == "a b"


def test_the_words_survive():
    """Flattening is not censoring: nothing but the spacing may change."""
    (flat,) = _texts(["Happy 30th Birthday Priya!  With love, from everyone"])
    assert flat == "Happy 30th Birthday Priya! With love, from everyone"


# ─── EVERYTHING ELSE IS LEFT ALONE ────────────────────


def test_ordinary_parameters_pass_through():
    sent = _texts(["49", "Prashasti Jaiswal", "Rs 2,300.00"])
    assert sent == ["49", "Prashasti Jaiswal", "Rs 2,300.00"]


def test_a_number_is_sent_as_text():
    """Meta wants strings; the callers do not all pass them."""
    assert _texts([49, 2300.0]) == ["49", "2300.0"]


def test_no_parameters_means_no_component():
    """
    A template with no placeholders must not carry an empty body component —
    Meta refuses that too.
    """
    assert _body_component([]) == []
    assert _body_component(None) == []
