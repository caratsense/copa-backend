"""
Turning the bot's numbered menus into things you tap.

WHY IT IS DONE HERE AND NOT IN THE FLOW
---------------------------------------
app/services/wa_customer_flow.py is a thousand lines of state machine that
returns one string per turn, and a good half of those strings end in a
numbered list — "1. Place an order / 2. Track an order / 3. Talk to us". The
obvious way to add buttons is to rewrite every one of those return sites to
hand back options as data. That is dozens of edits through the most
intricate code in the service, to change how a message LOOKS.

So the conversation is left exactly as it is and the options are recovered at
the send boundary instead. The ids attached to the buttons are the numbers
the flow already understands, and the webhook's `_extract_text` already reads
a tapped reply's id and passes it on as though it had been typed. Nothing in
the state machine can tell the difference, people who prefer to type still
can, and a menu this module declines to convert simply goes out as the text
it has always been.

WHAT IS AND IS NOT A MENU
-------------------------
An unbroken block of lines, each "N. something", numbered 1 upward with
nothing missing, at least two of them, and exactly one such block in the
message. Whatever sits above and below it is kept as the body.

It deliberately does NOT have to be the last thing in the message. That was
the first rule here, chosen because being too strict only costs a plain-text
message while being too loose attaches the wrong id to the wrong label. It
was too strict: the bot's most-used menu of all, the welcome, carries two
lines of prose and a link underneath the options, so under that rule the one
message every customer sees first was the one message that never converted.
Every unit test passed; the end-to-end test is what caught it.

Prose that merely contains a number is still not a menu, a list that starts
at 3 is not a menu, and a message with two separate lists is not converted at
all — guessing there would attach the wrong number to the wrong label, and it
would fail silently, because the conversation answers the wrong choice
perfectly plausibly.
"""

import logging
import re

from app.config.settings import get_settings
from app.services.whatsapp_sender import (
    BUTTON_LIMIT, LIST_LIMIT, send_buttons, send_image, send_list, send_text,
)

logger = logging.getLogger(__name__)

# "1. Place an order", "2) Track an order". The number is the id the flow
# already expects; the rest is the label.
_OPTION = re.compile(r"^\s*(\d{1,2})[.)]\s+(\S.*)$")

# Markdown-ish emphasis WhatsApp renders in a body but shows literally in a
# button title, where there is no formatting at all.
_DECORATION = re.compile(r"[*_~`]")

MIN_OPTIONS = 2

# ── The picture that goes with a menu ─────────────────────────────────────
#
# Every menu the flow writes opens with its own name in bold on the first
# line — "*Cookies*", "*Cakes*", "*What are you looking for?*" — so the
# message says which picture belongs with it without the flow having to be
# changed to carry one. That is the same trick as the options themselves:
# recover the structure at the send boundary rather than thread it through a
# thousand lines of state machine.
#
# The files are written by the frontend's scripts/wa_photos.py and served
# with the site. This list has to agree with that script's; a name here with
# no file behind it costs one refused image and the menu still goes out.
_WITH_A_PHOTO = frozenset({
    "What are you looking for?",
    "Cakes", "Bakes & Desserts", "Gifting & More",
    "Chocolate Celebration Cakes", "Vanilla Celebration Cakes", "Cheesecakes",
    "Desserts & Pudding Tubs", "Brownies", "Tea Cakes",
    "Breads & Bun Collection", "Cookies", "Healthy Collection", "Dog Cakes",
    "Gifting Collection", "Signature Collection",
})

_HEADING = re.compile(r"^\s*\*([^*\n]+)\*\s*$")


def _slug(name: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in name)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def photo_for(text: str) -> str:
    """The absolute URL of this menu's photograph, or "" if it has none."""
    first = (text or "").lstrip().split("\n", 1)[0]
    match = _HEADING.match(first)
    if not match:
        return ""
    heading = match.group(1).strip()
    if heading not in _WITH_A_PHOTO:
        return ""
    return f"{get_settings().PUBLIC_SITE_URL.rstrip('/')}/wa/{_slug(heading)}.jpg"


def parse_options(text: str) -> tuple[str, list[tuple[str, str]]]:
    """
    Split a reply into its body and the menu inside it.

    Returns (text, []) unchanged when there is no menu — which is the answer
    for most messages, and the one to give whenever there is any doubt.

    The body that comes back is everything except the option lines: what was
    above them and what was below, rejoined. For a message with no menu it is
    the original text, untouched.
    """
    lines = (text or "").split("\n")

    # Every unbroken run of option lines in the message. Unbroken matters: a
    # blank line between two options makes them two runs, which is what keeps
    # a paragraph with "1." in it from merging with an unrelated "2." further
    # down.
    runs: list[tuple[int, int, list[tuple[int, str]]]] = []
    start = None
    current: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _OPTION.match(line)
        if match:
            if start is None:
                start = index
            current.append((int(match.group(1)), match.group(2).strip()))
            continue
        if current:
            runs.append((start, index - 1, current))
        start, current = None, []
    if current:
        runs.append((start, len(lines) - 1, current))

    # 1, 2, 3 … with nothing missing. A run starting at 3, or skipping 4, is
    # not a whole menu — more likely a numbered paragraph — and guessing would
    # attach the wrong id to the wrong label, which is worse than plain text.
    menus = [
        run for run in runs
        if len(run[2]) >= MIN_OPTIONS
        and [n for n, _ in run[2]] == list(range(1, len(run[2]) + 1))
    ]

    # Two menus in one message is ambiguous about which one is being offered,
    # and getting it wrong is silent. One or none.
    if len(menus) != 1:
        return text, []

    first, last, collected = menus[0]
    kept = lines[:first] + lines[last + 1:]
    # Collapse the run of blank lines the removal leaves behind, so the body
    # does not end up with a hole where the menu was.
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

    options = [(str(n), _DECORATION.sub("", label).strip()) for n, label in collected]
    return body, options


def _looks_sent(result) -> bool:
    """
    Whether Meta accepted the message.

    `_send` returns the decoded response on success and None when it is
    disabled or the request threw; an API error comes back as a dict with an
    `error` key and HTTP 4xx, which is logged there but still returned.
    """
    return isinstance(result, dict) and "error" not in result


def send_reply(to: str, text: str, menu_label: str = "Choose") -> None:
    """
    Send one reply, as buttons, a list, or plain text — whichever it turns out
    to be.

    Falls back to text whenever the interactive send is refused. A customer
    looking at nothing because a payload broke one of Meta's limits is far
    worse than a customer reading "reply 1, 2 or 3".
    """
    if not text:
        return

    body, options = parse_options(text)

    photo = photo_for(text)

    if MIN_OPTIONS <= len(options) <= BUTTON_LIMIT:
        # The buttons say what the choices are, so repeating them above is
        # noise. But a body cannot be empty, so if stripping the menu left
        # nothing, keep the original.
        # A button message can carry the picture itself, in its header.
        if _looks_sent(send_buttons(to, body or text, options, image_url=photo)):
            return
        # A refused header should not cost the customer the buttons as well.
        if photo and _looks_sent(send_buttons(to, body or text, options)):
            logger.warning("[WA] Header image refused for %s - buttons sent without it", to[-4:])
            return
        logger.warning("[WA] Buttons refused for %s — falling back to text", to[-4:])

    elif BUTTON_LIMIT < len(options) <= LIST_LIMIT:
        # The rows ARE the menu, so the body is the question and nothing else.
        #
        # This used to pass the untouched `text`, on the reasoning that a list
        # hides its options behind a tap and the customer should be able to
        # read them without opening anything. That reasoning was wrong twice
        # over: WhatsApp renders a body as one flowing paragraph, so six cakes
        # and their prices arrived as an unbroken wall of words with a Choose
        # button stranded underneath it, and the same six were one tap away in
        # a clean list the whole time. The question alone is the message.
        # A list's header is text-only, so the picture goes ahead of it as a
        # message of its own and WhatsApp stacks the two. If it is refused —
        # a 404, a format Meta will not take — the menu still goes out, which
        # is the half that matters.
        if photo:
            send_image(to, photo)
        if _looks_sent(send_list(to, body or text, menu_label, options)):
            return
        logger.warning("[WA] List refused for %s — falling back to text", to[-4:])

    send_text(to, text)
