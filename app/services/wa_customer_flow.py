"""
Customer WhatsApp Flow — Professional, Conversational
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
SITE = settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "") if settings.WHATSAPP_TRACKING_BASE_URL else "cakeoclock.co.in"


def _get_redis():
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL, db=2, decode_responses=True)
        r.ping()
        return r
    except:
        return None

def _get_state(phone): 
    r = _get_redis()
    if r:
        raw = r.get(f"wa:{phone}")
        return json.loads(raw) if raw else {"step": "IDLE"}
    return _memory.get(phone, {"step": "IDLE"})

def _set_state(phone, state):
    r = _get_redis()
    if r: r.setex(f"wa:{phone}", 3600, json.dumps(state))
    else: _memory[phone] = state

def _clear_state(phone):
    r = _get_redis()
    if r: r.delete(f"wa:{phone}")
    else: _memory.pop(phone, None)


def handle_customer_message(phone: str, message: str, user: Optional[User]) -> str:
    state = _get_state(phone)
    step = state.get("step", "IDLE")
    db = SessionLocal()

    try:
        products = [{"id": p.id, "name": p.name, "base_price": p.base_price}
                    for p in db.query(Product).filter(Product.is_available == True).all()]
        context = {"step": step, "products": products}
        action = parse_message(message, "customer", context)
        act = action.get("action", "UNKNOWN")

        # ─── CONVERSATIONAL (AI answered directly) ───
        if act == "CONVERSATIONAL":
            reply = action.get("reply", "")
            if reply: return reply

        # ─── WELCOME ─────────────────────────
        if act == "WELCOME" or (step == "IDLE" and act == "UNKNOWN"):
            _set_state(phone, {"step": "IDLE"})
            return (
                f"Welcome to Cake O' Clock.\n\n"
                f"We offer premium handcrafted cakes with 40+ flavors, delivered across Lucknow.\n\n"
                f"How can I assist you?\n\n"
                f"1. Place an order\n"
                f"2. Track an order\n"
                f"3. View our menu\n\n"
                f"You can also ask me anything about our cakes, pricing, or delivery.\n\n"
                f"{SITE}"
            )

        # ─── VIEW MENU ───────────────────────
        if act == "VIEW_MENU":
            lines = ["*Our Menu*\n"]
            for p in products:
                lines.append(f"· {p['name']} — ₹{p['base_price']:,.0f}/kg")
            lines.append(f"\nFull menu with customization options: {SITE}/menu")
            lines.append(f"\nWould you like to place an order?")
            return "\n".join(lines)

        # ─── CHECK STATUS ────────────────────
        if act == "CHECK_STATUS":
            _set_state(phone, {"step": "AWAITING_STATUS_ID"})
            return "Please share your order number."

        if act == "STATUS_ORDER_ID":
            oid = action.get("order_id")
            _set_state(phone, {"step": "IDLE"})
            if oid:
                order = db.query(Order).filter(Order.id == oid).first()
                if order:
                    from app.services.order_service import _enrich_order
                    _enrich_order(order)
                    items = ", ".join([f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}" for i in order.items]) if order.items else "Cake"
                    status_map = {"CONFIRMED": "Order confirmed", "ASSIGNED": "Assigned to baker", "IN_PRODUCTION": "Being prepared",
                        "AWAITING_APPROVAL": "Quality check", "PACKAGED": "Ready for delivery", "OUT_FOR_DELIVERY": "Out for delivery",
                        "DELIVERED": "Delivered", "CANCELLED": "Cancelled"}
                    return (
                        f"*Order #{order.id}*\n\n"
                        f"Items: {items}\n"
                        f"Amount: ₹{order.total_price:,.0f}\n"
                        f"Status: {status_map.get(order.status.value, order.status.value)}\n\n"
                        f"Track online: {SITE}/track?id={order.id}"
                    )
                return f"Order #{oid} not found. Please verify the order number."
            return "Please enter a valid order number."

        # ─── START ORDER ─────────────────────
        if act == "START_ORDER":
            lines = ["*Select a cake:*\n"]
            for i, p in enumerate(products, 1):
                lines.append(f"{i}. {p['name']} — ₹{p['base_price']:,.0f}/kg")
            lines.append(f"\nYou can type the number or the cake name.")
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
                return f"Please select a valid option (1 to {len(prods)}) or type the cake name."

            sizes = [{"id": s.id, "name": s.name, "multiplier": s.multiplier}
                     for s in db.query(SizeRule).filter(SizeRule.is_active == True).all()]
            lines = [f"*{selected['name']}* — ₹{selected['base_price']:,.0f}/kg\n\n*Select size:*\n"]
            for i, s in enumerate(sizes, 1):
                lines.append(f"{i}. {s['name']}")
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
                return f"Please select a size (1 to {len(sizes)})."

            flavors = [{"id": f.id, "name": f.name, "extra_cost": f.extra_cost}
                       for f in db.query(FlavorRule).filter(FlavorRule.is_active == True).all()][:10]
            lines = [f"Size: *{selected['name']}*\n\n*Select flavor:*\n"]
            for i, f in enumerate(flavors, 1):
                cost = f" (+₹{f['extra_cost']})" if f['extra_cost'] > 0 else ""
                lines.append(f"{i}. {f['name']}{cost}")
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
                return f"Please select a flavor (1 to {len(flavors)})."

            _set_state(phone, {**state, "step": "CAKE_MESSAGE", "flavor": selected})
            return "Would you like a message on the cake?\nType your message or reply *skip*."

        # ─── CAKE MESSAGE ────────────────────
        if step == "CAKE_MESSAGE":
            cake_msg = "" if act == "SKIP" else message.strip()
            _set_state(phone, {**state, "step": "DELIVERY_ADDRESS", "cake_message": cake_msg})
            return "Please share your delivery address with a landmark.\nFor self-pickup, reply *pickup*."

        # ─── ADDRESS ─────────────────────────
        if step == "DELIVERY_ADDRESS":
            addr = message.strip()
            is_pickup = addr.upper() in ("PICKUP", "SELF PICKUP")
            _set_state(phone, {**state, "step": "SELECT_DATE", "address": "Self Pickup" if is_pickup else addr})
            return "*Delivery date:*\n\n1. Tomorrow\n2. Day after tomorrow\n3. 3 days from now"

        # ─── DATE ────────────────────────────
        if step == "SELECT_DATE":
            val = action.get("value", 1) if act == "SELECT_OPTION" else 1
            d = datetime.now() + timedelta(days=max(1, min(val, 7)))
            _set_state(phone, {**state, "step": "SELECT_TIME", "delivery_date": d.strftime("%Y-%m-%d"), "delivery_date_display": d.strftime("%d %b %Y")})
            return "*Preferred time:*\n\n1. Morning (8 AM – 12 PM)\n2. Afternoon (12 PM – 4 PM)\n3. Evening (4 PM – 8 PM)"

        # ─── TIME → SUMMARY ──────────────────
        if step == "SELECT_TIME":
            val = action.get("value", 2) if act == "SELECT_OPTION" else 2
            hours = {1: 10, 2: 14, 3: 18}.get(val, 14)
            time_label = {1: "Morning (8 AM – 12 PM)", 2: "Afternoon (12 PM – 4 PM)", 3: "Evening (4 PM – 8 PM)"}.get(val, "Afternoon")

            product = state.get("product", {})
            size = state.get("size", {})
            flavor = state.get("flavor", {})
            addr = state.get("address", "")
            cake_msg = state.get("cake_message", "")
            ddate_display = state.get("delivery_date_display", state.get("delivery_date", ""))
            total = (product.get("base_price", 0) + flavor.get("extra_cost", 0)) * size.get("multiplier", 1)

            summary = (
                f"*Order Summary*\n\n"
                f"Cake: {product.get('name', 'Cake')}\n"
                f"Size: {size.get('name', '1kg')}\n"
                f"Flavor: {flavor.get('name', 'Classic')}\n"
            )
            if cake_msg:
                summary += f"Message: \"{cake_msg}\"\n"
            summary += (
                f"Delivery: {addr}\n"
                f"Date: {ddate_display} — {time_label}\n\n"
                f"*Total: ₹{total:,.0f}*\n\n"
                f"Reply *confirm* to place this order.\n"
                f"Reply *cancel* to start over."
            )
            _set_state(phone, {**state, "step": "CONFIRM", "time_hours": hours, "time_label": time_label, "total": total})
            return summary

        # ─── CONFIRM ─────────────────────────
        if step == "CONFIRM" and act == "CONFIRM_ORDER":
            if not user:
                _clear_state(phone)
                return "Please share your name to create an account."

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
                    items=[OrderItemCreate(product_id=product.get("id", 1), quantity=1,
                        customization={"size": size.get("name", "1kg"), "flavor": flavor.get("name", "Classic"),
                            "design": "Basic Cream Finish", "addons": [], "rush": "Standard (24hr+)"})],
                    delivery_address=addr if addr != "Self Pickup" else None,
                    delivery_time=f"{ddate}T{time_hours:02d}:00:00",
                    notes=cake_msg or None,
                )
                order = create_order(db, order_data, user)
                _clear_state(phone)

                from app.services.whatsapp_sender import notify_admin_new_order
                admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
                items_str = f"{size.get('name', '1kg')} {flavor.get('name', '')} {product.get('name', 'Cake')}"
                delivery_str = f"{state.get('delivery_date_display', ddate)} {state.get('time_label', '')}"
                for admin in admins:
                    notify_admin_new_order(admin.phone, order.id, user.name, phone, items_str, total, delivery_str)

                return (
                    f"*Order #{order.id} — Confirmed*\n\n"
                    f"Amount: ₹{total:,.0f}\n"
                    f"You will receive updates on this number.\n\n"
                    f"Track your order: {SITE}/track?id={order.id}\n\n"
                    f"Thank you for choosing Cake O' Clock."
                )
            except Exception as e:
                logger.error(f"[WA ORDER] Failed: {e}")
                _clear_state(phone)
                return f"We couldn't process your order at the moment. Please try again or order online at {SITE}"

        # ─── CANCEL ──────────────────────────
        if act == "CANCEL_ORDER":
            _clear_state(phone)
            return "Order cancelled. You can start a new order anytime."

        # ─── UNKNOWN ─────────────────────────
        reply = action.get("reply", "")
        if reply: return reply

        return (
            f"I can help you with:\n"
            f"1. Place an order\n"
            f"2. Track an order\n"
            f"3. View our menu\n\n"
            f"Or ask me anything about our cakes and services."
        )
    finally:
        db.close()
