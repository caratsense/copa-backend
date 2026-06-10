"""
WhatsApp Notification Service
==============================
Sends notifications to customers via WhatsApp Business API.

SETUP:
1. Create a Meta Business account: https://business.facebook.com
2. Set up WhatsApp Business API in Meta Developer Portal
3. Get your Phone Number ID and Access Token
4. Create message templates in the WhatsApp Manager
5. Add credentials to .env:
   WHATSAPP_PHONE_ID=your_phone_number_id
   WHATSAPP_TOKEN=your_access_token
   WHATSAPP_ENABLED=true

TEMPLATES TO CREATE IN WHATSAPP MANAGER:
- order_confirmed: "Your order #{{1}} has been confirmed! Total: ₹{{2}}"
- order_preparing: "Your order #{{1}} is being prepared by our baker."
- rider_on_way: "Your rider is on the way! Track here: {{1}}"
- order_delivered: "Your order #{{1}} has been delivered! Enjoy!"
- order_cancelled: "Your order #{{1}} has been cancelled. Refund: ₹{{2}}"

HOW IT WORKS:
The event worker (app/workers/event_worker.py) calls these functions
when events fire. You don't need to call them from routes manually.
"""

import httpx
from typing import Optional
from app.config import get_settings

settings = get_settings()

WHATSAPP_API_URL = "https://graph.facebook.com/v21.0"


def _is_enabled() -> bool:
    return getattr(settings, "WHATSAPP_ENABLED", False)


def _get_headers() -> dict:
    token = getattr(settings, "WHATSAPP_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _send_template(
    to_phone: str,
    template_name: str,
    parameters: list[str],
    language: str = "en",
) -> dict:
    """
    Send a WhatsApp template message.
    to_phone: customer's phone number WITH country code (e.g. "919999999999")
    template_name: name of the approved template in WhatsApp Manager
    parameters: list of variable values ["#1234", "₹500"]
    """
    if not _is_enabled():
        print(f"[WhatsApp] DISABLED — would send '{template_name}' to {to_phone} with {parameters}")
        return {"status": "disabled", "template": template_name, "to": to_phone}

    phone_id = getattr(settings, "WHATSAPP_PHONE_ID", "")

    # Build template components
    components = []
    if parameters:
        components.append({
            "type": "body",
            "parameters": [
                {"type": "text", "text": str(p)} for p in parameters
            ]
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone.replace("+", ""),  # remove + prefix
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        }
    }

    try:
        response = httpx.post(
            f"{WHATSAPP_API_URL}/{phone_id}/messages",
            headers=_get_headers(),
            json=payload,
            timeout=10.0,
        )
        result = response.json()
        print(f"[WhatsApp] Sent '{template_name}' to {to_phone}: {response.status_code}")
        return result
    except Exception as e:
        print(f"[WhatsApp] Failed to send '{template_name}' to {to_phone}: {e}")
        return {"error": str(e)}


def _send_text(to_phone: str, message: str) -> dict:
    """Send a plain text message (for testing / non-template messages)."""
    if not _is_enabled():
        print(f"[WhatsApp] DISABLED — would send text to {to_phone}: {message}")
        return {"status": "disabled"}

    phone_id = getattr(settings, "WHATSAPP_PHONE_ID", "")
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone.replace("+", ""),
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = httpx.post(
            f"{WHATSAPP_API_URL}/{phone_id}/messages",
            headers=_get_headers(),
            json=payload,
            timeout=10.0,
        )
        return response.json()
    except Exception as e:
        print(f"[WhatsApp] Text send failed: {e}")
        return {"error": str(e)}


# ─── HIGH-LEVEL NOTIFICATION FUNCTIONS ────────────────
# Called by the event worker when events fire.

def notify_order_confirmed(customer_phone: str, order_id: int, total: float):
    return _send_template(customer_phone, "order_confirmed", [str(order_id), f"₹{total}"])


def notify_order_preparing(customer_phone: str, order_id: int):
    return _send_template(customer_phone, "order_preparing", [str(order_id)])


def notify_rider_on_way(customer_phone: str, tracking_url: str):
    return _send_template(customer_phone, "rider_on_way", [tracking_url])


def notify_order_delivered(customer_phone: str, order_id: int):
    return _send_template(customer_phone, "order_delivered", [str(order_id)])


def notify_order_cancelled(customer_phone: str, order_id: int, refund_amount: float):
    return _send_template(customer_phone, "order_cancelled", [str(order_id), f"₹{refund_amount}"])
