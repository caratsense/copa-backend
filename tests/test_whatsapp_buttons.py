"""
The bot's menus go out as things you tap.

HOW IT WORKS, AND WHY IT IS SAFE
--------------------------------
wa_customer_flow returns one string per turn and a good half of them end in
"1. … / 2. … / 3. …". Rather than rewrite dozens of return sites through the
most intricate code in the service, wa_interactive recovers those options at
the send boundary and attaches the SAME numbers as button ids. The webhook's
_extract_text already reads a tapped reply's id and passes it on as though it
had been typed, so the state machine cannot tell the difference.

That property is the thing these tests exist to hold. If the ids ever stop
being the numbers the flow expects, every tap starts landing on the wrong
option — and it would do it silently, because the conversation would still
produce a perfectly reasonable-looking reply to the wrong choice.
"""

import pytest

from app.api.routes.webhook import _extract_text
from app.services.wa_interactive import parse_options, send_reply
from app.services import wa_interactive



def _menu(sent_messages):
    """
    The interactive message, skipping any photograph sent ahead of it.

    A category menu now sends its picture first, so sent_messages[0] is no
    longer the menu.
    """
    return [m for m in sent_messages if m.get("type") == "interactive"][0]


# ─── READING A MENU ───────────────────────────────────

def test_a_three_option_menu_is_found_and_lifted_out_of_the_body():
    body, options = parse_options(
        "How can I assist you?\n\n1. Place an order\n2. Track an order\n3. Talk to us"
    )
    assert options == [("1", "Place an order"), ("2", "Track an order"), ("3", "Talk to us")]
    assert body == "How can I assist you?"


def test_closing_brackets_count_too():
    _, options = parse_options("Pick:\n1) Chocolate\n2) Vanilla")
    assert options == [("1", "Chocolate"), ("2", "Vanilla")]


def test_bold_markers_are_stripped_from_a_label():
    """WhatsApp renders *bold* in a body and prints the asterisks in a button."""
    _, options = parse_options("Size:\n1. *1kg* - Rs 1200\n2. *2kg* - Rs 2200")
    assert options[0][1] == "1kg - Rs 1200"


def test_prose_that_merely_contains_a_number_is_not_a_menu():
    text = "Delivery takes about 1 hour. Call us on 9984444337 if you need it sooner."
    assert parse_options(text) == (text, [])


def test_a_single_numbered_line_is_not_a_menu():
    text = "Your order is confirmed.\n1. We will message you when it is out."
    assert parse_options(text) == (text, [])


def test_a_list_that_does_not_start_at_one_is_left_alone():
    """
    Guessing would attach the wrong id to the wrong label — and that failure
    is silent, because the conversation answers the wrong choice perfectly
    plausibly.
    """
    text = "Remaining:\n3. Pistachio\n4. Red Velvet"
    assert parse_options(text) == (text, [])


def test_a_list_with_a_gap_is_left_alone():
    text = "Choose:\n1. Chocolate\n2. Vanilla\n4. Pistachio"
    assert parse_options(text) == (text, [])


def test_a_menu_does_not_have_to_be_the_last_thing_in_the_message():
    """
    It used to have to be, and that rule cost the bot its most important menu
    of all: the welcome carries two lines of prose and a link underneath the
    options, so the one message every customer sees first was the one that
    never converted. Every unit test passed; the end-to-end test caught it.
    """
    body, options = parse_options(
        "Choose:\n1. Chocolate\n2. Vanilla\n\nReply cancel to stop."
    )
    assert [o[0] for o in options] == ["1", "2"]
    # What was above and below the menu is kept, with no hole where it was.
    assert body == "Choose:\n\nReply cancel to stop."


def test_the_real_welcome_message_converts():
    """The exact shape wa_customer_flow produces, prose and link included."""
    body, options = parse_options(
        "Welcome to Cake O' Clock.\n\n"
        "We offer premium handcrafted cakes with 40+ flavors, delivered across Lucknow.\n\n"
        "How can I assist you?\n\n"
        "1. Place an order\n2. Track an order\n3. View our menu\n\n"
        "You can also ask me anything about our cakes, pricing, or delivery.\n\n"
        "https://cakeoclock.co.in"
    )
    assert [o[1] for o in options] == ["Place an order", "Track an order", "View our menu"]
    assert "How can I assist you?" in body
    assert "ask me anything" in body
    assert "1." not in body


def test_two_separate_menus_are_not_guessed_between():
    """
    Ambiguous about which list is being offered, and getting it wrong is
    silent — the conversation answers the wrong choice perfectly plausibly.
    """
    text = ("Sizes:\n1. 1kg\n2. 2kg\n\n"
            "Flavours:\n1. Chocolate\n2. Vanilla")
    assert parse_options(text) == (text, [])


def test_trailing_blank_lines_do_not_hide_a_menu():
    _, options = parse_options("Choose:\n1. Chocolate\n2. Vanilla\n\n")
    assert len(options) == 2


# ─── SENDING IT ───────────────────────────────────────

def test_three_options_go_out_as_buttons(sent_messages):
    send_reply("919999999999",
               "How can I assist you?\n\n1. Place an order\n2. Track an order\n3. Talk to us")

    assert len(sent_messages) == 1
    payload = sent_messages[0]
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "button"

    buttons = payload["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["1", "2", "3"]
    assert [b["reply"]["title"] for b in buttons] == [
        "Place an order", "Track an order", "Talk to us",
    ]
    # The buttons say what the choices are, so the body does not repeat them.
    assert "1." not in payload["interactive"]["body"]["text"]


def test_more_than_three_options_go_out_as_a_list(sent_messages):
    send_reply("919999999999",
               "Pick a size:\n1. 500g\n2. 1kg\n3. 1.5kg\n4. 2kg\n5. 3kg")

    payload = sent_messages[0]
    assert payload["interactive"]["type"] == "list"
    rows = payload["interactive"]["action"]["sections"][0]["rows"]
    assert [r["id"] for r in rows] == ["1", "2", "3", "4", "5"]
    # The rows ARE the menu, so the body is the question and nothing else.
    # Sending the numbered lines as well put six cakes and their prices into
    # one run-on paragraph with a Choose button stranded underneath, while
    # the same six sat one tap away in a clean list.
    body = payload["interactive"]["body"]["text"]
    assert body == "Pick a size:"
    assert "500g" not in body


def test_too_many_options_stay_as_plain_text(sent_messages):
    """Eleven will not fit a list, and a truncated menu is a broken one."""
    menu = "Our cakes:\n" + "\n".join(f"{i}. Cake number {i}" for i in range(1, 12))
    send_reply("919999999999", menu)

    assert sent_messages[0]["type"] == "text"
    assert sent_messages[0]["text"]["body"] == menu


def test_a_reply_with_no_menu_is_sent_as_text(sent_messages):
    send_reply("919999999999", "Your order #41 is out for delivery.")
    assert sent_messages[0]["type"] == "text"


def test_a_long_label_is_clipped_to_fit_a_button(sent_messages):
    send_reply("919999999999",
               "Choose:\n1. Belgian Chocolate Truffle Celebration Cake\n2. Vanilla")

    title = sent_messages[0]["interactive"]["action"]["buttons"][0]["reply"]["title"]
    assert len(title) <= 20
    assert title.endswith("…")


def test_a_long_list_label_keeps_the_rest_in_a_description(sent_messages):
    """
    "Belgian Chocolate Tru…" is not something anyone can choose between, so
    the full name goes in the row's description.
    """
    send_reply("919999999999",
               "Choose:\n1. Belgian Chocolate Truffle Celebration Cake\n"
               "2. Vanilla\n3. Pistachio\n4. Red Velvet")

    row = sent_messages[0]["interactive"]["action"]["sections"][0]["rows"][0]
    assert len(row["title"]) <= 24
    assert "Truffle" in row["description"]


def test_a_refused_interactive_send_falls_back_to_text(monkeypatch, sent_messages):
    """
    A customer looking at nothing because a payload broke one of Meta's limits
    is far worse than a customer reading "reply 1, 2 or 3".
    """
    def refuse(*a, **kw):
        return {"error": {"message": "(#131009) Parameter value is not valid"}}

    monkeypatch.setattr(wa_interactive, "send_buttons", refuse)

    send_reply("919999999999", "Choose:\n1. Chocolate\n2. Vanilla")

    assert len(sent_messages) == 1
    assert sent_messages[0]["type"] == "text"
    assert "1. Chocolate" in sent_messages[0]["text"]["body"]


# ─── THE ROUND TRIP ───────────────────────────────────

def test_a_tapped_button_reaches_the_flow_as_the_number_it_carries():
    """
    The whole design rests on this. The id we put on the button is the text
    the conversation receives.
    """
    tapped = {
        "type": "interactive",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "2", "title": "Track an order"}},
    }
    text, early = _extract_text(tapped, "919999999999")
    assert (text, early) == ("2", None)


def test_a_tapped_list_row_does_the_same():
    tapped = {
        "type": "interactive",
        "interactive": {"type": "list_reply",
                        "list_reply": {"id": "4", "title": "2kg"}},
    }
    text, early = _extract_text(tapped, "919999999999")
    assert (text, early) == ("4", None)


def test_typing_the_number_still_works():
    typed = {"type": "text", "text": {"body": " 2 "}}
    assert _extract_text(typed, "919999999999") == ("2", None)


@pytest.mark.parametrize("menu,expected", [
    ("How can I assist you?\n\n1. Place an order\n2. Track an order\n3. Talk to us",
     ["1", "2", "3"]),
    ("*Delivery date:*\n\n1. Today\n2. Tomorrow\n3. Day after", ["1", "2", "3"]),
    ("*Preferred time:*\n\n1. 10am - 12pm\n2. 12pm - 3pm\n3. 3pm - 6pm\n4. 6pm - 9pm",
     ["1", "2", "3", "4"]),
])
def test_the_ids_are_always_the_numbers_the_flow_expects(sent_messages, menu, expected):
    send_reply("919999999999", menu)
    payload = sent_messages[0]["interactive"]["action"]
    ids = ([b["reply"]["id"] for b in payload["buttons"]]
           if "buttons" in payload
           else [r["id"] for r in payload["sections"][0]["rows"]])
    assert ids == expected


# ─── THROUGH THE REAL WEBHOOK ─────────────────────────
# Everything above tests the pieces. This drives an actual signed webhook
# delivery and looks at what Meta would have received, so the wiring between
# the flow, the send boundary and the transport is covered too — a unit test
# of parse_options passes perfectly well while send_text is still the thing
# the webhook calls.

def test_a_real_conversation_turn_comes_out_tappable(client, db, wa_secret, sent_messages):
    from app.models.user import UserRole
    from tests.conftest import make_user
    from tests.test_whatsapp_webhook import message, post

    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    customer.whatsapp_opt_in = True
    db.commit()

    res = post(client, message("hi", "919222222222", msg_id="wamid.BTN1"))
    assert res.status_code == 200, res.text

    assert sent_messages, "the webhook sent nothing at all"
    last = sent_messages[-1]
    assert last["type"] == "interactive", (
        "the welcome menu went out as plain text — the webhook is probably "
        f"still calling send_text. Payload: {last}"
    )
    buttons = last["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["1", "2", "3"]


def test_tapping_that_button_moves_the_conversation_on(client, db, wa_secret, sent_messages):
    """
    The round trip for real: tap option 2, and the bot answers option 2.
    """
    from app.models.user import UserRole
    from tests.conftest import make_user
    from tests.test_whatsapp_webhook import post

    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    customer.whatsapp_opt_in = True
    db.commit()

    post(client, {"entry": [{"changes": [{"value": {"messages": [{
        "id": "wamid.BTN2", "from": "919222222222", "type": "interactive",
        "interactive": {"type": "button_reply",
                        "button_reply": {"id": "2", "title": "Track an order"}},
    }]}}]}]})

    assert sent_messages, "tapping a button produced no reply at all"
    last = sent_messages[-1]
    body = (last.get("text", {}).get("body")
            or last.get("interactive", {}).get("body", {}).get("text", ""))
    assert "order" in body.lower()


# ──────── What the cookies menu actually did to a customer ────────────────────────


def test_rows_that_differ_only_at_the_end_stay_distinguishable(sent_messages):
    """
    The real failure, from a real conversation.

    At a 24-character row title a front-truncating cut gave the cookies menu
    two pairs of rows identical character for character — "Chip Chocolate
    Cookie B..." was both the Button box and the Jumbo box. Four of the six
    rows could not be told apart, so the list was unusable.
    """
    send_reply("919999999999",
               "*Cookies*\n"
               "1. Biscoff Cookie — ₹650\n"
               "2. Chip Chocolate Cookie Box - Button — from ₹200\n"
               "3. Chip Chocolate Cookie Box - Jumbo — ₹400\n"
               "4. Chocolate Brookie — ₹650\n"
               "5. Orange Cardamom Butter Cookie - Button — from ₹200\n"
               "6. Orange Cardamom Butter Cookie - Jumbo — ₹400\n\n"
               "Reply with the number or the name.")

    rows = _menu(sent_messages)["interactive"]["action"]["sections"][0]["rows"]
    titles = [r["title"] for r in rows]

    assert len(set(titles)) == len(titles), f"identical rows: {titles}"
    # The word that tells them apart survives the trim.
    assert "Button" in titles[1] and "Jumbo" in titles[2]
    assert "Button" in titles[4] and "Jumbo" in titles[5]


def test_the_price_goes_in_the_description_not_the_title(sent_messages):
    """A row has two lines; the name belongs on one and the price the other."""
    send_reply("919999999999",
               "*Cookies*\n"
               "1. Biscoff Cookie — ₹650\n"
               "2. Chocolate Brookie — ₹700\n"
               "3. Pistachio Cookie — ₹800\n"
               "4. Almond Cookie — ₹900")

    rows = _menu(sent_messages)["interactive"]["action"]["sections"][0]["rows"]
    assert rows[0]["title"] == "Biscoff Cookie"
    assert rows[0]["description"] == "₹650"


def test_an_interactive_body_keeps_its_line_breaks(sent_messages):
    """
    WhatsApp renders a newline in a body, so collapsing them is a choice and
    the wrong one: the heading and the instruction under it ran together.
    """
    send_reply("919999999999",
               "*Cookies*\n\n"
               "1. One — ₹1\n2. Two — ₹2\n"
               "3. Three — ₹3\n4. Four — ₹4\n\n"
               "Reply with the number.")

    body = _menu(sent_messages)["interactive"]["body"]["text"]
    assert body == "*Cookies*\n\nReply with the number."


# ── The photograph above a menu ───────────────────────────────────


def test_a_list_menu_sends_its_photograph_first(sent_messages):
    """
    Meta allows a header picture on buttons and not on a list, so the only
    way to put a cake above a menu of cakes is to send the cake as its own
    message immediately before. WhatsApp stacks the two.
    """
    send_reply("919999999999",
               "*Cookies*\n1. One — ₹1\n2. Two — ₹2\n"
               "3. Three — ₹3\n4. Four — ₹4")

    assert [m["type"] for m in sent_messages] == ["image", "interactive"]
    assert sent_messages[0]["image"]["link"].endswith("/wa/cookies.jpg")
    assert sent_messages[1]["interactive"]["type"] == "list"


def test_a_button_menu_carries_its_photograph_in_the_header(sent_messages):
    """One message, not two — buttons are the type that allows a header."""
    send_reply("919999999999",
               "*What are you looking for?*\n\n1. Cakes\n2. Bakes & Desserts\n"
               "3. Gifting & More\n\nTap one.")

    assert [m["type"] for m in sent_messages] == ["interactive"]
    header = sent_messages[0]["interactive"]["header"]
    assert header["type"] == "image"
    assert header["image"]["link"].endswith("/wa/what-are-you-looking-for.jpg")


def test_a_menu_with_no_photograph_of_its_own_just_goes_out(sent_messages):
    """Most of the bot's menus are sizes and times, not categories."""
    send_reply("919999999999",
               "*Select size*\n1. 500g\n2. 1kg\n3. 1.5kg\n4. 2kg")

    assert [m["type"] for m in sent_messages] == ["interactive"]
    assert "header" not in sent_messages[0]["interactive"]


def test_the_menu_survives_a_refused_photograph(sent_messages, monkeypatch):
    """
    A 404 on the picture must not cost the customer the menu. Meta fetches
    the URL itself, so a bad deploy or a renamed file is a live possibility
    and the half that matters is the list.
    """
    from app.services import whatsapp_sender

    real = whatsapp_sender._send

    def refuse_images(payload):
        if payload.get("type") == "image":
            return {"error": {"message": "(#131053) Media upload error"}}
        return real(payload)

    monkeypatch.setattr(whatsapp_sender, "_send", refuse_images)

    send_reply("919999999999",
               "*Cookies*\n1. One — ₹1\n2. Two — ₹2\n"
               "3. Three — ₹3\n4. Four — ₹4")

    kinds = [m.get("type") for m in sent_messages]
    assert "interactive" in kinds
    assert sent_messages[-1]["interactive"]["type"] == "list"
