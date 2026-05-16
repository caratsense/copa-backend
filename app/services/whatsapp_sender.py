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
        logger.error(f"[WA] Missing PHONE_ID or TOKEN. PHONE_ID={settings.WHATSAPP_PHONE_ID[:5] if settings.WHATSAPP_PHONE_ID else 'EMPTY'}")
        return None
    try:
        url = f"https://graph.facebook.com/v21.0/{settings.WHATSAPP_PHONE_ID}/messages"
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


def send_text(to: str, text: str) -> dict | None:
    """Send a free-form text message (only works within 24hr reply window)."""
    to = to.replace("+", "").replace(" ", "").replace("-", "")
    return _send({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    })


def send_template(to: str, template_name: str, params: list[str]) -> dict | None:
    """Send a pre-approved template message (works anytime, no 24hr restriction)."""
    to = to.replace("+", "").replace(" ", "").replace("-", "")
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
            "language": {"code": "en"},
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
