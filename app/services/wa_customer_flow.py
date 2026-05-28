"""
Customer WhatsApp Ordering Flow
================================
Conversational, AI-powered, fetches real-time data from DB.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.product import Product
from app.models.pricing import SizeRule, FlavorRule
from app.models.user import User, UserRole
from app.models.order import Order
from app.services.gemini_parser import parse_message
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)
_memory: dict = {}

SITE_URL = settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "")


def _get_redis():
    try:
        import redis
        r = redis.Redis(host="redis", port=6379, db=2, decode_responses=True)
        r.ping()
        return r
    except:
        try:
            import redis
            r = redis.from_url(settings.REDIS_URL, db=2, decode_responses=True)
            r.ping()
            return r
        except:
            return None


def _get_state(phone: str) -> dict:
    r = _get_redis()
    if r:
        raw = r.get(f"wa:{phone}")
        return json.loads(raw) if raw else {"step": "IDLE"}
    return _memory.get(phone, {"step": "IDLE"})


def _set_state(phone: str, state: dict):
    r = _get_redis()
    if r:
        r.setex(f"wa:{phone}", 3600, json.dumps(state))
    else:
        _memory[phone] = state


def _clear_state(phone: str):
    r = _get_redis()
    if r: r.delete(f"wa:{phone}")
    else: _memory.pop(phone, None)


def handle_customer_message(phone: str, message: str, user: Optional[User]) -> str:
    state = _get_state(phone)
    step = state.get("step", "IDLE")
    db = SessionLocal()

    try:
        # Fetch real-time products
        products = [{"id": p.id, "name": p.name, "base_price": p.base_price}
                    for p in db.query(Product).filter(Product.is_available == True).all()]

        context = {"step": step, "products": products}
        action = parse_message(message, "customer", context)
        act = action.get("action", "UNKNOWN")

        # ─── WELCOME ─────────────────────────
        if act == "WELCOME" or (step == "IDLE" and act == "UNKNOWN"):
            name = user.name if user else "there"
            _set_state(phone, {"step": "IDLE"})
            return (
                f"👋 Hey {name}! Welcome to *Cake O' Clock* 🧁\n\n"
                f"We craft premium cakes with love in Lucknow ❤️\n\n"
                f"🎂 40+ flavors · 📦 Doorstep delivery\n\n"
                f"What can I help you with?\n"
                f"1️⃣ Order a Cake\n"
                f"2️⃣ Check Order Status\n"
                f"3️⃣ View Menu\n\n"
                f"🌐 Browse online: {SITE_URL}"
            )

        # ─── VIEW MENU ───────────────────────
        if act == "VIEW_MENU":
            lines = ["🎂 *Our Menu*\n"]
            for p in products:
                lines.append(f"• *{p['name']}* — ₹{p['base_price']:,.0f}/kg")
            lines.append(f"\n40+ flavors available!")
            lines.append(f"\n🌐 Full menu: {SITE_URL}/menu")
            lines.append(f"\nWant to order? Just say *order* or type *1*!")
            return "\n".join(lines)

        # ─── CHECK STATUS ────────────────────
        if act == "CHECK_STATUS":
            _set_state(phone, {"step": "AWAITING_STATUS_ID"})
            return "Sure! What's your order number? 🔍"

        if act == "STATUS_ORDER_ID":
            oid = action.get("order_id")
            _set_state(phone, {"step": "IDLE"})
            if oid:
                order = db.query(Order).filter(Order.id == oid).first()
                if order:
                    from app.services.order_service import _enrich_order
                    _enrich_order(order)
                    items = ", ".join([f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}" for i in order.items]) if order.items else "Cake"
                    emoji = {"CONFIRMED": "📋", "ASSIGNED": "👨‍🍳", "IN_PRODUCTION": "🧁", "AWAITING_APPROVAL": "✅", "PACKAGED": "📦", "OUT_FOR_DELIVERY": "🛵", "DELIVERED": "🎉", "CANCELLED": "❌"}.get(order.status.value, "📋")
                    return (
                        f"📦 *Order #{order.id}*\n\n"
                        f"🎂 {items}\n"
                        f"💰 ₹{order.total_price:,.0f}\n"
                        f"{emoji} Status: *{order.status.value.replace('_', ' ')}*\n\n"
                        f"🔗 Track live: {SITE_URL}/track?id={order.id}"
                    )
                return f"Couldn't find order #{oid}. Please check the number. 🤔"
            return "Please send me a valid order number."

        # ─── START ORDER ─────────────────────
        if act == "START_ORDER":
            lines = ["Great choice! 🎂 Let's build your perfect cake.\n\nHere's what we have:\n"]
            for i, p in enumerate(products, 1):
                lines.append(f"{i}️⃣ *{p['name']}* — ₹{p['base_price']:,.0f}/kg")
            lines.append(f"\nJust tell me which one, or type the number!")
            _set_state(phone, {"step": "SELECT_PRODUCT", "products": products})
            return "\n".join(lines)

        # ─── SELECT PRODUCT ──────────────────
        if step == "SELECT_PRODUCT":
            prods = state.get("products", products)
            selected = None

            if act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(prods): selected = prods[idx]
            elif act == "SELECT_PRODUCT_BY_NAME":
                name = action.get("name", "").lower()
                for p in prods:
                    if name in p["name"].lower(): selected = p; break

            if not selected:
                return f"Hmm, I didn't catch that. Pick a number between 1 and {len(prods)}, or tell me the cake name! 🎂"

            sizes = [{"id": s.id, "name": s.name, "multiplier": s.multiplier}
                     for s in db.query(SizeRule).filter(SizeRule.is_active == True).all()]
            lines = [f"Nice! *{selected['name']}* 😍\n\nWhat size would you like?\n"]
            for i, s in enumerate(sizes, 1):
                lines.append(f"{i}️⃣ {s['name']}")
            _set_state(phone, {**state, "step": "SELECT_SIZE", "product": selected, "sizes": sizes})
            return "\n".join(lines)

        # ─── SELECT SIZE ─────────────────────
        if step == "SELECT_SIZE":
            sizes = state.get("sizes", [])
            selected = None
            if act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(sizes): selected = sizes[idx]
            elif act == "SELECT_SIZE_BY_NAME":
                name = action.get("name", "").lower()
                for s in sizes:
                    if name in s["name"].lower(): selected = s; break

            if not selected:
                return f"Pick a size (1 to {len(sizes)}) or tell me like '2 kg'! 📏"

            product = state.get("product", {})
            pid = product.get("id")
            flavors = [{"id": f.id, "name": f.name, "extra_cost": f.extra_cost}
                       for f in db.query(FlavorRule).filter(FlavorRule.is_active == True).all()]
            # Limit to 10 for WhatsApp readability
            flavors = flavors[:10]
            lines = [f"*{selected['name']}* it is! 📏\n\nNow pick a flavor:\n"]
            for i, f in enumerate(flavors, 1):
                cost = f" (+₹{f['extra_cost']})" if f['extra_cost'] > 0 else ""
                lines.append(f"{i}️⃣ {f['name']}{cost}")
            _set_state(phone, {**state, "step": "SELECT_FLAVOR", "size": selected, "flavors": flavors})
            return "\n".join(lines)

        # ─── SELECT FLAVOR ───────────────────
        if step == "SELECT_FLAVOR":
            flavors = state.get("flavors", [])
            selected = None
            if act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(flavors): selected = flavors[idx]
            elif act == "SELECT_FLAVOR_BY_NAME":
                name = action.get("name", "").lower()
                for f in flavors:
                    if name in f["name"].lower(): selected = f; break

            if not selected:
                return f"Which flavor? Pick 1 to {len(flavors)} or tell me the name! 🍓"

            _set_state(phone, {**state, "step": "CAKE_MESSAGE", "flavor": selected})
            return "Yum! 🍓\n\nAny message on the cake? 💬\n(Type your message or say *skip*)"

        # ─── CAKE MESSAGE ────────────────────
        if step == "CAKE_MESSAGE":
            cake_msg = "" if act == "SKIP" else message.strip()
            _set_state(phone, {**state, "step": "DELIVERY_ADDRESS", "cake_message": cake_msg})
            return "📍 *Delivery address?*\n\nSend your full address with landmark.\nOr type *pickup* for self-pickup."

        # ─── ADDRESS ─────────────────────────
        if step == "DELIVERY_ADDRESS":
            addr = message.strip()
            is_pickup = addr.upper() in ("PICKUP", "SELF PICKUP", "SELF-PICKUP")
            _set_state(phone, {**state, "step": "SELECT_DATE", "address": "Self Pickup" if is_pickup else addr})
            return "📅 *When do you want it?*\n\n1️⃣ Tomorrow\n2️⃣ Day after tomorrow\n3️⃣ 3 days from now"

        # ─── DATE ────────────────────────────
        if step == "SELECT_DATE":
            val = action.get("value", 1) if act == "SELECT_OPTION" else 1
            days = max(1, min(val, 7))
            d = datetime.now() + timedelta(days=days)
            _set_state(phone, {**state, "step": "SELECT_TIME", "delivery_date": d.strftime("%Y-%m-%d")})
            return "⏰ *Preferred time?*\n\n1️⃣ Morning (8AM-12PM)\n2️⃣ Afternoon (12PM-4PM)\n3️⃣ Evening (4PM-8PM)"

        # ─── TIME ────────────────────────────
        if step == "SELECT_TIME":
            val = action.get("value", 2) if act == "SELECT_OPTION" else 2
            hours = {1: 10, 2: 14, 3: 18}.get(val, 14)
            time_label = {1: "Morning", 2: "Afternoon", 3: "Evening"}.get(val, "Afternoon")

            product = state.get("product", {})
            size = state.get("size", {})
            flavor = state.get("flavor", {})
            addr = state.get("address", "")
            cake_msg = state.get("cake_message", "")
            ddate = state.get("delivery_date", "")
            base = product.get("base_price", 0)
            total = (base + flavor.get("extra_cost", 0)) * size.get("multiplier", 1)

            summary = (
                f"📋 *Order Summary*\n\n"
                f"🎂 *{product.get('name', 'Cake')}*\n"
                f"📏 {size.get('name', '1kg')}\n"
                f"🍓 {flavor.get('name', 'Classic')}\n"
            )
            if cake_msg: summary += f'💬 "{cake_msg}"\n'
            summary += (
                f"📍 {addr}\n"
                f"📅 {ddate} — {time_label}\n\n"
                f"💰 *Total: ₹{total:,.0f}*\n\n"
                f"Type *confirm* to place order\n"
                f"Type *cancel* to start over"
            )
            _set_state(phone, {**state, "step": "CONFIRM", "time_hours": hours, "time_label": time_label, "total": total})
            return summary

        # ─── CONFIRM ─────────────────────────
        if step == "CONFIRM" and act == "CONFIRM_ORDER":
            if not user:
                _clear_state(phone)
                return "Please register first! Send your name to create an account. 😊"

            product = state.get("product", {})
            size = state.get("size", {})
            flavor = state.get("flavor", {})
            total = state.get("total", 0)
            addr = state.get("address", "Self Pickup")
            cake_msg = state.get("cake_message", "")
            ddate = state.get("delivery_date", "")
            time_hours = state.get("time_hours", 14)

            try:
                from app.schemas import OrderCreate, OrderItemCreate
                from app.services.order_service import create_order
                order_data = OrderCreate(
                    items=[OrderItemCreate(
                        product_id=product.get("id", 1), quantity=1,
                        customization={"size": size.get("name", "1kg"), "flavor": flavor.get("name", "Classic"), "design": "Basic Cream Finish", "addons": [], "rush": "Standard (24hr+)"},
                    )],
                    delivery_address=addr if addr != "Self Pickup" else None,
                    delivery_time=f"{ddate}T{time_hours:02d}:00:00",
                    notes=cake_msg or None,
                )
                order = create_order(db, order_data, user)
                _clear_state(phone)

                # Notify admin
                from app.services.whatsapp_sender import notify_admin_new_order
                admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
                items_str = f"{size.get('name', '1kg')} {flavor.get('name', '')} {product.get('name', 'Cake')}"
                delivery_str = f"{ddate} {state.get('time_label', '')}"
                for admin in admins:
                    notify_admin_new_order(admin.phone, order.id, user.name, phone, items_str, total, delivery_str)

                return (
                    f"🎉 *Order #{order.id} Placed!*\n\n"
                    f"Thank you! Your cake will be ready on time. 🧁\n\n"
                    f"💰 Total: ₹{total:,.0f}\n"
                    f"🔗 Track: {SITE_URL}/track?id={order.id}\n\n"
                    f"We'll keep you updated! ❤️"
                )
            except Exception as e:
                logger.error(f"[WA ORDER] Failed: {e}")
                _clear_state(phone)
                return f"Oops, something went wrong placing your order. Please try again or order online at {SITE_URL} 🙏"

        # ─── CANCEL ──────────────────────────
        if act == "CANCEL_ORDER":
            _clear_state(phone)
            return "Order cancelled. ❌\n\nType *hi* to start again anytime! 🧁"

        # ─── UNKNOWN ─────────────────────────
        reply = action.get("reply", "")
        if reply: return reply

        return (
            f"I didn't quite catch that. 🤔\n\n"
            f"Try:\n"
            f"1️⃣ Order a Cake\n"
            f"2️⃣ Check Order Status\n"
            f"3️⃣ View Menu\n\n"
            f"Or just tell me what you need!"
        )

    finally:
        db.close()
