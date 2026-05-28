"""
WhatsApp Webhook — receives messages, transcribes voice, routes by role.
"""

import json
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus
from app.schemas import StatusUpdate
from app.services.order_service import update_order_status, _enrich_order
from app.services.assignment_engine import auto_assign_baker, auto_assign_rider
from app.services.whatsapp_sender import send_text
from app.services.gemini_parser import parse_message, transcribe_voice, get_audio_url
from app.services.wa_customer_flow import handle_customer_message
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["Webhook"])


@router.get("/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        logger.info("[WA] Webhook verified")
        return PlainTextResponse(content=challenge, status_code=200)
    logger.warning(f"[WA] Verification failed: mode={mode}, token={token}")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def receive_whatsapp(request: Request):
    try:
        body = await request.json()
    except:
        return {"status": "invalid body"}

    logger.info(f"[WA] Payload: {json.dumps(body, default=str)[:500]}")

    entry = body.get("entry", [])
    if not entry:
        return {"status": "no entry"}

    changes = entry[0].get("changes", [])
    if not changes:
        return {"status": "no changes"}

    value = changes[0].get("value", {})
    messages = value.get("messages", [])

    # Status updates — acknowledge silently
    if value.get("statuses") and not messages:
        return {"status": "status update"}

    if not messages:
        return {"status": "no messages"}

    msg = messages[0]
    sender_phone = msg.get("from", "")
    msg_type = msg.get("type", "")
    text = ""

    # ─── EXTRACT TEXT FROM MESSAGE ────────
    if msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()

    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if "button_reply" in interactive:
            text = interactive["button_reply"].get("title", "")
        elif "list_reply" in interactive:
            text = interactive["list_reply"].get("title", "")

    elif msg_type == "audio":
        # Voice message → transcribe
        media_id = msg.get("audio", {}).get("id", "")
        if media_id:
            audio_url = get_audio_url(media_id)
            if audio_url:
                text = transcribe_voice(audio_url)
                if text:
                    logger.info(f"[WA] Voice transcribed from {sender_phone}: {text}")
                else:
                    send_text(sender_phone, "I couldn't understand that voice message. Could you type it instead? 🙏")
                    return {"status": "voice transcription failed"}
            else:
                send_text(sender_phone, "I couldn't process that voice message. Please type your message. 🙏")
                return {"status": "audio url failed"}
        else:
            send_text(sender_phone, "I couldn't process that voice message. Please type your message. 🙏")
            return {"status": "no media id"}

    elif msg_type == "image":
        send_text(sender_phone, "Thanks for the image! 📸 I can't process images yet, but you can describe what you need and I'll help.")
        return {"status": "image not supported"}

    else:
        send_text(sender_phone, "I can read text and voice messages. Please type or speak your message! 🙏")
        return {"status": "unsupported type"}

    if not text:
        return {"status": "empty"}

    logger.info(f"[WA] From {sender_phone}: {text}")

    # ─── ROUTE BY ROLE ────────────────────
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == f"+{sender_phone}").first()
        if not user:
            user = db.query(User).filter(User.phone == sender_phone).first()
        if not user:
            # Try with +91 prefix
            clean = sender_phone[-10:] if len(sender_phone) >= 10 else sender_phone
            user = db.query(User).filter(User.phone.endswith(clean)).first()

        if user and user.role == UserRole.ADMIN:
            reply = _handle_admin(db, user, text, sender_phone)
        elif user and user.role == UserRole.BAKER:
            reply = _handle_baker(db, user, text, sender_phone)
        elif user and user.role == UserRole.RIDER:
            reply = _handle_rider(db, user, text, sender_phone)
        elif user:
            reply = handle_customer_message(sender_phone, text, user)
        else:
            reply = _handle_new_user(db, sender_phone, text)

        send_text(sender_phone, reply)

    except Exception as e:
        logger.error(f"[WA] Error: {e}")
        send_text(sender_phone, "Oops! Something went wrong. Please try again in a moment. 🙏")
    finally:
        db.close()

    return {"status": "processed"}


# ─── ADMIN ────────────────────────────────

def _handle_admin(db: Session, user: User, text: str, phone: str) -> str:
    context = {"step": "ADMIN"}
    action = parse_message(text, "admin", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "ADMIN_APPROVE" and oid:
        order = db.query(Order).filter(Order.id == oid).first()
        if not order: return f"Order #{oid} not found."
        if order.status != OrderStatus.AWAITING_APPROVAL: return f"Order #{oid} is {order.status.value}, not awaiting approval."
        try:
            update_order_status(db, oid, StatusUpdate(status="PACKAGED"))
            try: auto_assign_rider(db, oid, force=True)
            except: pass
            return f"✅ Order #{oid} approved & packaged! 📦"
        except Exception as e: return f"Error: {e}"

    elif act == "ADMIN_REJECT" and oid:
        try:
            update_order_status(db, oid, StatusUpdate(status="IN_PRODUCTION"))
            return f"⚠️ Order #{oid} sent back to baker."
        except Exception as e: return f"Error: {e}"

    elif act == "ADMIN_PAID" and oid:
        try:
            from app.services.order_service import update_payment_status
            update_payment_status(db, oid, "PAID")
            return f"💰 Order #{oid} marked as PAID."
        except Exception as e: return f"Error: {e}"

    elif act == "ADMIN_CANCEL" and oid:
        try:
            update_order_status(db, oid, StatusUpdate(status="CANCELLED"))
            return f"❌ Order #{oid} cancelled."
        except Exception as e: return f"Error: {e}"

    elif act == "ADMIN_ORDERS":
        from datetime import datetime, timezone
        from sqlalchemy import func
        today = datetime.now(timezone.utc).date()
        total = db.query(func.count(Order.id)).filter(func.date(Order.created_at) == today).scalar() or 0
        revenue = db.query(func.sum(Order.total_price)).filter(func.date(Order.created_at) == today).scalar() or 0
        pending = db.query(func.count(Order.id)).filter(func.date(Order.created_at) == today, Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.AWAITING_APPROVAL])).scalar() or 0
        return f"📊 *Today's Summary*\n\n🧁 Orders: {total}\n💰 Revenue: ₹{revenue:,.0f}\n⏳ Pending: {pending}\n\n🌐 Dashboard: {settings.WHATSAPP_TRACKING_BASE_URL.replace('/track', '/admin')}"

    reply = action.get("reply", "")
    if reply: return reply
    return "🔑 *Admin Commands*\n\nAPPROVE {id} · REJECT {id} · PAID {id}\nCANCEL {id} · ORDERS\n\nOr just tell me what you need!"


# ─── BAKER ────────────────────────────────

def _handle_baker(db: Session, user: User, text: str, phone: str) -> str:
    context = {"step": "BAKER"}
    action = parse_message(text, "baker", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "BAKER_START" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_baker_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.ASSIGNED: return f"Order #{oid} is {order.status.value}, can't start."
        update_order_status(db, oid, StatusUpdate(status="IN_PRODUCTION"))
        return f"🧁 Started baking #{oid}! Let me know when it's done."

    elif act == "BAKER_DONE" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_baker_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.IN_PRODUCTION: return f"Order #{oid} is {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="AWAITING_APPROVAL"))
        return f"✅ #{oid} marked done! Waiting for approval. 👍"

    elif act == "BAKER_QUEUE":
        orders = db.query(Order).filter(Order.assigned_baker_id == user.id, Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION])).order_by(Order.delivery_time.asc().nullslast()).all()
        if not orders: return "🧁 No orders in your queue right now. Take a break! ☕"
        lines = ["🧁 *Your Queue:*\n"]
        for o in orders:
            _enrich_order(o)
            items = ", ".join([f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}" for i in o.items]) if o.items else "Cake"
            status = "⏳ Waiting" if o.status == OrderStatus.ASSIGNED else "🔥 Baking"
            dt = o.delivery_time.strftime("%I:%M %p") if o.delivery_time else "ASAP"
            lines.append(f"*#{o.id}* — {items}\n  {status} | By {dt}\n")
        return "\n".join(lines)

    reply = action.get("reply", "")
    if reply: return reply
    return "🧁 *Baker Panel*\n\nSTART {id} · DONE {id} · QUEUE\n\nOr just tell me what's up!"


# ─── RIDER ────────────────────────────────

def _handle_rider(db: Session, user: User, text: str, phone: str) -> str:
    context = {"step": "RIDER"}
    action = parse_message(text, "rider", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "RIDER_PICKED" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_rider_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.PACKAGED: return f"Order #{oid} is {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="OUT_FOR_DELIVERY"))
        return f"🛵 #{oid} picked up! Safe delivery! 🙏"

    elif act == "RIDER_DELIVERED" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_rider_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.OUT_FOR_DELIVERY: return f"Order #{oid} is {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="DELIVERED"))
        return f"✅ #{oid} delivered! Great job! 🎉"

    elif act == "RIDER_QUEUE":
        orders = db.query(Order).filter(Order.assigned_rider_id == user.id, Order.status.in_([OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY])).all()
        if not orders: return "🛵 No deliveries right now. Relax! ☕"
        lines = ["🛵 *Your Deliveries:*\n"]
        for o in orders:
            _enrich_order(o)
            status = "📦 Ready" if o.status == OrderStatus.PACKAGED else "🛵 On the way"
            addr = o.delivery_address or "Self Pickup"
            maps = f"https://www.google.com/maps/search/?api=1&query={addr.replace(' ', '+')}" if addr != "Self Pickup" else ""
            lines.append(f"*#{o.id}* — {status}\n  👤 {o.customer_name or 'Customer'}\n  📞 {o.user.phone if o.user else ''}\n  📍 {addr}")
            if maps: lines.append(f"  🗺️ {maps}")
            lines.append("")
        return "\n".join(lines)

    reply = action.get("reply", "")
    if reply: return reply
    return "🛵 *Rider Panel*\n\nPICKED {id} · DELIVERED {id} · QUEUE\n\nOr just tell me!"


# ─── NEW USER ─────────────────────────────

def _handle_new_user(db: Session, phone: str, text: str) -> str:
    from app.core.auth import hash_password
    import random, string

    if len(text) > 1 and not text.isdigit() and len(text) < 50:
        name = text.strip().title()
        phone_with_plus = f"+{phone}" if not phone.startswith("+") else phone
        pwd = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        new_user = User(name=name, phone=phone_with_plus, password_hash=hash_password(pwd), role=UserRole.CUSTOMER, is_active=True)
        db.add(new_user)
        db.commit()

        return (
            f"✅ Welcome, {name}! 🎉\n\n"
            f"Your account is ready.\n\n"
            f"What would you like to do?\n"
            f"1️⃣ Order a Cake\n"
            f"2️⃣ Check Order Status\n"
            f"3️⃣ View Menu\n\n"
            f"🌐 Or browse online: {settings.WHATSAPP_TRACKING_BASE_URL.replace('/track', '')}"
        )

    return (
        f"👋 Hey! Welcome to *Cake O' Clock* 🧁\n\n"
        f"We craft premium cakes with love in Lucknow ❤️\n\n"
        f"🎂 40+ flavors · 📦 Doorstep delivery\n\n"
        f"To get started, please share your name!"
    )
