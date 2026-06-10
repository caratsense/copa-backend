"""
AI Message Parser — Professional, Context-Aware
=================================================
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
SITE = settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "") if settings.WHATSAPP_TRACKING_BASE_URL else "cakeoclock.co.in"


def transcribe_voice(audio_url: str) -> str:
    if not settings.GROQ_API_KEY:
        return ""
    try:
        audio_resp = httpx.get(audio_url, headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}, timeout=30, follow_redirects=True)
        if audio_resp.status_code != 200:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_resp.content)
            path = f.name
        with open(path, "rb") as af:
            resp = httpx.post(GROQ_WHISPER_URL, headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"},
                files={"file": ("voice.ogg", af, "audio/ogg")},
                data={"model": "whisper-large-v3", "language": "hi", "response_format": "json"}, timeout=30)
        os.unlink(path)
        if resp.status_code == 200:
            text = resp.json().get("text", "").strip()
            logger.info(f"[VOICE] Transcribed: {text}")
            return text
        return ""
    except Exception as e:
        logger.error(f"[VOICE] Error: {e}")
        return ""


def get_audio_url(media_id: str) -> str:
    try:
        resp = httpx.get(f"https://graph.facebook.com/v21.0/{media_id}",
            headers={"Authorization": f"Bearer {settings.WHATSAPP_TOKEN}"}, timeout=10)
        return resp.json().get("url", "")
    except:
        return ""


def parse_message(message: str, role: str, context: dict) -> dict:
    quick = _quick_match(message, role, context)
    if quick:
        return quick
    if settings.GROQ_API_KEY:
        try:
            return _groq_parse(message, role, context)
        except Exception as e:
            logger.error(f"[AI] Groq failed: {e}")
    return {"action": "UNKNOWN"}


def _quick_match(message: str, role: str, context: dict) -> dict | None:
    msg = message.strip().upper()
    step = context.get("step", "IDLE")

    if msg in ("CANCEL", "STOP", "EXIT"):
        return {"action": "CANCEL_ORDER"}
    if msg in ("CONFIRM", "YES"):
        return {"action": "CONFIRM_ORDER"}
    if msg in ("SKIP", "NO"):
        return {"action": "SKIP"}

    if msg.isdigit():
        num = int(msg)
        if role == "customer":
            if step in (None, "IDLE"):
                if num == 1: return {"action": "START_ORDER"}
                if num == 2: return {"action": "CHECK_STATUS"}
                if num == 3: return {"action": "VIEW_MENU"}
            elif step == "AWAITING_STATUS_ID":
                return {"action": "STATUS_ORDER_ID", "order_id": num}
            elif step in ("SELECT_PRODUCT", "SELECT_SIZE", "SELECT_FLAVOR", "SELECT_DATE", "SELECT_TIME"):
                return {"action": "SELECT_OPTION", "value": num}

    # Customer greetings → always use welcome template
    if role == "customer" and step in (None, "IDLE"):
        if msg in ("HI", "HELLO", "HEY", "START", "NAMASTE", "HOLA"):
            return {"action": "WELCOME"}
        if msg in ("MENU", "3"):
            return {"action": "VIEW_MENU"}
        if any(w in msg for w in ("ORDER", "CAKE", "BUY", "CHAHIYE", "KARNA")):
            return {"action": "START_ORDER"}
        if any(w in msg for w in ("STATUS", "TRACK", "KAHAN")):
            return {"action": "CHECK_STATUS"}

    if role == "baker":
        words = msg.split()
        oid = next((int(w) for w in words if w.isdigit()), None)
        if any(w in msg for w in ("START", "BEGIN", "SHURU")) and oid: return {"action": "BAKER_START", "order_id": oid}
        if any(w in msg for w in ("DONE", "COMPLETE", "HO GAYA", "TAYYAR")) and oid: return {"action": "BAKER_DONE", "order_id": oid}
        if msg in ("QUEUE", "ORDERS"): return {"action": "BAKER_QUEUE"}

    if role == "rider":
        words = msg.split()
        oid = next((int(w) for w in words if w.isdigit()), None)
        if any(w in msg for w in ("PICKED", "UTHA", "COLLECTED")) and oid: return {"action": "RIDER_PICKED", "order_id": oid}
        if any(w in msg for w in ("DELIVERED", "PAHUNCHA", "DROP")) and oid: return {"action": "RIDER_DELIVERED", "order_id": oid}
        if msg in ("QUEUE", "DELIVERIES"): return {"action": "RIDER_QUEUE"}

    if role == "admin":
        words = msg.split()
        oid = next((int(w) for w in words if w.isdigit()), None)
        if any(w in msg for w in ("APPROVE", "OK", "THEEK")) and oid: return {"action": "ADMIN_APPROVE", "order_id": oid}
        if any(w in msg for w in ("REJECT", "WAPAS")) and oid: return {"action": "ADMIN_REJECT", "order_id": oid}
        if msg.startswith("PAID") and oid: return {"action": "ADMIN_PAID", "order_id": oid}
        if msg.startswith("CANCEL") and oid: return {"action": "ADMIN_CANCEL", "order_id": oid}
        if msg in ("ORDERS", "TODAY", "SUMMARY"): return {"action": "ADMIN_ORDERS"}

    if role == "customer":
        if step == "CAKE_MESSAGE": return {"action": "SET_MESSAGE", "text": message.strip()}
        if step == "DELIVERY_ADDRESS": return {"action": "SET_ADDRESS", "text": message.strip()}

    return None


def _groq_parse(message: str, role: str, context: dict) -> dict:
    step = context.get("step", "IDLE")
    products = context.get("products", [])

    system = f"""You are a professional WhatsApp assistant for Cake O' Clock — a premium bakery in Lucknow, India.

BUSINESS INFO:
- Founded by Shriya Mahendru
- 40+ flavors, premium handcrafted cakes
- Delivery across Lucknow
- Orders require minimum 24 hours advance notice
- Website: {SITE}
- Phone: +91 955 444 4462
- Payment: Online (UPI/Card) or Cash on Delivery

MENU: {json.dumps(products[:8]) if products else "Fetching..."}

TONE: Professional and courteous. Not overly casual, not robotic. Like a trained bakery staff member. Reply in the same language the customer uses (Hindi/English/Hinglish). Use maximum 1-2 emojis per message. Be concise — no filler text.

ROLE: {role} | STATE: {step}

Return ONLY a JSON object. No markdown, no explanation.

ACTIONS:"""

    if role == "customer":
        system += f"""
For greetings/general: {{"action":"CONVERSATIONAL","reply":"your professional reply using business info above"}}
To start ordering: {{"action":"START_ORDER"}}
To check status: {{"action":"CHECK_STATUS"}}
To view menu: {{"action":"VIEW_MENU"}}
During ordering steps:
  {{"action":"SELECT_PRODUCT_BY_NAME","name":"product name"}}
  {{"action":"SELECT_SIZE_BY_NAME","name":"2kg"}}
  {{"action":"SELECT_FLAVOR_BY_NAME","name":"strawberry"}}
  {{"action":"SET_MESSAGE","text":"Happy Birthday"}}
  {{"action":"SET_ADDRESS","text":"full address"}}
  {{"action":"SET_DATE","value":1}} (1=tomorrow, 2=day after)
  {{"action":"SET_TIME","value":2}} (1=morning, 2=afternoon, 3=evening)
  {{"action":"CONFIRM_ORDER"}}
  {{"action":"CANCEL_ORDER"}}
  {{"action":"SKIP"}}
  {{"action":"SELECT_OPTION","value":number}}
For questions about menu/price/delivery/timing: {{"action":"CONVERSATIONAL","reply":"answer using business info"}}

IMPORTANT: If someone asks a question (suggest, price, timing, delivery, flavors), use CONVERSATIONAL with a helpful professional reply. Only use START_ORDER if they clearly want to place an order."""
    elif role == "baker":
        system += """
{"action":"BAKER_START","order_id":N}
{"action":"BAKER_DONE","order_id":N}
{"action":"BAKER_QUEUE"}
{"action":"CONVERSATIONAL","reply":"helpful reply"}"""
    elif role == "rider":
        system += """
{"action":"RIDER_PICKED","order_id":N}
{"action":"RIDER_DELIVERED","order_id":N}
{"action":"RIDER_QUEUE"}
{"action":"CONVERSATIONAL","reply":"helpful reply"}"""
    elif role == "admin":
        system += """
{"action":"ADMIN_APPROVE","order_id":N}
{"action":"ADMIN_REJECT","order_id":N}
{"action":"ADMIN_PAID","order_id":N}
{"action":"ADMIN_CANCEL","order_id":N}
{"action":"ADMIN_ORDERS"}
{"action":"CONVERSATIONAL","reply":"helpful reply"}"""

    resp = httpx.post(GROQ_CHAT_URL,
        headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}", "Content-Type": "application/json"},
        json={"model": "llama-3.1-8b-instant",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": message}],
            "temperature": 0.15, "max_tokens": 300}, timeout=10)
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"]
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)
