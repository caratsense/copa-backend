"""
Customer WhatsApp Ordering Flow
================================
Redis-backed state machine for step-by-step cake ordering.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.product import Product
from app.models.pricing import SizeRule, FlavorRule, DesignRule
from app.models.delivery import DeliveryZone
from app.models.user import User, UserRole
from app.models.order import Order
from app.services.order_service import create_order, _enrich_order
from app.services.whatsapp_sender import send_text, notify_customer_order_confirmed
from app.services.gemini_parser import parse_message
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)

# Try to use Redis for state, fall back to in-memory dict
_memory_store: dict = {}

def _get_redis():
    try:
        import redis
        r = redis.Redis(host="redis", port=6379, db=2, decode_responses=True)
        r.ping()
        return r
    except:
        return None


def _get_state(phone: str) -> dict:
    r = _get_redis()
    if r:
        raw = r.get(f"wa_order:{phone}")
        return json.loads(raw) if raw else {"step": "IDLE"}
    return _memory_store.get(phone, {"step": "IDLE"})


def _set_state(phone: str, state: dict):
    r = _get_redis()
    if r:
        r.setex(f"wa_order:{phone}", 3600, json.dumps(state))  # 1hr TTL
    else:
        _memory_store[phone] = state


def _clear_state(phone: str):
    r = _get_redis()
    if r:
        r.delete(f"wa_order:{phone}")
    else:
        _memory_store.pop(phone, None)


def handle_customer_message(phone: str, message: str, user: Optional[User]) -> str:
    """Process a customer's WhatsApp message and return the reply text."""
    state = _get_state(phone)
    step = state.get("step", "IDLE")

    # Parse message with context
    db = SessionLocal()
    try:
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
                f"👋 Hi {name}! Welcome to *Cake O' Clock*! 🧁\n"
                f"Made with Love by Shriya Mahendru\n\n"
                f"What would you like to do?\n"
                f"1️⃣ Order a Cake\n"
                f"2️⃣ Check Order Status\n"
                f"3️⃣ View Menu"
            )

        # ─── VIEW MENU ───────────────────────
        if act == "VIEW_MENU":
            lines = ["🎂 *Our Menu:*\n"]
            for p in products:
                lines.append(f"• *{p['name']}* — ₹{p['base_price']:,.0f}/kg")
            lines.append(f"\n40+ flavors available!")
            lines.append(f"Reply *1* to order or visit: {settings.WHATSAPP_TRACKING_BASE_URL.replace('/track', '/menu')}")
            return "\n".join(lines)

        # ─── CHECK STATUS ────────────────────
        if act == "CHECK_STATUS":
            _set_state(phone, {"step": "AWAITING_STATUS_ID"})
            return "Enter your order number:"

        if act == "STATUS_ORDER_ID":
            oid = action.get("order_id")
            _set_state(phone, {"step": "IDLE"})
            if oid:
                order = db.query(Order).filter(Order.id == oid).first()
                if order:
                    _enrich_order(order)
                    items_str = ", ".join([f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}" for i in order.items]) if order.items else "Cake"
                    status_emoji = {"CONFIRMED": "📋", "ASSIGNED": "👨‍🍳", "IN_PRODUCTION": "🧁", "AWAITING_APPROVAL": "✅", "PACKAGED": "📦", "OUT_FOR_DELIVERY": "🛵", "DELIVERED": "✅", "CANCELLED": "❌"}.get(order.status.value, "📋")
                    return (
                        f"📦 *Order #{order.id} Status*\n\n"
                        f"🎂 {items_str}\n"
                        f"💰 ₹{order.total_price:,.0f}\n"
                        f"Status: {status_emoji} {order.status.value.replace('_', ' ')}\n\n"
                        f"Track live: {settings.WHATSAPP_TRACKING_BASE_URL}?id={order.id}"
                    )
                return f"Order #{oid} not found. Please check the number."
            return "Please enter a valid order number."

        # ─── START ORDER ─────────────────────
        if act == "START_ORDER":
            lines = ["🎂 *Let's build your cake!*\n\nWhich cake type?"]
            for i, p in enumerate(products, 1):
                lines.append(f"{i}️⃣ {p['name']} — ₹{p['base_price']:,.0f}/kg")
            _set_state(phone, {"step": "SELECT_PRODUCT", "products": products})
            return "\n".join(lines)

        # ─── SELECT PRODUCT ──────────────────
        if step == "SELECT_PRODUCT" and (act == "SELECT_OPTION" or act == "SELECT_PRODUCT_BY_NAME"):
            prods = state.get("products", products)
            selected = None

            if act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(prods):
                    selected = prods[idx]
            elif act == "SELECT_PRODUCT_BY_NAME":
                name = action.get("name", "").lower()
                for p in prods:
                    if name in p["name"].lower():
                        selected = p
                        break

            if not selected:
                return f"Please select a number between 1 and {len(prods)}."

            sizes = [{"id": s.id, "name": s.name, "multiplier": s.multiplier}
                     for s in db.query(SizeRule).filter(SizeRule.is_active == True).all()]
            lines = ["📏 *What size?*\n"]
            for i, s in enumerate(sizes, 1):
                lines.append(f"{i}️⃣ {s['name']}")
            _set_state(phone, {**state, "step": "SELECT_SIZE", "product": selected, "sizes": sizes})
            return "\n".join(lines)

        # ─── SELECT SIZE ─────────────────────
        if step == "SELECT_SIZE" and (act == "SELECT_OPTION" or act == "SELECT_SIZE_BY_NAME"):
            sizes = state.get("sizes", [])
            selected = None

            if act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(sizes):
                    selected = sizes[idx]
            elif act == "SELECT_SIZE_BY_NAME":
                name = action.get("name", "").lower()
                for s in sizes:
                    if name in s["name"].lower():
                        selected = s
                        break

            if not selected:
                return f"Please select a number between 1 and {len(sizes)}."

            product = state.get("product", {})
            pid = product.get("id")
            # Get flavors for this product's category
            product_obj = db.query(Product).filter(Product.id == pid).first()
            cat = product_obj.category if product_obj else ""
            flavors = [{"id": f.id, "name": f.name, "extra_cost": f.extra_cost}
                       for f in db.query(FlavorRule).filter(FlavorRule.is_active == True).all()
                       if cat.split("-")[0] in f.name.lower() or "white" in f.name.lower() or f.extra_cost > 0]
            if not flavors:
                flavors = [{"id": f.id, "name": f.name, "extra_cost": f.extra_cost}
                           for f in db.query(FlavorRule).filter(FlavorRule.is_active == True).all()]

            lines = ["🍓 *Pick a flavor:*\n"]
            for i, f in enumerate(flavors[:10], 1):
                cost = f" +₹{f['extra_cost']}" if f['extra_cost'] > 0 else ""
                lines.append(f"{i}️⃣ {f['name']}{cost}")

            _set_state(phone, {**state, "step": "SELECT_FLAVOR", "size": selected, "flavors": flavors[:10]})
            return "\n".join(lines)

        # ─── SELECT FLAVOR ───────────────────
        if step == "SELECT_FLAVOR" and (act == "SELECT_OPTION" or act == "SELECT_FLAVOR_BY_NAME"):
            flavors = state.get("flavors", [])
            selected = None

            if act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(flavors):
                    selected = flavors[idx]
            elif act == "SELECT_FLAVOR_BY_NAME":
                name = action.get("name", "").lower()
                for f in flavors:
                    if name in f["name"].lower():
                        selected = f
                        break

            if not selected:
                return f"Please select a number between 1 and {len(flavors)}."

            _set_state(phone, {**state, "step": "CAKE_MESSAGE", "flavor": selected})
            return "💬 *Any message on the cake?*\n(Type your message or reply *SKIP*)"

        # ─── CAKE MESSAGE ────────────────────
        if step == "CAKE_MESSAGE":
            cake_msg = "" if act == "SKIP" else message.strip()
            _set_state(phone, {**state, "step": "DELIVERY_ADDRESS", "cake_message": cake_msg})
            return "📍 *Delivery address?*\n(Send your full address with landmark, or reply *PICKUP* for self-pickup)"

        # ─── DELIVERY ADDRESS ────────────────
        if step == "DELIVERY_ADDRESS":
            address = message.strip()
            is_pickup = address.upper() in ("PICKUP", "SELF PICKUP", "SELF-PICKUP")
            _set_state(phone, {**state, "step": "SELECT_DATE", "address": "Self Pickup" if is_pickup else address})
            return (
                "📅 *When do you want delivery?*\n\n"
                "1️⃣ Tomorrow\n"
                "2️⃣ Day after tomorrow\n"
                "3️⃣ 3 days from now"
            )

        # ─── SELECT DATE ─────────────────────
        if step == "SELECT_DATE" and (act == "SELECT_OPTION" or act == "SET_DATE"):
            val = action.get("value", 1)
            days = max(1, min(val, 7))
            delivery_date = datetime.now() + timedelta(days=days)
            _set_state(phone, {**state, "step": "SELECT_TIME", "delivery_date": delivery_date.strftime("%Y-%m-%d")})
            return (
                "⏰ *Preferred time?*\n\n"
                "1️⃣ Morning (8AM - 12PM)\n"
                "2️⃣ Afternoon (12PM - 4PM)\n"
                "3️⃣ Evening (4PM - 8PM)"
            )

        # ─── SELECT TIME ─────────────────────
        if step == "SELECT_TIME" and (act == "SELECT_OPTION" or act == "SET_TIME"):
            val = action.get("value", 2)
            hours = {1: 10, 2: 14, 3: 18}.get(val, 14)
            time_label = {1: "Morning (8AM-12PM)", 2: "Afternoon (12PM-4PM)", 3: "Evening (4PM-8PM)"}.get(val, "Afternoon")

            product = state.get("product", {})
            size = state.get("size", {})
            flavor = state.get("flavor", {})
            address = state.get("address", "")
            cake_msg = state.get("cake_message", "")
            delivery_date = state.get("delivery_date", "")

            # Calculate price
            base = product.get("base_price", 0)
            multiplier = size.get("multiplier", 1)
            flavor_cost = flavor.get("extra_cost", 0)
            total = (base + flavor_cost) * multiplier

            summary = (
                f"📋 *Order Summary*\n\n"
                f"🎂 {product.get('name', 'Cake')}\n"
                f"📏 {size.get('name', '1kg')}\n"
                f"🍓 {flavor.get('name', 'Classic')}\n"
            )
            if cake_msg:
                summary += f'💬 "{cake_msg}"\n'
            summary += (
                f"📍 {address}\n"
                f"📅 {delivery_date} — {time_label}\n\n"
                f"💰 *Total: ₹{total:,.0f}*\n\n"
                f"Reply *CONFIRM* to place order\n"
                f"Reply *CANCEL* to start over"
            )

            _set_state(phone, {
                **state, "step": "CONFIRM",
                "time_hours": hours, "time_label": time_label, "total": total,
            })
            return summary

        # ─── CONFIRM ORDER ───────────────────
        if step == "CONFIRM" and act == "CONFIRM_ORDER":
            if not user:
                _clear_state(phone)
                return "❌ Please register on our website first or send your name to create an account."

            product = state.get("product", {})
            size = state.get("size", {})
            flavor = state.get("flavor", {})
            total = state.get("total", 0)
            address = state.get("address", "Self Pickup")
            cake_msg = state.get("cake_message", "")
            delivery_date = state.get("delivery_date", "")
            time_hours = state.get("time_hours", 14)

            delivery_time_str = f"{delivery_date}T{time_hours:02d}:00:00"

            try:
                from app.schemas import OrderCreate, OrderItemCreate
                order_data = OrderCreate(
                    items=[OrderItemCreate(
                        product_id=product.get("id", 1),
                        quantity=1,
                        customization={
                            "size": size.get("name", "1kg"),
                            "flavor": flavor.get("name", "Classic"),
                            "design": "Basic Cream Finish",
                            "addons": [],
                            "rush": "Standard (24hr+)",
                        },
                    )],
                    delivery_address=address if address != "Self Pickup" else None,
                    delivery_time=delivery_time_str,
                    notes=cake_msg or None,
                )
                order = create_order(db, order_data, user)
                _clear_state(phone)

                # Notify admin
                from app.services.whatsapp_sender import notify_admin_new_order
                admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
                items_str = f"{size.get('name', '1kg')} {flavor.get('name', '')} {product.get('name', 'Cake')}"
                delivery_str = f"{delivery_date} {state.get('time_label', '')}"
                for admin in admins:
                    notify_admin_new_order(admin.phone, order.id, user.name, phone, items_str, total, delivery_str)

                return (
                    f"✅ *Order #{order.id} Placed!*\n\n"
                    f"Thank you! Your cake will be ready on time. 🧁\n\n"
                    f"💰 Total: ₹{total:,.0f}\n"
                    f"📱 Track: {settings.WHATSAPP_TRACKING_BASE_URL}?id={order.id}\n\n"
                    f"For queries, just message us here!"
                )
            except Exception as e:
                logger.error(f"[WA ORDER] Failed: {e}")
                _clear_state(phone)
                return f"❌ Sorry, something went wrong placing your order. Please try again or visit our website."

        # ─── CANCEL ──────────────────────────
        if act == "CANCEL_ORDER":
            _clear_state(phone)
            return "❌ Order cancelled.\n\nReply *HI* to start again."

        # ─── UNKNOWN ─────────────────────────
        reply = action.get("reply", "")
        if reply:
            return reply

        return (
            "Sorry, I didn't understand that.\n\n"
            "1️⃣ Order a Cake\n"
            "2️⃣ Check Order Status\n"
            "3️⃣ View Menu"
        )

    finally:
        db.close()
