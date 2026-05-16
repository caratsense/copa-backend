"""
AI Message Parser (Groq / Keyword Fallback)
=============================================
Parses WhatsApp messages into structured actions.
Uses keyword matching first (free, instant), then Groq AI for natural language.
"""

import httpx
import json
import logging
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def parse_message(message: str, role: str, context: dict) -> dict:
    """Parse a WhatsApp message into an action dict."""
    # Always try keyword matching first (free, instant, no API needed)
    keyword_result = _keyword_match(message, role, context)
    if keyword_result:
        logger.info(f"[PARSER] Keyword match: {keyword_result}")
        return keyword_result

    # If no keyword match and Groq key available, use AI
    if settings.GROQ_API_KEY:
        try:
            result = _groq_parse(message, role, context)
            logger.info(f"[PARSER] Groq result: {result}")
            return result
        except Exception as e:
            logger.error(f"[PARSER] Groq failed: {e}")

    # Final fallback
    return {"action": "UNKNOWN", "reply": "Sorry, I didn't understand that. Please try again or type HELP."}


def _keyword_match(message: str, role: str, context: dict) -> dict | None:
    """Fast keyword matching — handles all common commands without AI."""
    msg = message.strip().upper()
    words = msg.split()

    # Extract order ID from message
    order_id = None
    for w in words:
        if w.isdigit():
            order_id = int(w)
            break

    # ─── BAKER ────────────────────────────
    if role == "baker":
        if any(w in msg for w in ("START", "BEGIN", "SHURU")):
            if order_id:
                return {"action": "BAKER_START", "order_id": order_id}
        if any(w in msg for w in ("DONE", "READY", "COMPLETE", "FINISHED", "HO GAYA", "HOGAYA", "TAYYAR")):
            if order_id:
                return {"action": "BAKER_DONE", "order_id": order_id}
        if any(w in msg for w in ("QUEUE", "ORDERS", "LIST", "STATUS", "KYA HAI", "DIKHAO")):
            return {"action": "BAKER_QUEUE"}
        if any(w in msg for w in ("HI", "HELLO", "HEY", "HELP")):
            return {"action": "UNKNOWN", "reply": "🧁 *Baker Commands:*\n\nSTART {id} — Begin baking\nDONE {id} — Mark complete\nQUEUE — Show your orders"}

    # ─── RIDER ────────────────────────────
    elif role == "rider":
        if any(w in msg for w in ("PICKED", "PICKUP", "UTHA", "LIYA", "COLLECTED")):
            if order_id:
                return {"action": "RIDER_PICKED", "order_id": order_id}
        if any(w in msg for w in ("DELIVERED", "DELIVER", "PAHUNCHA", "DONE", "DROP")):
            if order_id:
                return {"action": "RIDER_DELIVERED", "order_id": order_id}
        if any(w in msg for w in ("QUEUE", "DELIVERIES", "LIST", "STATUS", "KYA HAI")):
            return {"action": "RIDER_QUEUE"}
        if any(w in msg for w in ("HI", "HELLO", "HEY", "HELP")):
            return {"action": "UNKNOWN", "reply": "🛵 *Rider Commands:*\n\nPICKED {id} — Confirm pickup\nDELIVERED {id} — Mark delivered\nQUEUE — Show your deliveries"}

    # ─── ADMIN ────────────────────────────
    elif role == "admin":
        if any(w in msg for w in ("APPROVE", "OK", "THEEK", "SHIP")):
            if order_id:
                return {"action": "ADMIN_APPROVE", "order_id": order_id}
        if any(w in msg for w in ("REJECT", "WAPAS", "BACK", "REDO")):
            if order_id:
                return {"action": "ADMIN_REJECT", "order_id": order_id}
        if any(w in msg for w in ("PAID", "PAYMENT", "PAISA")):
            if order_id:
                return {"action": "ADMIN_PAID", "order_id": order_id}
        if msg.startswith("CANCEL") and order_id:
            return {"action": "ADMIN_CANCEL", "order_id": order_id}
        if "ASSIGN BAKER" in msg and order_id:
            return {"action": "ADMIN_ASSIGN_BAKER", "order_id": order_id}
        if "ASSIGN RIDER" in msg and order_id:
            return {"action": "ADMIN_ASSIGN_RIDER", "order_id": order_id}
        if any(w in msg for w in ("ORDERS", "TODAY", "SUMMARY", "AAJ")):
            return {"action": "ADMIN_ORDERS"}
        if any(w in msg for w in ("HI", "HELLO", "HEY", "HELP")):
            return {"action": "UNKNOWN", "reply": "🔑 *Admin Commands:*\n\nAPPROVE {id} — Approve order\nREJECT {id} — Send back\nPAID {id} — Mark paid\nCANCEL {id} — Cancel\nASSIGN BAKER {id}\nASSIGN RIDER {id}\nORDERS — Today's summary"}

    # ─── CUSTOMER ─────────────────────────
    elif role == "customer":
        step = context.get("step", "IDLE")

        if step == "IDLE" or step is None:
            if msg in ("1", "ORDER", "CAKE", "ORDER CAKE"):
                return {"action": "START_ORDER"}
            if msg in ("2", "STATUS", "TRACK", "ORDER STATUS"):
                return {"action": "CHECK_STATUS"}
            if msg in ("3", "MENU", "SHOW MENU"):
                return {"action": "VIEW_MENU"}
            if any(w in msg for w in ("HI", "HELLO", "HEY", "START", "NAMASTE")):
                return {"action": "WELCOME"}

        if msg in ("CANCEL", "STOP", "EXIT", "QUIT"):
            return {"action": "CANCEL_ORDER"}
        if msg in ("CONFIRM", "YES", "HAAN", "HA"):
            return {"action": "CONFIRM_ORDER"}
        if msg in ("SKIP", "NO", "NAHI"):
            return {"action": "SKIP"}
        if msg in ("PICKUP", "SELF PICKUP", "SELF-PICKUP"):
            return {"action": "SET_ADDRESS", "text": "Self Pickup"}

        # Number selection during ordering flow
        if msg.isdigit():
            num = int(msg)
            if step == "AWAITING_STATUS_ID":
                return {"action": "STATUS_ORDER_ID", "order_id": num}
            if step in ("SELECT_PRODUCT", "SELECT_SIZE", "SELECT_FLAVOR", "SELECT_DATE", "SELECT_TIME"):
                return {"action": "SELECT_OPTION", "value": num}

        # Text input during ordering flow
        if step == "CAKE_MESSAGE":
            return {"action": "SET_MESSAGE", "text": message.strip()}
        if step == "DELIVERY_ADDRESS":
            return {"action": "SET_ADDRESS", "text": message.strip()}

    return None


def _groq_parse(message: str, role: str, context: dict) -> dict:
    """Use Groq AI to understand natural language."""
    step = context.get("step", "IDLE")

    system = f"""You parse WhatsApp messages for a bakery. Sender role: {role}, state: {step}.
Return ONLY valid JSON, no markdown. Examples:
- "start 5" → {{"action":"BAKER_START","order_id":5}}
- "ho gaya 3" → {{"action":"BAKER_DONE","order_id":3}}
- "picked 7" → {{"action":"RIDER_PICKED","order_id":7}}
- "approve 4" → {{"action":"ADMIN_APPROVE","order_id":4}}
- "hi" → {{"action":"WELCOME"}}
- "menu" → {{"action":"VIEW_MENU"}}
If unclear: {{"action":"UNKNOWN","reply":"helpful message"}}"""

    resp = httpx.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {settings.GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": message},
            ],
            "temperature": 0.1,
            "max_tokens": 150,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)
