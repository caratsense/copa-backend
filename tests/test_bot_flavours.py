"""
Every flavour the kitchen makes can be ordered over WhatsApp.

THE BUG THIS REPLACES
---------------------
The flavour step took the first ten flavours of thirty-four. Not the ten most
popular or the ten cheapest - the first ten by id, which are all vanilla. Every
chocolate flavour and all twelve Belgian ones were unreachable: the Belgian
surcharge was correct in the data, the website charged it, and no WhatsApp
customer could pick a Belgian flavour at all.

It had the shape of a sensible truncation, which is why it survived. Ten is
exactly what an interactive list holds, so it could not simply be deleted
either - the fix is groups first, then the flavours inside one.

WHAT THESE HOLD
---------------
The invariant worth testing is not "the groups are named correctly" but
"nothing is unreachable". A flavour that falls into no group is invisible to
every WhatsApp customer, and it fails silently: the menu still looks complete.
"""

from types import SimpleNamespace

import pytest

from app.services.wa_customer_flow import (
    FLAVOUR_GROUPS,
    _without_group_prefix,
    FLAVOUR_ROWS,
    TYPE_IT_INSTEAD,
    _flavour_group_of,
    _flavour_groups,
    _flavour_list_prompt,
)
from app.services.wa_interactive import parse_options
from app.services.whatsapp_sender import BUTTON_LIMIT, LIST_LIMIT


def _flavour(name, sponge="vanilla", extra=0.0):
    return SimpleNamespace(name=name, sponge=sponge, extra_cost=extra)


# ── Which group a flavour belongs to ──────────────────────────────────────


def test_belgian_is_split_out_of_the_chocolate_sponge_it_shares():
    """
    Belgian flavours carry sponge="chocolate" in the database - they are a
    chocolate cake. They are shown separately because they are the ones the
    customer pays extra for, and burying twelve of them inside nineteen
    chocolates is how they stayed invisible.
    """
    assert _flavour_group_of(_flavour("Belgian Chocolate Hazelnut", "chocolate", 150)) == "Belgian"
    assert _flavour_group_of(_flavour("Plain Chocolate", "chocolate")) == "Chocolate"
    assert _flavour_group_of(_flavour("Vanilla Pineapple", "vanilla")) == "Vanilla"


def test_an_unknown_sponge_lands_somewhere_rather_than_nowhere():
    """
    A flavour added next month with a blank or misspelled sponge must still
    be orderable. Falling through to Vanilla is arbitrary; falling through to
    nothing would make it unreachable, which is the bug being fixed.
    """
    assert _flavour_group_of(_flavour("Something New", "")) in FLAVOUR_GROUPS
    assert _flavour_group_of(_flavour("Something New", None)) in FLAVOUR_GROUPS
    assert _flavour_group_of(_flavour("Something New", "sponge-cake")) in FLAVOUR_GROUPS


# ── Nothing is unreachable ────────────────────────────────────────────────


def test_every_active_flavour_falls_into_exactly_one_group(db):
    """The invariant. A flavour in no group cannot be ordered by anybody."""
    from app.models.pricing import FlavorRule

    active = db.query(FlavorRule).filter(FlavorRule.is_active == True).all()
    grouped = [f["id"] for _, members in _flavour_groups(db) for f in members]

    assert sorted(grouped) == sorted(f.id for f in active)
    assert len(grouped) == len(set(grouped)), "a flavour appears in two groups"


def test_the_group_menu_is_small_enough_to_become_buttons(db):
    """
    Three groups fit in buttons, which is what makes this better than the
    list it replaces. More than three and it silently degrades to a list.
    """
    groups = _flavour_groups(db)
    assert len(groups) <= BUTTON_LIMIT


# ── Each group fits what a list can hold ──────────────────────────────────


def test_a_group_larger_than_a_list_offers_the_rest_by_name(db):
    twelve = [{"id": i, "name": f"Belgian Chocolate {i}", "extra_cost": 150.0}
              for i in range(1, 13)]

    prompt = _flavour_list_prompt("919999999999", {}, "Belgian", twelve)
    _, options = parse_options(prompt)

    assert len(options) <= LIST_LIMIT, "a list cannot hold more than ten rows"
    assert len(options) == FLAVOUR_ROWS + 1
    assert options[-1][1] == TYPE_IT_INSTEAD


def test_a_group_that_fits_shows_every_one_of_its_flavours(db):
    seven = [{"id": i, "name": f"Chocolate {i}", "extra_cost": 0.0}
             for i in range(1, 8)]

    prompt = _flavour_list_prompt("919999999999", {}, "Chocolate", seven)
    _, options = parse_options(prompt)

    assert len(options) == 7
    assert TYPE_IT_INSTEAD not in [label for _, label in options]


def test_the_surcharge_is_shown_against_the_flavour_that_carries_it(db):
    """
    The customer should see the +150 before choosing, not discover it in the
    total. It is read from the row, so repricing Belgian moves this with it.
    """
    prompt = _flavour_list_prompt("919999999999", {}, "Belgian", [
        {"id": 1, "name": "Plain Belgian Chocolate", "extra_cost": 150.0},
        {"id": 2, "name": "Vanilla Pineapple", "extra_cost": 0.0},
    ])

    assert "+₹150" in prompt
    # A flavour with no surcharge says nothing rather than "+0".
    assert "+₹0" not in prompt


def test_every_real_group_fits_a_list_once_trimmed(db):
    """The live catalogue, not a fixture: each group must be sendable."""
    for name, members in _flavour_groups(db):
        prompt = _flavour_list_prompt("919999999999", {}, name, members)
        _, options = parse_options(prompt)
        assert options, f"{name} produced no menu at all"
        assert len(options) <= LIST_LIMIT, f"{name} is {len(options)} rows"


# ── The label is not the identity ─────────────────────────────────


def test_the_heading_is_not_repeated_in_every_row():
    """
    Every Belgian flavour is "Belgian Chocolate something", which spent 18 of
    a row title's 24 characters on a word printed at the top of the same
    list - and left Orange and Orange Hazelnut Crumble reading identically.
    """
    assert _without_group_prefix("Belgian Chocolate Hazelnut", "Belgian") == "Hazelnut"
    assert _without_group_prefix("Chocolate Coffee", "Chocolate") == "Coffee"
    assert _without_group_prefix("Vanilla Pineapple", "Vanilla") == "Pineapple"


def test_a_name_that_only_contains_the_word_keeps_it():
    """Only a LEADING match goes, or "Plain Chocolate" becomes "Plain"."""
    assert _without_group_prefix("Plain Chocolate", "Chocolate") == "Plain Chocolate"
    assert _without_group_prefix("Plain Vanilla White Chocolate", "Vanilla") ==         "Plain Vanilla White Chocolate"


def test_a_name_that_is_nothing_but_the_prefix_is_left_alone():
    """
    Stripping it would leave an empty row with nothing to read, so the
    length check refuses to reduce a name to nothing.

    Shortening "Belgian Chocolate" to "Chocolate" is correct and not an
    exception to that: the heading above it says Belgian, so the row still
    reads as the flavour it is.
    """
    assert _without_group_prefix("Vanilla", "Vanilla") == "Vanilla"
    assert _without_group_prefix("Belgian ", "Belgian") == "Belgian "
    assert _without_group_prefix("Belgian Chocolate", "Belgian") == "Chocolate"
