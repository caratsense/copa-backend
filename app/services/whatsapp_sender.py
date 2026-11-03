"""
WhatsApp Message Sender
========================
Sends messages via Meta WhatsApp Cloud API.
Handles both template messages (outbound/system-initiated) and
free-form text messages (within 24hr reply window).
"""

import httpx
import logging
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


def send_buttons(to: str, body: str, options: list[tuple[str, str]]) -> dict | None:
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
            "body": {"text": _clip(body, BODY_LIMIT)},
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
    rows = []
    for oid, label in options[:LIST_LIMIT]:
        label = " ".join((label or "").split())
        row = {"id": str(oid), "title": _clip(label, ROW_TITLE_LIMIT)}
        if len(label) > ROW_TITLE_LIMIT:
            row["description"] = _clip(label, ROW_DESC_LIMIT)
        rows.append(row)

    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": _clip(body, BODY_LIMIT)},
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
