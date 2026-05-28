"""
AI Message Parser (Groq-First)
===============================
1. Quick number/keyword match for "1", "2", "3", "CONFIRM", "CANCEL" etc.
2. Everything else → Groq AI (llama-3.1-8b-instant)
3. Voice messages → Groq Whisper → text → AI parse
"""

import httpx
import json
import logging
import tempfile
import os
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def transcribe_voice(audio_url: str) -> str:
    """Download WhatsApp voice message and transcribe with Groq Whisper."""
    if not settings.GROQ_API_KEY:
        return ""
    try:
        # Download audio from Meta
        audio_resp = httpx.get(audio_url, headers={
            "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        }, timeout=30, follow_redirects=True)
        
        if audio_resp.status_code != 200:
            logger.error(f"[VOICE] Download failed: {audio_resp.status_code}")
            return ""

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_resp.content)
            temp_path = f.name

        # Transcribe with Groq Whisper
        with open(temp_path, "rb") as audio_file:
            resp = httpx.post(
                GROQ_WHISPER_URL,
                headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                files={"file": ("voice.ogg", audio_file, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "hi", "response_format": "json"},
                timeout=30,
            )
        
        os.unlink(temp_path)  # cleanup
        
        if resp.status_code == 200:
            text = resp.json().get("text", "").strip()
            logger.info(f"[VOICE] Transcribed: {text}")
            return text
        else:
            logger.error(f"[VOICE] Whisper failed: {resp.status_code} {resp.text[:200]}")
            return ""
    except Exception as e:
        logger.error(f"[VOICE] Error: {e}")
        return ""


def get_audio_url(media_id: str) -> str:
    """Get download URL for a WhatsApp media file."""
    try:
        resp = httpx.get(
            f"https://graph.facebook.com/v21.0/{media_id}",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"},
            timeout=10,
        )
        return resp.json().get("url", "")
    except:
        return ""


def parse_message(message: str, role: str, context: dict) -> dict:
    """Parse message — quick match first, then Groq AI."""
    # Quick exact matches (free, instant)
    quick = _quick_match(message, role, context)
    if quick:
        return quick

    # AI parsing with Groq
    if settings.GROQ_API_KEY:
        try:
            return _groq_parse(message, role, context)
        except Exception as e:
            logger.error(f"[AI] Groq failed: {e}")

    # Fallback
    return {"action": "UNKNOWN", "reply": "I didn't quite get that. Could you try again? You can type a number or describe what you need."}


def _quick_match(message: str, role: str, context: dict) -> dict | None:
    """Fast exact matching for numbers and common commands."""
    msg = message.strip().upper()
    step = context.get("step", "IDLE")

    # Universal commands
    if msg in ("CANCEL", "STOP", "EXIT", "QUIT", "RESET"):
        return {"action": "CANCEL_ORDER"}
    if msg in ("CONFIRM", "YES", "HAAN", "HA", "OK"):
        return {"action": "CONFIRM_ORDER"}
    if msg in ("SKIP", "NO", "NAHI"):
        return {"action": "SKIP"}
    if msg in ("HELP", "COMMANDS", "?"):
        return None  # Let AI handle with context

    # Number selections
    if msg.isdigit():
        num = int(msg)
        if role == "customer":
            if step == "IDLE" or step is None:
                if num == 1: return {"action": "START_ORDER"}
                if num == 2: return {"action": "CHECK_STATUS"}
                if num == 3: return {"action": "VIEW_MENU"}
            elif step == "AWAITING_STATUS_ID":
                return {"action": "STATUS_ORDER_ID", "order_id": num}
            elif step in ("SELECT_PRODUCT", "SELECT_SIZE", "SELECT_FLAVOR", "SELECT_DATE", "SELECT_TIME"):
                return {"action": "SELECT_OPTION", "value": num}

    # Baker exact commands
    if role == "baker":
        words = msg.split()
        oid = next((int(w) for w in words if w.isdigit()), None)
        if msg.startswith("START") and oid: return {"action": "BAKER_START", "order_id": oid}
        if msg.startswith("DONE") and oid: return {"action": "BAKER_DONE", "order_id": oid}
        if msg in ("QUEUE", "ORDERS"): return {"action": "BAKER_QUEUE"}

    # Rider exact commands
    if role == "rider":
        words = msg.split()
        oid = next((int(w) for w in words if w.isdigit()), None)
        if msg.startswith("PICKED") and oid: return {"action": "RIDER_PICKED", "order_id": oid}
        if msg.startswith("DELIVERED") and oid: return {"action": "RIDER_DELIVERED", "order_id": oid}
        if msg in ("QUEUE", "DELIVERIES"): return {"action": "RIDER_QUEUE"}

    # Admin exact commands
    if role == "admin":
        words = msg.split()
        oid = next((int(w) for w in words if w.isdigit()), None)
        if msg.startswith("APPROVE") and oid: return {"action": "ADMIN_APPROVE", "order_id": oid}
        if msg.startswith("REJECT") and oid: return {"action": "ADMIN_REJECT", "order_id": oid}
        if msg.startswith("PAID") and oid: return {"action": "ADMIN_PAID", "order_id": oid}
        if msg.startswith("CANCEL") and oid: return {"action": "ADMIN_CANCEL", "order_id": oid}
        if msg in ("ORDERS", "TODAY", "SUMMARY"): return {"action": "ADMIN_ORDERS"}

    # Text input during flow steps
    if role == "customer":
        if step == "CAKE_MESSAGE": return {"action": "SET_MESSAGE", "text": message.strip()}
        if step == "DELIVERY_ADDRESS": return {"action": "SET_ADDRESS", "text": message.strip()}

    return None  # Not a quick match → use AI


def _groq_parse(message: str, role: str, context: dict) -> dict:
    """Use Groq AI to understand natural language."""
    step = context.get("step", "IDLE")
    products = context.get("products", [])
    orders = context.get("orders", [])

    system = f"""You are the WhatsApp assistant for Cake O' Clock, a premium bakery in Lucknow.
You parse customer/staff messages and return ONLY a JSON action. No markdown, no explanation.

Sender role: {role}
Current step: {step}
{"Available products: " + json.dumps(products[:6]) if products else ""}
{"Active orders: " + json.dumps(orders[:5]) if orders else ""}

RULES:
- Understand English, Hindi, and Hinglish naturally
- Be smart about intent: "cake chahiye" = wants to order, "kya hai menu" = view menu
- Extract numbers and names from natural speech
- If ordering and they mention a product/size, match it

VALID ACTIONS for {role}:"""

    if role == "customer":
        system += """
- {"action":"WELCOME"} — greeting, hi, hello
- {"action":"START_ORDER"} — wants to order, cake chahiye, I want a cake
- {"action":"CHECK_STATUS"} — order status, track, kahan hai mera order
- {"action":"VIEW_MENU"} — menu, what do you have, kya kya hai
- {"action":"SELECT_OPTION","value":<number>} — selecting a numbered option
- {"action":"SELECT_PRODUCT_BY_NAME","name":"<name>"} — "chocolate cake", "vanilla premium"
- {"action":"SELECT_SIZE_BY_NAME","name":"<size>"} — "2 kg", "1.5kg", "half kg"
- {"action":"SELECT_FLAVOR_BY_NAME","name":"<flavor>"} — "strawberry", "pineapple"
- {"action":"SET_MESSAGE","text":"<cake message>"} — message for cake
- {"action":"SET_ADDRESS","text":"<address>"} — delivery address
- {"action":"SET_DATE","value":<1=tomorrow,2=day after>}
- {"action":"SET_TIME","value":<1=morning,2=afternoon,3=evening>}
- {"action":"CONFIRM_ORDER"} — confirm, yes, done, place it
- {"action":"CANCEL_ORDER"} — cancel, no, stop
- {"action":"SKIP"} — skip this step
- {"action":"UNKNOWN","reply":"<helpful reply>"}"""
    elif role == "baker":
        system += """
- {"action":"BAKER_START","order_id":<n>} — start baking, shuru karo
- {"action":"BAKER_DONE","order_id":<n>} — done, ho gaya, ready, tayyar
- {"action":"BAKER_QUEUE"} — show orders, kya hai
- {"action":"UNKNOWN","reply":"<help text>"}"""
    elif role == "rider":
        system += """
- {"action":"RIDER_PICKED","order_id":<n>} — picked up, utha liya, collected
- {"action":"RIDER_DELIVERED","order_id":<n>} — delivered, pahuncha diya, done
- {"action":"RIDER_QUEUE"} — show deliveries
- {"action":"UNKNOWN","reply":"<help text>"}"""
    elif role == "admin":
        system += """
- {"action":"ADMIN_APPROVE","order_id":<n>} — approve, ok, theek hai, ship karo
- {"action":"ADMIN_REJECT","order_id":<n>} — reject, wapas, no good
- {"action":"ADMIN_PAID","order_id":<n>} — payment received
- {"action":"ADMIN_CANCEL","order_id":<n>} — cancel order
- {"action":"ADMIN_ORDERS"} — today's summary, aaj ke orders
- {"action":"UNKNOWN","reply":"<help text>"}"""

    resp = httpx.post(
        GROQ_CHAT_URL,
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
            "max_tokens": 200,
        },
        timeout=10,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)
