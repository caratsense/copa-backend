"""
Gemini AI Message Parser
=========================
Uses Google Gemini to parse natural language WhatsApp messages
into structured actions (JSON).
"""

import httpx
import json
import logging
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent"


def parse_message(message: str, role: str, context: dict) -> dict:
    """
    Parse a WhatsApp message using Gemini AI.
    Returns a dict like: {"action": "START_ORDER", "order_id": 47}
    Falls back to keyword matching if Gemini fails.
    """
    # Try keyword matching first (fast, free)
    keyword_result = _keyword_match(message, role, context)
    if keyword_result:
        return keyword_result

    # If no keyword match, use Gemini for natural language
    if not settings.GEMINI_API_KEY:
        return {"action": "UNKNOWN", "reply": "Sorry, I didn't understand that. Please try again."}

    try:
        return _gemini_parse(message, role, context)
    except Exception as e:
        logger.error(f"[GEMINI] Parse failed: {e}")
        return {"action": "UNKNOWN", "reply": "Sorry, something went wrong. Please try again."}


def _keyword_match(message: str, role: str, context: dict) -> dict | None:
    """Fast keyword matching — handles obvious commands without AI."""
    msg = message.strip().upper()
    words = msg.split()

    # Extract order ID from message
    order_id = None
    for w in words:
        if w.isdigit():
            order_id = int(w)
            break

    if role == "baker":
        if msg.startswith("START") and order_id:
            return {"action": "BAKER_START", "order_id": order_id}
        if msg in ("DONE", "READY", "COMPLETE", "FINISHED") or (msg.startswith("DONE") and order_id):
            return {"action": "BAKER_DONE", "order_id": order_id}
        if msg in ("QUEUE", "ORDERS", "LIST", "STATUS"):
            return {"action": "BAKER_QUEUE"}

    elif role == "rider":
        if msg.startswith("PICKED") and order_id:
            return {"action": "RIDER_PICKED", "order_id": order_id}
        if msg.startswith("DELIVER") and order_id:
            return {"action": "RIDER_DELIVERED", "order_id": order_id}
        if msg in ("QUEUE", "DELIVERIES", "LIST", "STATUS"):
            return {"action": "RIDER_QUEUE"}

    elif role == "admin":
        if msg.startswith("APPROVE") and order_id:
            return {"action": "ADMIN_APPROVE", "order_id": order_id}
        if msg.startswith("REJECT") and order_id:
            return {"action": "ADMIN_REJECT", "order_id": order_id}
        if msg.startswith("PAID") and order_id:
            return {"action": "ADMIN_PAID", "order_id": order_id}
        if msg.startswith("CANCEL") and order_id:
            return {"action": "ADMIN_CANCEL", "order_id": order_id}
        if msg.startswith("ASSIGN BAKER") and order_id:
            return {"action": "ADMIN_ASSIGN_BAKER", "order_id": order_id}
        if msg.startswith("ASSIGN RIDER") and order_id:
            return {"action": "ADMIN_ASSIGN_RIDER", "order_id": order_id}
        if msg in ("ORDERS", "TODAY", "SUMMARY"):
            return {"action": "ADMIN_ORDERS"}

    elif role == "customer":
        step = context.get("step", "IDLE")
        if step == "IDLE":
            if msg in ("1", "ORDER", "CAKE"):
                return {"action": "START_ORDER"}
            if msg in ("2", "STATUS", "TRACK"):
                return {"action": "CHECK_STATUS"}
            if msg in ("3", "MENU"):
                return {"action": "VIEW_MENU"}
            if msg in ("HI", "HELLO", "HEY", "START"):
                return {"action": "WELCOME"}
        if msg == "CANCEL":
            return {"action": "CANCEL_ORDER"}
        if msg == "CONFIRM":
            return {"action": "CONFIRM_ORDER"}
        # Number selection during ordering
        if msg.isdigit() and step not in ("IDLE", "AWAITING_STATUS_ID"):
            return {"action": "SELECT_OPTION", "value": int(msg)}
        if step == "AWAITING_STATUS_ID" and msg.isdigit():
            return {"action": "STATUS_ORDER_ID", "order_id": int(msg)}

    return None  # No keyword match, use Gemini


def _gemini_parse(message: str, role: str, context: dict) -> dict:
    """Use Gemini to understand natural language messages."""
    step = context.get("step", "IDLE")
    products = context.get("products", [])
    orders = context.get("orders", [])

    system_prompt = f"""You are a WhatsApp message parser for Cake O' Clock bakery.
The sender's role is: {role}
Their current conversation state: {step}

{"Available products: " + json.dumps(products) if products else ""}
{"Their active orders: " + json.dumps(orders) if orders else ""}

Parse the user's message and return ONLY a JSON object (no markdown, no explanation).

For {role}, valid actions are:
"""
    if role == "baker":
        system_prompt += """
- {"action": "BAKER_START", "order_id": <number>} — baker wants to start baking
- {"action": "BAKER_DONE", "order_id": <number>} — baker finished baking
- {"action": "BAKER_QUEUE"} — baker wants to see their queue
- {"action": "UNKNOWN", "reply": "<helpful message>"} — can't understand
"""
    elif role == "rider":
        system_prompt += """
- {"action": "RIDER_PICKED", "order_id": <number>} — rider picked up order
- {"action": "RIDER_DELIVERED", "order_id": <number>} — rider delivered order
- {"action": "RIDER_QUEUE"} — rider wants to see deliveries
- {"action": "UNKNOWN", "reply": "<helpful message>"} — can't understand
"""
    elif role == "admin":
        system_prompt += """
- {"action": "ADMIN_APPROVE", "order_id": <number>} — approve baked order
- {"action": "ADMIN_REJECT", "order_id": <number>} — reject/send back order
- {"action": "ADMIN_PAID", "order_id": <number>} — mark payment received
- {"action": "ADMIN_CANCEL", "order_id": <number>} — cancel order
- {"action": "ADMIN_ASSIGN_BAKER", "order_id": <number>} — assign baker
- {"action": "ADMIN_ASSIGN_RIDER", "order_id": <number>} — assign rider
- {"action": "ADMIN_ORDERS"} — show today's summary
- {"action": "UNKNOWN", "reply": "<helpful message>"} — can't understand
"""
    elif role == "customer":
        system_prompt += f"""
Current ordering step: {step}
- {{"action": "WELCOME"}} — greeting
- {{"action": "START_ORDER"}} — wants to order a cake
- {{"action": "CHECK_STATUS"}} — wants to check order status
- {{"action": "VIEW_MENU"}} — wants to see the menu
- {{"action": "SELECT_OPTION", "value": <number>}} — selecting a numbered option
- {{"action": "SELECT_PRODUCT_BY_NAME", "name": "<product name>"}} — selecting product by name
- {{"action": "SELECT_SIZE_BY_NAME", "name": "<size>"}} — selecting size by name
- {{"action": "SELECT_FLAVOR_BY_NAME", "name": "<flavor>"}} — selecting flavor by name
- {{"action": "SET_MESSAGE", "text": "<cake message>"}} — cake message like "Happy Birthday"
- {{"action": "SKIP"}} — skip optional step
- {{"action": "SET_ADDRESS", "text": "<address>"}} — delivery address
- {{"action": "SET_DATE", "value": <1=tomorrow, 2=day after>}} — delivery date
- {{"action": "SET_TIME", "value": <1=morning, 2=afternoon, 3=evening>}} — delivery time
- {{"action": "CONFIRM_ORDER"}} — confirm the order
- {{"action": "CANCEL_ORDER"}} — cancel current order
- {{"action": "STATUS_ORDER_ID", "order_id": <number>}} — check specific order
- {{"action": "UNKNOWN", "reply": "<helpful message>"}} — can't understand
"""

    resp = httpx.post(
        f"{GEMINI_URL}?key={settings.GEMINI_API_KEY}",
        json={
            "contents": [{"parts": [{"text": f"{system_prompt}\n\nUser message: {message}"}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256},
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    # Clean potential markdown wrapping
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)
