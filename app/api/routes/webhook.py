"""
WhatsApp Webhook Handler
=========================
Receives all incoming WhatsApp messages via Meta Cloud API webhook.
Routes messages based on sender's role (admin/baker/rider/customer).
Also handles webhook verification from Meta.
"""

import logging
from fastapi import APIRouter, Request, HTTPException, Depends, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus
from app.schemas import StatusUpdate
from app.services.order_service import update_order_status, _enrich_order
from app.services.assignment_engine import auto_assign_baker, auto_assign_rider
from app.services.whatsapp_sender import send_text
from app.services.gemini_parser import parse_message
from app.services.wa_customer_flow import handle_customer_message
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhook"])


# ─── WEBHOOK VERIFICATION (GET) ─────────────────────────

@router.get("/whatsapp")
async def verify_webhook(
    request: Request,
):
    """Meta sends a GET request to verify the webhook URL."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        logger.info("[WA WEBHOOK] Verification successful")
        return PlainTextResponse(content=challenge, status_code=200)

    logger.warning(f"[WA WEBHOOK] Verification failed: mode={mode}, token={token}")
    raise HTTPException(status_code=403, detail="Verification failed")


# ─── INCOMING MESSAGES (POST) ────────────────────────────

@router.post("/whatsapp")
async def receive_whatsapp(request: Request):
    """Receive incoming WhatsApp messages and route to correct handler."""
    body = await request.json()

    # Extract message from webhook payload
    entry = body.get("entry", [{}])[0]
    changes = entry.get("changes", [{}])[0]
    value = changes.get("value", {})
    messages = value.get("messages", [])

    if not messages:
        return {"status": "no messages"}

    msg = messages[0]
    sender_phone = msg.get("from", "")
    msg_type = msg.get("type", "")
    text = ""

    if msg_type == "text":
        text = msg.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        # Button/list replies
        interactive = msg.get("interactive", {})
        if "button_reply" in interactive:
            text = interactive["button_reply"].get("title", "")
        elif "list_reply" in interactive:
            text = interactive["list_reply"].get("title", "")
    else:
        # Ignore images, audio, etc for now
        send_text(sender_phone, "Sorry, I can only read text messages right now. Please type your message.")
        return {"status": "unsupported type"}

    if not text:
        return {"status": "empty message"}

    logger.info(f"[WA] From {sender_phone}: {text}")

    # ─── ROUTE BY SENDER ROLE ────────────────────────
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == f"+{sender_phone}").first()
        if not user:
            user = db.query(User).filter(User.phone == sender_phone).first()

        if user and user.role == UserRole.ADMIN:
            reply = _handle_admin(db, user, text, sender_phone)
        elif user and user.role == UserRole.BAKER:
            reply = _handle_baker(db, user, text, sender_phone)
        elif user and user.role == UserRole.RIDER:
            reply = _handle_rider(db, user, text, sender_phone)
        elif user:
            # Existing customer
            reply = handle_customer_message(sender_phone, text, user)
        else:
            # New user — auto-register and start ordering
            reply = _handle_new_user(db, sender_phone, text)

        send_text(sender_phone, reply)

    except Exception as e:
        logger.error(f"[WA] Error processing message from {sender_phone}: {e}")
        send_text(sender_phone, "Sorry, something went wrong. Please try again.")
    finally:
        db.close()

    return {"status": "processed"}


# ─── ADMIN HANDLER ───────────────────────────────────────

def _handle_admin(db: Session, user: User, text: str, phone: str) -> str:
    context = {"step": "ADMIN"}
    action = parse_message(text, "admin", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "ADMIN_APPROVE" and oid:
        try:
            order = db.query(Order).filter(Order.id == oid).first()
            if not order:
                return f"Order #{oid} not found."
            if order.status != OrderStatus.AWAITING_APPROVAL:
                return f"Order #{oid} is {order.status.value}, not awaiting approval."
            update_order_status(db, oid, StatusUpdate(status="PACKAGED"))
            # Try auto-assign rider
            try:
                auto_assign_rider(db, oid, force=True)
                return f"✅ Order #{oid} approved & packaged! Rider assigned."
            except:
                return f"✅ Order #{oid} approved & packaged! No rider available — assign manually."
        except Exception as e:
            return f"Error: {e}"

    elif act == "ADMIN_REJECT" and oid:
        try:
            update_order_status(db, oid, StatusUpdate(status="IN_PRODUCTION"))
            return f"⚠️ Order #{oid} sent back to baker."
        except Exception as e:
            return f"Error: {e}"

    elif act == "ADMIN_PAID" and oid:
        try:
            from app.services.order_service import update_payment_status
            update_payment_status(db, oid, "PAID")
            return f"💰 Order #{oid} marked as PAID."
        except Exception as e:
            return f"Error: {e}"

    elif act == "ADMIN_CANCEL" and oid:
        try:
            update_order_status(db, oid, StatusUpdate(status="CANCELLED"))
            return f"❌ Order #{oid} cancelled."
        except Exception as e:
            return f"Error: {e}"

    elif act == "ADMIN_ASSIGN_BAKER" and oid:
        try:
            auto_assign_baker(db, oid, force=True)
            return f"👨‍🍳 Baker assigned to order #{oid}."
        except Exception as e:
            return f"Error: {e}"

    elif act == "ADMIN_ASSIGN_RIDER" and oid:
        try:
            auto_assign_rider(db, oid, force=True)
            return f"🛵 Rider assigned to order #{oid}."
        except Exception as e:
            return f"Error: {e}"

    elif act == "ADMIN_ORDERS":
        from datetime import datetime, timezone
        from sqlalchemy import func
        today = datetime.now(timezone.utc).date()
        total_orders = db.query(func.count(Order.id)).filter(func.date(Order.created_at) == today).scalar() or 0
        total_revenue = db.query(func.sum(Order.total_price)).filter(func.date(Order.created_at) == today).scalar() or 0
        pending = db.query(func.count(Order.id)).filter(func.date(Order.created_at) == today, Order.status.in_([OrderStatus.CONFIRMED, OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.AWAITING_APPROVAL])).scalar() or 0
        return (
            f"📊 *Today's Summary*\n\n"
            f"Orders: {total_orders}\n"
            f"Revenue: ₹{total_revenue:,.0f}\n"
            f"Pending: {pending}\n"
        )

    return (
        "🔑 *Admin Commands:*\n\n"
        "APPROVE {id} — Approve baked order\n"
        "REJECT {id} — Send back to baker\n"
        "PAID {id} — Mark payment received\n"
        "CANCEL {id} — Cancel order\n"
        "ASSIGN BAKER {id}\n"
        "ASSIGN RIDER {id}\n"
        "ORDERS — Today's summary"
    )


# ─── BAKER HANDLER ───────────────────────────────────────

def _handle_baker(db: Session, user: User, text: str, phone: str) -> str:
    context = {"step": "BAKER"}
    action = parse_message(text, "baker", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "BAKER_START" and oid:
        try:
            order = db.query(Order).filter(Order.id == oid, Order.assigned_baker_id == user.id).first()
            if not order:
                return f"Order #{oid} not found or not assigned to you."
            if order.status != OrderStatus.ASSIGNED:
                return f"Order #{oid} is {order.status.value}, can't start."
            update_order_status(db, oid, StatusUpdate(status="IN_PRODUCTION"))
            return f"🧁 Started baking order #{oid}! Reply DONE {oid} when finished."
        except Exception as e:
            return f"Error: {e}"

    elif act == "BAKER_DONE" and oid:
        try:
            order = db.query(Order).filter(Order.id == oid, Order.assigned_baker_id == user.id).first()
            if not order:
                return f"Order #{oid} not found or not assigned to you."
            if order.status != OrderStatus.IN_PRODUCTION:
                return f"Order #{oid} is {order.status.value}, can't mark done."
            update_order_status(db, oid, StatusUpdate(status="AWAITING_APPROVAL"))
            return f"✅ Order #{oid} marked as done! Waiting for approval."
        except Exception as e:
            return f"Error: {e}"

    elif act == "BAKER_QUEUE":
        orders = db.query(Order).filter(
            Order.assigned_baker_id == user.id,
            Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION])
        ).order_by(Order.delivery_time.asc().nullslast()).all()

        if not orders:
            return "🧁 No orders in your queue right now."

        lines = ["🧁 *Your Queue:*\n"]
        for o in orders:
            _enrich_order(o)
            items = ", ".join([f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}" for i in o.items]) if o.items else "Cake"
            status = "⏳ Waiting" if o.status == OrderStatus.ASSIGNED else "🔥 Baking"
            dt = o.delivery_time.strftime("%I:%M %p") if o.delivery_time else "ASAP"
            lines.append(f"#{o.id} — {items} | {status} | By {dt}")
        lines.append(f"\nSTART {{id}} — Begin baking\nDONE {{id}} — Mark complete")
        return "\n".join(lines)

    return (
        "🧁 *Baker Commands:*\n\n"
        "START {id} — Begin baking\n"
        "DONE {id} — Mark complete\n"
        "QUEUE — Show your orders"
    )


# ─── RIDER HANDLER ───────────────────────────────────────

def _handle_rider(db: Session, user: User, text: str, phone: str) -> str:
    context = {"step": "RIDER"}
    action = parse_message(text, "rider", context)
    act = action.get("action", "UNKNOWN")
    oid = action.get("order_id")

    if act == "RIDER_PICKED" and oid:
        try:
            order = db.query(Order).filter(Order.id == oid, Order.assigned_rider_id == user.id).first()
            if not order:
                return f"Order #{oid} not found or not assigned to you."
            if order.status != OrderStatus.PACKAGED:
                return f"Order #{oid} is {order.status.value}, can't pickup."
            update_order_status(db, oid, StatusUpdate(status="OUT_FOR_DELIVERY"))
            return f"🛵 Order #{oid} picked up! Reply DELIVERED {oid} when done."
        except Exception as e:
            return f"Error: {e}"

    elif act == "RIDER_DELIVERED" and oid:
        try:
            order = db.query(Order).filter(Order.id == oid, Order.assigned_rider_id == user.id).first()
            if not order:
                return f"Order #{oid} not found or not assigned to you."
            if order.status != OrderStatus.OUT_FOR_DELIVERY:
                return f"Order #{oid} is {order.status.value}, can't deliver."
            update_order_status(db, oid, StatusUpdate(status="DELIVERED"))
            return f"✅ Order #{oid} delivered! Great job!"
        except Exception as e:
            return f"Error: {e}"

    elif act == "RIDER_QUEUE":
        orders = db.query(Order).filter(
            Order.assigned_rider_id == user.id,
            Order.status.in_([OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY])
        ).order_by(Order.created_at.asc()).all()

        if not orders:
            return "🛵 No deliveries right now."

        lines = ["🛵 *Your Deliveries:*\n"]
        for o in orders:
            _enrich_order(o)
            status = "📦 Ready" if o.status == OrderStatus.PACKAGED else "🛵 On the way"
            addr = o.delivery_address or "Self Pickup"
            maps = f"https://www.google.com/maps/search/?api=1&query={addr.replace(' ', '+')}" if addr != "Self Pickup" else ""
            lines.append(
                f"#{o.id} — {status}\n"
                f"  👤 {o.customer_name or 'Customer'} | 📞 {o.user.phone if o.user else ''}\n"
                f"  📍 {addr}\n"
                f"  {('🗺️ ' + maps) if maps else ''}"
            )
        lines.append(f"\nPICKED {{id}} — Confirm pickup\nDELIVERED {{id}} — Mark delivered")
        return "\n".join(lines)

    return (
        "🛵 *Rider Commands:*\n\n"
        "PICKED {id} — Confirm pickup\n"
        "DELIVERED {id} — Mark delivered\n"
        "QUEUE — Show your deliveries"
    )


# ─── NEW USER HANDLER ────────────────────────────────────

def _handle_new_user(db: Session, phone: str, text: str) -> str:
    """Auto-register new users from WhatsApp."""
    from app.core.auth import hash_password
    import random
    import string

    # Check if they sent their name
    if len(text) > 1 and not text.isdigit() and len(text) < 50:
        name = text.strip().title()
        phone_with_plus = f"+{phone}" if not phone.startswith("+") else phone
        random_pwd = "".join(random.choices(string.ascii_letters + string.digits, k=12))

        new_user = User(
            name=name,
            phone=phone_with_plus,
            password_hash=hash_password(random_pwd),
            role=UserRole.CUSTOMER,
            is_active=True,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        return (
            f"✅ Welcome {name}! Account created.\n\n"
            f"What would you like to do?\n"
            f"1️⃣ Order a Cake\n"
            f"2️⃣ Check Order Status\n"
            f"3️⃣ View Menu"
        )

    return (
        "👋 Welcome to *Cake O' Clock*! 🧁\n"
        "Made with Love by Shriya Mahendru\n\n"
        "To get started, please share your name."
    )
