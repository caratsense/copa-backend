"""
Customer WhatsApp Flow — Professional, Conversational
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from app.db import SessionLocal
from app.models.product import Product
from app.models.pricing import SizeRule, FlavorRule
from app.models.delivery import DeliveryZone
from app.models.user import User
from app.models.order import Order
from app.services.gemini_parser import parse_message
from app.schemas import ItemCustomization
from app.services.pricing_engine import calculate_item_price
from app.config import get_settings
settings = get_settings()
logger = logging.getLogger(__name__)
_memory: dict = {}
SITE = settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "") if settings.WHATSAPP_TRACKING_BASE_URL else "cakeoclock.co.in"


_client = None  # cached connection; tests replace this with a fake


def _get_redis():
    """
    The conversation-state store (db 2, keyed by phone).

    Cached in a module global so tests can replace it. Building a fresh client
    per call meant a live Redis carried `wa:{phone}` state across tests and
    across runs for the full hour of its TTL -- the same way the webhook
    dedupe store used to make this suite pass only when no Redis happened to
    be running.
    """
    global _client
    if _client is not None:
        return _client
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL, db=2, decode_responses=True)
        r.ping()
        _client = r
        return _client
    except Exception:
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


DATE_PROMPT = "*Delivery date:*\n\n1. Tomorrow\n2. Day after tomorrow\n3. 3 days from now"


def _is_per_kg(product: Optional[dict]) -> bool:
    """
    Whether this product is sold by weight, so a size applies to it.

    Defaults to "kg" for a product dict that carries no pricing_unit at all:
    conversations already parked in Redis when this shipped hold the older
    shape, and "kg" is both the column default and what those conversations
    were already being quoted.
    """
    return (product or {}).get("pricing_unit", "kg") == "kg"


def _options(product: Optional[dict]) -> list[dict]:
    """
    The sizes this particular product is sold in, or [] if it uses the global
    ones. Absent from conversations parked in Redis before this shipped, which
    read as "no options" - the behaviour those conversations already had.
    """
    return list((product or {}).get("options") or [])


def _needs_size(product: Optional[dict]) -> bool:
    """Whether to ask for a size at all: a per-kg cake, or anything with its
    own options - including a fixed-price tea cake sold as a loaf or a round."""
    return bool(_options(product)) or _is_per_kg(product)


def _price_label(product: dict) -> str:
    """
    The headline price.

    "Rs 2,000/kg" for a cake sold by weight, "Rs 400" for a fixed-price item,
    and for a product whose options carry their own prices, those prices - a
    1.3kg chiffon cake at Rs 2,400 is not Rs 2,400/kg, and quoting it that way
    would be a lie whichever size the customer then picked. The numbers here
    are read from the options, never worked out from a multiplier: what an
    option costs is the pricing engine's business, not this flow's.
    """
    priced = [o["price"] for o in _options(product) if o.get("price") is not None]
    if len(priced) == 1:
        return f"₹{priced[0]:,.0f}"
    if priced:
        return f"from ₹{min(priced):,.0f}"
    price = f"₹{product.get('base_price', 0):,.0f}"
    return f"{price}/kg" if _is_per_kg(product) else price


def _ask_for_size(phone: str, state: dict, product: dict) -> str:
    """
    Offer the product's own sizes, and only those.

    The global size list is not shown for these products, so a weight it does
    not sell in is not offered and cannot be picked. An explicitly priced
    option shows its price, because a Rs 800 loaf and a Rs 1,700 round are not
    the same choice; a per-kg option shows only its label, since its price
    comes from the kg rate in the header.
    """
    options = _options(product)
    lines = [f"*{product['name']}* — {_price_label(product)}\n\n*Select size:*\n"]
    for i, option in enumerate(options, 1):
        price = option.get("price")
        suffix = f" — ₹{price:,.0f}" if price is not None else ""
        lines.append(f"{i}. {option['label']}{suffix}")
    # Stored under the same key the size step already reads, so an option and a
    # global SizeRule are picked the same way.
    _set_state(phone, {
        **state, "step": "SELECT_SIZE", "product": product,
        "sizes": [{"name": o["label"], "price": o.get("price")} for o in options],
    })
    return "\n".join(lines)


def _ask_for_flavor(db, phone: str, state: dict, header: str) -> str:
    """
    Move the conversation to the flavour step.

    Shared because two steps now arrive here: a per-kg cake after its size, and
    a fixed-price product straight from the product list, which has no size to
    choose.
    """
    flavors = [{"id": f.id, "name": f.name, "extra_cost": f.extra_cost}
               for f in db.query(FlavorRule).filter(FlavorRule.is_active == True).all()][:10]
    lines = [f"{header}\n\n*Select flavor:*\n"]
    for i, f in enumerate(flavors, 1):
        cost = f" (+₹{f['extra_cost']})" if f['extra_cost'] > 0 else ""
        lines.append(f"{i}. {f['name']}{cost}")
    _set_state(phone, {**state, "step": "SELECT_FLAVOR", "flavors": flavors})
    return "\n".join(lines)


def _quote(db, product: dict, size: Optional[dict], flavor: dict, quantity: int = 1):
    """
    Price the conversation through the pricing engine, or None if it cannot.

    The summary used to work the total out for itself as
    (base_price + flavour) * size multiplier. That is not what the customer is
    charged: the engine multiplies only the base by the size and adds the
    flavour afterwards, so a 2kg Rs 1,000 cake with a Rs 2,000 flavour was
    quoted Rs 6,000 in chat and billed Rs 4,000. Asking the engine is the only
    way the quote and the charge cannot disagree - and it is also what makes a
    fixed-price product skip the size multiplier, since the engine already
    knows to.

    Delivery is deliberately not included, which matches what this summary has
    always shown; the charge for it is quoted from the real order at CONFIRM.
    """
    row = db.query(Product).filter(Product.id == product.get("id")).first()
    if row is None:
        return None
    return calculate_item_price(
        db=db,
        product=row,
        customization=ItemCustomization(
            size=(size or {}).get("name") or "",
            flavor=(flavor or {}).get("name") or "",
        ),
        quantity=quantity,
    )


def handle_customer_message(phone: str, message: str, user: Optional[User]) -> str:
    state = _get_state(phone)
    step = state.get("step", "IDLE")
    db = SessionLocal()

    try:
        products = [{"id": p.id, "name": p.name, "base_price": p.base_price,
                     "pricing_unit": p.pricing_unit,
                     "options": [{"label": o.label, "price": o.price}
                                 for o in p.options if o.is_active]}
                    for p in db.query(Product).filter(Product.is_available == True).all()]
        context = {"step": step, "products": products}
        action = parse_message(message, "customer", context)
        act = action.get("action", "UNKNOWN")

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

        # ─── CONVERSATIONAL (AI answered a question) ───
        if act == "CONVERSATIONAL":
            reply = action.get("reply", "")
            if reply: return reply

        # ─── VIEW MENU ───────────────────────
        if act == "VIEW_MENU":
            lines = ["*Our Menu*\n"]
            for p in products:
                lines.append(f"· {p['name']} — {_price_label(p)}")
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
                # Scoped to the sender. This used to look up by id alone, so any
                # WhatsApp number could read any customer's items, amount and
                # status just by guessing an order number.
                order = (
                    db.query(Order)
                    .filter(Order.id == oid, Order.user_id == user.id)
                    .first()
                    if user else None
                )
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
                # Deliberately identical whether the order is missing or
                # simply someone else's — otherwise this confirms which order
                # numbers exist.
                return f"We couldn't find order #{oid} on your account. Please check the number."
            return "Please enter a valid order number."

        # ─── START ORDER ─────────────────────
        if act == "START_ORDER":
            lines = ["*Select a cake:*\n"]
            for i, p in enumerate(products, 1):
                lines.append(f"{i}. {p['name']} — {_price_label(p)}")
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

            # This product sells in its own sizes. Offer those and nothing
            # else - the global list is not consulted for it, which is how a
            # cake that is not sold under 1kg stops offering 500g.
            if _options(selected):
                return _ask_for_size(phone, state, selected)

            # A fixed-price product with no options has no size to ask about -
            # base_price is its price - so it goes straight to the flavour
            # step. Asking a customer whether they want 500g or 5kg of a pack
            # of six buns is a question with no answer.
            if not _is_per_kg(selected):
                return _ask_for_flavor(
                    db, phone, {**state, "product": selected, "size": None},
                    header=f"*{selected['name']}* — {_price_label(selected)}",
                )

            sizes = [{"id": s.id, "name": s.name, "multiplier": s.multiplier}
                     for s in db.query(SizeRule).filter(SizeRule.is_active == True).all()]
            lines = [f"*{selected['name']}* — {_price_label(selected)}\n\n*Select size:*\n"]
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

            return _ask_for_flavor(db, phone, {**state, "size": selected},
                                   header=f"Size: *{selected['name']}*")

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
            if is_pickup:
                _set_state(phone, {**state, "step": "SELECT_DATE", "address": "Self Pickup"})
                return DATE_PROMPT

            # Which area? The conversation never asked, so every WhatsApp
            # order was created with no zone at all - and a blank zone means
            # "pickup" to the pricing engine, so delivery was free on all of
            # them. Passing the value through was not enough on its own;
            # nothing ever set it.
            zones = (
                db.query(DeliveryZone)
                .filter(DeliveryZone.is_active == True)
                .order_by(DeliveryZone.area_name)
                .all()
            )
            if not zones:
                # Nothing configured to deliver to. Carry on rather than
                # trapping the customer in a step they cannot answer.
                _set_state(phone, {**state, "step": "SELECT_DATE", "address": addr})
                return DATE_PROMPT

            _set_state(phone, {
                **state, "step": "SELECT_AREA", "address": addr,
                "zone_options": [z.area_name for z in zones],
            })
            listing = "\n".join(
                f"{i}. {z.area_name} - Rs {z.charge:,.0f}"
                for i, z in enumerate(zones, 1)
            )
            return "*Which area are we delivering to?*\n\n" + listing + "\n\nReply with the number."

        # ─── AREA ────────────────────────
        if step == "SELECT_AREA":
            options = state.get("zone_options") or []
            typed = message.strip()

            # Read the number straight from the message rather than relying on
            # the shared parser: this step decides what the customer is charged
            # for delivery, so it must not depend on a language-model fallback
            # to work out that "2" means the second option.
            choice = int(typed) if typed.isdigit() else None
            if choice is None and act == "SELECT_OPTION":
                choice = action.get("value")
            if choice is None:
                # Accept the area name as well as its number.
                lowered = typed.lower()
                for i, name in enumerate(options, 1):
                    if name.strip().lower() == lowered:
                        choice = i
                        break
            try:
                choice = int(choice)
            except (TypeError, ValueError):
                choice = 0
            if not 1 <= choice <= len(options):
                listing = "\n".join(f"{i}. {n}" for i, n in enumerate(options, 1))
                return "Please pick your area by number:\n\n" + listing

            _set_state(phone, {**state, "step": "SELECT_DATE",
                               "delivery_zone": options[choice - 1]})
            return DATE_PROMPT

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
            size = state.get("size") or {}
            flavor = state.get("flavor", {})
            addr = state.get("address", "")
            cake_msg = state.get("cake_message", "")
            ddate_display = state.get("delivery_date_display", state.get("delivery_date", ""))
            quantity = state.get("quantity", 1)

            breakdown = _quote(db, product, size, flavor, quantity)
            if breakdown is None:
                # The product went away mid-conversation. Saying so beats
                # quoting a number for something that can no longer be ordered.
                _clear_state(phone)
                return ("That item is no longer available. "
                        "Reply *1* to start a new order.")
            total = breakdown.line_total

            summary = (
                f"*Order Summary*\n\n"
                f"Cake: {product.get('name', 'Cake')}\n"
            )
            # Only a per-kg cake has one. Printing "Size: 1kg" against a
            # brownie invented a weight the customer never chose and we do not
            # sell it by. Gated on the product as well as on the state, so a
            # stale size left over from an earlier choice cannot put a weight
            # back on a fixed-price line.
            if size.get("name") and _needs_size(product):
                summary += f"Size: {size['name']}\n"
            summary += f"Flavor: {flavor.get('name', 'Classic')}\n"
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
            size = state.get("size") or {}
            flavor = state.get("flavor", {})
            quantity = state.get("quantity", 1)
            addr = state.get("address", "Self Pickup")
            cake_msg = state.get("cake_message", "")
            ddate = state.get("delivery_date", "")
            time_hours = state.get("time_hours", 14)

            try:
                from app.schemas import OrderCreate, OrderItemCreate
                from app.services.order_service import create_order
                order_data = OrderCreate(
                    user_id=user.id,
                    # Without this the pricing engine sees no zone and charges
                    # nothing for delivery, so every WhatsApp order shipped free.
                    delivery_zone=state.get("delivery_zone"),
                    items=[OrderItemCreate(product_id=product.get("id", 1), quantity=quantity,
                        # Blank size for a fixed-price product. The default
                        # used to be "1kg", which the engine reads as a real
                        # SizeRule - harmless at a 1.0 multiplier, but it wrote
                        # a weight onto the order line for something never sold
                        # by weight, and every staff screen then showed it.
                        customization={"size": size.get("name") or "", "flavor": flavor.get("name", "Classic"),
                            # The WhatsApp conversation never asks about design
                            # or rush, so it must not assert values for them.
                            # Both were hardcoded to zero-cost rule names, which
                            # priced the same as "not selected" but broke
                            # outright on any deployment missing those rules.
                            "design": "", "addons": [], "rush": ""})],
                    delivery_address=addr if addr != "Self Pickup" else None,
                    delivery_time=f"{ddate}T{time_hours:02d}:00:00",
                    notes=cake_msg or None,
                )
                order = create_order(db, order_data)
                _clear_state(phone)

                # Quote what the pricing engine actually charged, not the running
                # total from the chat state. Those could differ — the state total
                # never included delivery, so the customer was told one number and
                # the order carried another.
                amount = float(order.total_price or 0)

                # The order is NOT paid. It used to say "Confirmed" with an
                # amount and no payment step at all, which read as settled while
                # nothing had been collected and no payment link existed. Hand
                # off to the existing web checkout instead of pretending
                # WhatsApp can take money.
                return (
                    f"*Order #{order.id} created*\n\n"
                    f"Amount: Rs {amount:,.2f}\n"
                    f"Payment: pending\n\n"
                    f"Complete your payment to confirm the order:\n"
                    f"{SITE}/orders?order_id={order.id}\n\n"
                    f"We'll start baking once payment is received. "
                    f"You'll get updates on this number."
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
