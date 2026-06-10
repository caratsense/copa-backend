"""
WhatsApp Webhook — Professional, handles text/voice/image.
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
SITE = settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "") if settings.WHATSAPP_TRACKING_BASE_URL else ""


@router.get("/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        logger.info("[WA] Webhook verified")
        return PlainTextResponse(content=params.get("hub.challenge"), status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
async def receive_whatsapp(request: Request):
    try:
        body = await request.json()
    except:
        return {"status": "invalid"}

    entry = body.get("entry", [])
    if not entry: return {"status": "no entry"}
    changes = entry[0].get("changes", [])
    if not changes: return {"status": "no changes"}
    value = changes[0].get("value", {})
    messages = value.get("messages", [])
    if value.get("statuses") and not messages: return {"status": "status update"}
    if not messages: return {"status": "no messages"}

    msg = messages[0]
    sender = msg.get("from", "")
    msg_type = msg.get("type", "")
    text = ""

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        text = interactive.get("button_reply", {}).get("title", "") or interactive.get("list_reply", {}).get("title", "")
    elif msg_type == "audio":
        media_id = msg.get("audio", {}).get("id", "")
        if media_id:
            url = get_audio_url(media_id)
            if url: text = transcribe_voice(url)
        if not text:
            send_text(sender, "Could not process the voice message. Please type your message.")
            return {"status": "voice failed"}
    elif msg_type == "image":
        send_text(sender, "We cannot process images at the moment. Please describe what you need.")
        return {"status": "image"}
    elif msg_type == "reaction":
        return {"status": "reaction ignored"}
    else:
        send_text(sender, "Please send a text or voice message.")
        return {"status": "unsupported"}

    if not text: return {"status": "empty"}
    logger.info(f"[WA] {sender}: {text}")

    db = SessionLocal()
    try:
        # Find user by phone
        user = db.query(User).filter(User.phone == f"+{sender}").first()
        if not user:
            user = db.query(User).filter(User.phone == sender).first()
        if not user:
            clean = sender[-10:] if len(sender) >= 10 else sender
            user = db.query(User).filter(User.phone.endswith(clean)).first()

        if user and user.role == UserRole.ADMIN:
            reply = _handle_admin(db, user, text)
        elif user and user.role == UserRole.BAKER:
            reply = _handle_baker(db, user, text)
        elif user and user.role == UserRole.RIDER:
            reply = _handle_rider(db, user, text)
        elif user:
            reply = handle_customer_message(sender, text, user)
        else:
            reply = _handle_new_user(db, sender, text)

        send_text(sender, reply)
    except Exception as e:
        logger.error(f"[WA] Error: {e}")
        send_text(sender, "We're experiencing a temporary issue. Please try again shortly.")
    finally:
        db.close()

    return {"status": "ok"}


def _handle_admin(db, user, text):
    context = {"step": "ADMIN"}
    action = parse_message(text, "admin", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "ADMIN_APPROVE" and oid:
        order = db.query(Order).filter(Order.id == oid).first()
        if not order: return f"Order #{oid} not found."
        if order.status != OrderStatus.AWAITING_APPROVAL: return f"Order #{oid} — status is {order.status.value}. Cannot approve."
        update_order_status(db, oid, StatusUpdate(status="PACKAGED"))
        try: auto_assign_rider(db, oid, force=True)
        except: pass
        return f"Order #{oid} approved and packaged."

    elif act == "ADMIN_REJECT" and oid:
        update_order_status(db, oid, StatusUpdate(status="IN_PRODUCTION"))
        return f"Order #{oid} sent back to baker."

    elif act == "ADMIN_PAID" and oid:
        from app.services.order_service import update_payment_status
        update_payment_status(db, oid, "PAID")
        return f"Order #{oid} marked as paid."

    elif act == "ADMIN_CANCEL" and oid:
        update_order_status(db, oid, StatusUpdate(status="CANCELLED"))
        return f"Order #{oid} cancelled."

    elif act == "ADMIN_ORDERS":
        from datetime import datetime, timezone
        from sqlalchemy import func
        today = datetime.now(timezone.utc).date()
        total = db.query(func.count(Order.id)).filter(func.date(Order.created_at) == today).scalar() or 0
        revenue = db.query(func.sum(Order.total_price)).filter(func.date(Order.created_at) == today).scalar() or 0
        pending = db.query(func.count(Order.id)).filter(func.date(Order.created_at) == today,
            Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.AWAITING_APPROVAL])).scalar() or 0
        return f"Today's Summary\n\nOrders: {total}\nRevenue: ₹{revenue:,.0f}\nPending: {pending}\n\nDashboard: {SITE}/admin"

    elif act == "CONVERSATIONAL":
        return action.get("reply", "How can I help?")

    return "Commands: APPROVE {id} · REJECT {id} · PAID {id} · CANCEL {id} · ORDERS"


def _handle_baker(db, user, text):
    context = {"step": "BAKER"}
    action = parse_message(text, "baker", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "BAKER_START" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_baker_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.ASSIGNED: return f"Order #{oid} — cannot start. Status: {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="IN_PRODUCTION"))
        return f"Order #{oid} — production started."

    elif act == "BAKER_DONE" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_baker_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.IN_PRODUCTION: return f"Order #{oid} — cannot mark done. Status: {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="AWAITING_APPROVAL"))
        return f"Order #{oid} — marked complete. Awaiting approval."

    elif act == "BAKER_QUEUE":
        orders = db.query(Order).filter(Order.assigned_baker_id == user.id,
            Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION])).order_by(Order.delivery_time.asc().nullslast()).all()
        if not orders: return "No orders in your queue."
        lines = ["*Your Queue*\n"]
        for o in orders:
            _enrich_order(o)
            items = ", ".join([f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}" for i in o.items]) if o.items else "Cake"
            status = "Waiting" if o.status == OrderStatus.ASSIGNED else "In production"
            dt = o.delivery_time.strftime("%I:%M %p") if o.delivery_time else "ASAP"
            lines.append(f"#{o.id} — {items}\nStatus: {status} | By {dt}\n")
        return "\n".join(lines)

    elif act == "CONVERSATIONAL":
        return action.get("reply", "Commands: START {id} · DONE {id} · QUEUE")

    return "Commands: START {id} · DONE {id} · QUEUE"


def _handle_rider(db, user, text):
    context = {"step": "RIDER"}
    action = parse_message(text, "rider", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "RIDER_PICKED" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_rider_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.PACKAGED: return f"Order #{oid} — cannot pick up. Status: {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="OUT_FOR_DELIVERY"))
        return f"Order #{oid} — picked up. Deliver safely."

    elif act == "RIDER_DELIVERED" and oid:
        order = db.query(Order).filter(Order.id == oid, Order.assigned_rider_id == user.id).first()
        if not order: return f"Order #{oid} not found or not assigned to you."
        if order.status != OrderStatus.OUT_FOR_DELIVERY: return f"Order #{oid} — cannot mark delivered. Status: {order.status.value}."
        update_order_status(db, oid, StatusUpdate(status="DELIVERED"))
        return f"Order #{oid} — delivered successfully."

    elif act == "RIDER_QUEUE":
        orders = db.query(Order).filter(Order.assigned_rider_id == user.id,
            Order.status.in_([OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY])).all()
        if not orders: return "No deliveries assigned."
        lines = ["*Your Deliveries*\n"]
        for o in orders:
            _enrich_order(o)
            status = "Ready for pickup" if o.status == OrderStatus.PACKAGED else "Out for delivery"
            addr = o.delivery_address or "Self Pickup"
            maps = f"https://www.google.com/maps/search/?api=1&query={addr.replace(' ', '+')}" if addr != "Self Pickup" else ""
            lines.append(f"#{o.id} — {status}\nCustomer: {o.customer_name or 'Customer'}\nPhone: {o.user.phone if o.user else 'N/A'}\nAddress: {addr}")
            if maps: lines.append(f"Map: {maps}")
            lines.append("")
        return "\n".join(lines)

    elif act == "CONVERSATIONAL":
        return action.get("reply", "Commands: PICKED {id} · DELIVERED {id} · QUEUE")

    return "Commands: PICKED {id} · DELIVERED {id} · QUEUE"


def _handle_new_user(db, phone, text):
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
            f"Welcome, {name}. Your account has been created.\n\n"
            f"How can I assist you?\n"
            f"1. Place an order\n"
            f"2. Track an order\n"
            f"3. View our menu\n\n"
            f"{SITE}"
        )

    return (
        f"Welcome to Cake O' Clock.\n\n"
        f"We offer premium handcrafted cakes, delivered across Lucknow.\n\n"
        f"To get started, please share your name."
    )
