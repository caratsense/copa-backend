"""
WhatsApp Message Sender
========================
Sends messages via Meta WhatsApp Cloud API.
Handles both template messages (outbound/system-initiated) and
free-form text messages (within 24hr reply window).
"""

import httpx
import logging
import time
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)


def _send(payload: dict) -> dict | None:
    """Send a message via WhatsApp Cloud API."""
    if not settings.WHATSAPP_ENABLED:
        logger.info(f"[WA DISABLED] Would send to {payload.get('to', '?')}: {str(payload)[:200]}")
        return None
    if not settings.WHATSAPP_PHONE_ID or not settings.WHATSAPP_TOKEN:
        # Log the exact environment variable names. Abbreviating these to
        # "PHONE_ID"/"TOKEN" once sent someone hunting for variables that do
        # not exist; what the app reads is WHATSAPP_PHONE_ID / WHATSAPP_TOKEN.
        logger.error(
            "[WA] Not configured: WHATSAPP_PHONE_ID %s, WHATSAPP_TOKEN %s",
            "set" if settings.WHATSAPP_PHONE_ID else "MISSING",
            "set" if settings.WHATSAPP_TOKEN else "MISSING",
        )
        return None
    try:
        url = f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_PHONE_ID}/messages"
        headers = {
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
            "Content-Type": "application/json",
        }
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        data = resp.json()
        if resp.status_code >= 400:
            logger.error(f"[WA] API error {resp.status_code}: {data}")
        else:
            logger.info(f"[WA] Sent to {payload.get('to')}: {resp.status_code}")
        return data
    except Exception as e:
        logger.error(f"[WA] Failed to send to {payload.get('to')}: {e}")
        return None


def _normalise(to: str) -> str:
    """Meta wants digits only, no plus, no separators."""
    return (to or "").replace("+", "").replace(" ", "").replace("-", "").replace("(", "").replace(")", "")


def send_text(to: str, text: str) -> dict | None:
    """Send a free-form text message (only works within 24hr reply window)."""
    to = _normalise(to)
    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    })


# ─── INTERACTIVE ──────────────────────────────────────
# Tappable replies instead of "reply 1, 2 or 3". Meta's limits, which are
# refusals and not suggestions — a payload over any of them comes back 400 and
# the customer gets nothing at all:
#
#   buttons   at most 3, title <= 20 chars
#   list      at most 10 rows across all sections, title <= 24, description <= 72
#   body      <= 1024 chars either way
#
# The ids we attach are the ones the conversation already understands: "1",
# "2", "3". The webhook reads a tapped reply's id and hands it to the flow as
# though it had been typed, so the same state machine serves both, and anyone
# who prefers to type still can.

BUTTON_LIMIT = 3
LIST_LIMIT = 10
BODY_LIMIT = 1024
BUTTON_TITLE_LIMIT = 20
ROW_TITLE_LIMIT = 24
ROW_DESC_LIMIT = 72


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _shorten(text: str, limit: int) -> str:
    """
    Trim to `limit` keeping BOTH ends, not just the front.

    The end is where these labels differ. A plain cut turned the cookie menu
    into this, at a 24-character row title:

        Chip Chocolate Cookie B…      <- Box - Button, ₹200
        Chip Chocolate Cookie B…      <- Box - Jumbo,  ₹400
        Orange Cardamom Butter…       <- Cookie - Button
        Orange Cardamom Butter…       <- Cookie - Jumbo

    Two pairs identical character for character, so four of the six rows could
    not be told apart and the menu was unusable. Keeping the tail keeps the
    word that distinguishes them.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return text[:head].rstrip() + "…" + text[-tail:].lstrip()


def _clip_body(text: str, limit: int) -> str:
    """
    Trim a message body, keeping its line breaks.

    `_clip` collapses all whitespace, which is right for a row title or a
    button — neither renders a newline — and wrong for a body, where WhatsApp
    does. Running a body through it turned "*Cookies*" and the instruction
    under it into one run-on line.
    """
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# A product label carries its price after an em dash: "Biscoff Cookie — ₹650".
# A list row has a title and a description, so the two belong in different
# places rather than crammed into one line and then cut.
_PRICE_SEPARATOR = " — "


# ── Pictures Meta already holds ───────────────────────────────────────────
#
# A picture sent as a `link` is not delivered when the API accepts it: Meta
# has to go and fetch the file from the URL first. A list message needs no
# such fetch, so it overtakes the picture, and the menu arrived ABOVE the
# photograph that was supposed to introduce it — every time, in both the
# category and the product menus.
#
# Uploading the file once and sending its id removes the fetch from the send
# path entirely. Meta keeps an uploaded asset for 30 days; these are cached
# for 25, and re-uploaded lazily after that.
#
# The cache is a plain dict because the service runs one uvicorn worker, and
# losing it to a restart costs sixteen uploads spread over the next sixteen
# menus, not correctness.
_MEDIA_TTL_SECONDS = 25 * 24 * 60 * 60
_media_ids: dict[str, tuple[str, float]] = {}


def _upload_media(url: str) -> str | None:
    """
    Give Meta the picture itself, and remember the id it hands back.

    Returns None whenever anything at all goes wrong, because the caller's
    fallback — sending the link and letting Meta fetch it — still delivers
    the picture. It only loses the ordering.
    """
    if not (settings.WHATSAPP_ENABLED and settings.WHATSAPP_PHONE_ID and settings.WHATSAPP_TOKEN):
        return None

    cached = _media_ids.get(url)
    if cached and (time.time() - cached[1]) < _MEDIA_TTL_SECONDS:
        return cached[0]

    try:
        picture = httpx.get(url, timeout=15, follow_redirects=True)
        if picture.status_code >= 400:
            logger.warning("[WA] Could not fetch %s to upload: %s", url, picture.status_code)
            return None

        resp = httpx.post(
            f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}"
            f"/{settings.WHATSAPP_PHONE_ID}/media",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            data={"messaging_product": "whatsapp"},
            files={"file": (url.rsplit("/", 1)[-1], picture.content, "image/jpeg")},
            timeout=30,
        )
        media_id = (resp.json() or {}).get("id")
        if not media_id:
            logger.warning("[WA] Media upload refused for %s: %s", url, resp.text[:200])
            return None

        _media_ids[url] = (media_id, time.time())
        logger.info("[WA] Uploaded %s as media %s", url.rsplit("/", 1)[-1], media_id)
        return media_id
    except Exception as exc:
        logger.warning("[WA] Media upload failed for %s: %s", url, exc)
        return None


def send_image(to: str, url: str, caption: str = "") -> dict | None:
    """
    A photograph, as a message of its own.

    Used where a picture cannot be attached to the message it belongs with: a
    list's header is text-only, so the only way to put a cake above a menu of
    cakes is to send the cake first and the menu immediately after. WhatsApp
    stacks them, and they read as one.

    Meta fetches `url` itself, so it has to be publicly reachable, and it has
    to be a JPEG or a PNG — the WebP the site serves is refused.
    """
    to = _normalise(to)
    # An id if Meta already holds the file, a link if not. See _upload_media:
    # the link costs the ordering, because Meta fetches it before delivering.
    media_id = _upload_media(url)
    image: dict = {"id": media_id} if media_id else {"link": url}
    if caption:
        image["caption"] = _clip_body(caption, BODY_LIMIT)
    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image,
    })


def send_buttons(to: str, body: str, options: list[tuple[str, str]],
                 image_url: str = "") -> dict | None:
    """
    Up to three tappable replies. `options` is [(id, label), ...].

    Returns None if the send failed, exactly like the rest of this module, so
    the caller can fall back to plain text rather than leaving the customer
    looking at nothing.
    """
    to = _normalise(to)
    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            # Buttons are the one interactive type Meta allows a picture on.
            # A list's header is text or nothing.
            **({"header": {"type": "image", "image": {"link": image_url}}} if image_url else {}),
            "body": {"text": _clip_body(body, BODY_LIMIT)},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": str(oid),
                                                "title": _clip(label, BUTTON_TITLE_LIMIT)}}
                    for oid, label in options[:BUTTON_LIMIT]
                ],
            },
        },
    })


def send_list(to: str, body: str, menu_label: str,
              options: list[tuple[str, str]], section_title: str = "Options") -> dict | None:
    """
    A tap-to-open menu, for four to ten choices. `options` is [(id, label), ...].

    A label longer than a row title is split across the title and its
    description rather than simply cut: these are cake names and delivery
    windows, and "Belgian Chocolate Tru…" is not something anyone can choose
    between.
    """
    to = _normalise(to)
    labels = []
    for oid, label in options[:LIST_LIMIT]:
        label = " ".join((label or "").split())
        name, separator, price = label.rpartition(_PRICE_SEPARATOR)
        if not separator:
            name, price = label, ""
        labels.append((str(oid), name, price, label))

    # The trim is chosen for the list as a whole, not row by row.
    #
    # An end-trim reads best, so it is the default. But deciding row by row
    # fixed only half the cookie menu: row 2 took the plain trim because
    # nothing had collided with it YET, and row 3 — arriving to find its own
    # reading taken — took the two-ended one. The two rows then differed, and
    # the test for "no two rows are identical" passed, while the customer
    # still faced "Chip Chocolate Cookie B…" with no idea it meant Button.
    #
    # So every row whose plain reading is shared by another switches, and its
    # partner switches with it.
    plain = [_clip(name, ROW_TITLE_LIMIT) for _, name, _, _ in labels]
    ambiguous = {title for title in plain if plain.count(title) > 1}

    rows = []
    seen: set[str] = set()
    for (oid, name, price, label), flat in zip(labels, plain):
        title = _shorten(name, ROW_TITLE_LIMIT) if flat in ambiguous else flat
        # Last resort: names alike at both ends get their number back, so no
        # row can ever be indistinguishable from another.
        if title in seen:
            title = _clip(f"{oid}. {name}", ROW_TITLE_LIMIT)
        seen.add(title)

        # The description carries the price when the name fitted, and the
        # whole label when it did not, so nothing is lost to the trim.
        description = price if title == name else label
        row = {"id": oid, "title": title}
        if description:
            row["description"] = _clip(description, ROW_DESC_LIMIT)
        rows.append(row)

    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": _clip_body(body, BODY_LIMIT)},
            "action": {
                "button": _clip(menu_label, BUTTON_TITLE_LIMIT),
                "sections": [{"title": _clip(section_title, ROW_TITLE_LIMIT), "rows": rows}],
            },
        },
    })


def send_template(to: str, template_name: str, params: list[str]) -> dict | None:
    """Send a pre-approved template message (works anytime, no 24hr restriction)."""
    to = _normalise(to)
    components = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        })
    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            # Must match the language the template was approved under.
            "language": {"code": settings.WHATSAPP_TEMPLATE_LANG},
            "components": components,
        },
    })


def send_template_raw(to: str, template_name: str, language: str, params: list) -> dict | None:
    """
    Send a template by its exact Meta name and language.

    Used by the outbox so a retry replays precisely what was recorded, rather
    than re-resolving the registry (which may have been reconfigured since).
    """
    to = _normalise(to)
    components = []
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        })
    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    })


# ─── CONVENIENCE FUNCTIONS ────────────────────────────

def notify_customer_order_confirmed(phone: str, name: str, order_id: int, items_str: str, total: float, delivery_str: str):
    send_template(phone, "order_confirmation", [name, str(order_id), items_str, str(int(total)), delivery_str])


def notify_customer_delivered(phone: str, name: str, order_id: int):
    send_template(phone, "order_delivered", [name, str(order_id)])


def notify_customer_cancelled(phone: str, name: str, order_id: int):
    send_template(phone, "order_cancelled", [name, str(order_id)])


def notify_baker_new_order(phone: str, order_id: int, items_str: str, message: str, delivery_str: str):
    send_template(phone, "baker_new_order", [str(order_id), items_str, message or "None", delivery_str])


def notify_rider_new_delivery(phone: str, order_id: int, customer_name: str, customer_phone: str, address: str, maps_link: str, total: float):
    send_template(phone, "rider_new_delivery", [str(order_id), customer_name, customer_phone, address, maps_link, str(int(total))])


def notify_admin_new_order(phone: str, order_id: int, customer_name: str, customer_phone: str, items_str: str, total: float, delivery_str: str):
    send_template(phone, "admin_new_order", [str(order_id), f"{customer_name} ({customer_phone})", items_str, str(int(total)), delivery_str])


def notify_admin_approval_needed(phone: str, order_id: int, items_str: str, total: float):
    send_template(phone, "admin_approval_needed", [str(order_id), items_str, str(int(total))])
