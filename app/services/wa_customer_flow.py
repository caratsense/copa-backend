"""
Customer WhatsApp Flow — Professional, Conversational
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
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
from app.services.store_hours import delivery_date_options, slots_for_date
from app.services import wa_escalation
from app.config import get_settings
settings = get_settings()
logger = logging.getLogger(__name__)
_memory: dict = {}
# Who has already been given the owner's contact, when there is no Redis.
# Timestamps rather than flags, so the window still expires in a bare process.
_escalated_memory: dict = {}
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

# Bumped whenever the shape of a conversation changes. The key carries it, so
# a deploy leaves old conversations unreachable rather than resuming them
# against code that no longer understands them. The October category-first
# flow shipped without this, and anyone mid-order kept being served the old
# sixty-two-item list out of Redis for the next hour.
_STATE_VERSION = "v2"


def _state_key(phone: str) -> str:
    return f"wa:{_STATE_VERSION}:{phone}"


def _get_state(phone):
    r = _get_redis()
    if r:
        raw = r.get(_state_key(phone))
        return json.loads(raw) if raw else {"step": "IDLE"}
    return _memory.get(phone, {"step": "IDLE"})

def _set_state(phone, state):
    # Stamped on every write so the next message can tell a conversation that
    # is still going from one abandoned twenty minutes ago. See _is_stale.
    state = {**state, "touched_at": time.time()}
    r = _get_redis()
    if r: r.setex(_state_key(phone), 3600, json.dumps(state))
    else: _memory[phone] = state

def _clear_state(phone):
    r = _get_redis()
    if r: r.delete(_state_key(phone))
    else: _memory.pop(phone, None)


def _date_prompt(db) -> tuple[str, list[dict]]:
    """
    The date question, built from the days the kitchen can actually meet.

    This was a constant reading "1. Tomorrow / 2. Day after tomorrow / 3. 3
    days from now". It never named a date, so there was nothing to check it
    against, and it ruled out same-day entirely while the website was selling
    it - five hours' notice is the whole rule, so a cake ordered at eleven in
    the morning can go out the same evening.

    Returns the text and the options behind it, so the reply can be resolved
    against the same list the customer was shown rather than re-derived.
    """
    options = delivery_date_options(db)
    if not options:
        return ("", [])
    lines = ["*Delivery date:*\n"]
    for i, o in enumerate(options, 1):
        lines.append(f"{i}. {o['label']}")
    return ("\n".join(lines), options)


def _slot_prompt(slots: list[dict]) -> str:
    """The time question for one date, listing only what is still bookable."""
    lines = ["*Preferred time:*\n"]
    for i, s in enumerate(slots, 1):
        lines.append(f"{i}. {s['label']}")
    return "\n".join(lines)


# Said when every slot for the next week is gone, which in practice means the
# shop is closed and it is late. Better than an empty numbered list.
NO_DATES = ("We're not taking orders for collection or delivery just now. "
            "Please try again tomorrow, or order online at ")


def _go_to_dates(db, phone: str, state: dict) -> str:
    """
    Ask for the date, and remember what was offered.

    Three steps arrive here - pickup, delivery with no zones configured, and
    delivery once a zone is chosen - so the list is built once and stored on
    the conversation. The reply is then resolved against what the customer was
    actually shown, which matters because the list shortens as the day goes on:
    deriving the date again from the number would land on a different day for
    anyone who answered slowly.
    """
    text, options = _date_prompt(db)
    if not options:
        _clear_state(phone)
        return NO_DATES + SITE
    _set_state(phone, {**state, "step": "SELECT_DATE",
                       "date_options": [{"date": o["date"].isoformat(),
                                         "label": o["label"]} for o in options]})
    return text


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

    Both routes pass through the guard below, which is why it lives here
    rather than at either call site.
    """
    # A product the kitchen does not make to order has no flavour to pick.
    # The bot offered all ten anyway — to a pack of buns, to a brownie, and to
    # the rupee test cake, which is where this was finally seen. Worse than
    # noise: several of those flavours carry a surcharge, so it invited the
    # customer to add ₹250 to something nobody would flavour, and the kitchen
    # would receive an instruction it cannot honour.
    #
    # Only the flavour is skipped. This jumped to the address as well, which
    # took the message step with it on the way past - but a message is piped
    # onto the cake after it is baked, so it applies to a fixed-price cake
    # exactly as much as to a made-to-order one, and the website asks every
    # customer for one. Going to CAKE_MESSAGE keeps the two front doors asking
    # the same questions.
    product = state.get("product") or {}
    if not product.get("is_customizable", True):
        _set_state(phone, {**state, "step": "CAKE_MESSAGE", "flavor": {}})
        return (f"{header}\n\n"
                "Would you like a message on the cake?\n"
                "Type your message or reply *skip*.")

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


def _catalogue(db) -> list[dict]:
    """
    Everything a customer can actually order, in menu order.

    Two kinds of row are deliberately left out, because neither is a thing
    anyone can buy:

      pricing rows  — "Vanilla Base Cake", "Chocolate Premium Cake" and the
                      other two. They are the per-kg rates the Build a Cake
                      page quotes from, hidden from the website since October,
                      and the bot was still offering all four at the top of
                      its order list. A customer who picked one was buying a
                      price band.
      placeholders  — a product the kitchen has added but not priced. The ₹1
                      it sits at is an internal stand-in, and quoting it is
                      worse than not offering it.

    The section each product belongs to comes with it, because the order flow
    asks which part of the menu first; a flat list of fifty-eight cakes is not
    a question anyone can answer on a phone.
    """
    rows = db.query(Product).filter(Product.is_available == True).all()
    out = []
    for p in rows:
        if p.is_pricing_row or p.is_placeholder:
            continue
        section = p.section
        # A category hidden on the website is hidden here too. When Chocolate
        # Celebration Cakes was switched off in the admin, the site stopped
        # showing its six cakes and the bot carried on selling them — the two
        # disagreed about what was on the menu, which is exactly what "the bot
        # is not synchronised with the site" means.
        if section is not None and not section.is_active:
            continue
        out.append({
            "id": p.id, "name": p.name, "base_price": p.base_price,
            "pricing_unit": p.pricing_unit,
            # Carried so the flow can stop offering a flavour list for
            # something the kitchen does not flavour to order.
            "is_customizable": bool(p.is_customizable),
            "options": [{"label": o.label, "price": o.price}
                        for o in p.options if o.is_active],
            "section": section.name if section else "More",
            "section_sort": section.sort_order if section else 999,
        })
    out.sort(key=lambda p: (p["section_sort"], p["section"], p["name"]))
    return out


def _by_section(products: list[dict]) -> list[tuple[str, list[dict]]]:
    """The catalogue grouped into the categories the menu already uses."""
    grouped: dict[str, list[dict]] = {}
    for p in products:
        grouped.setdefault(p["section"], []).append(p)
    return list(grouped.items())


def _category_prompt(products: list[dict]) -> tuple[str, list[str]]:
    """
    The question that starts an order, and the categories it offers.

    WhatsApp got the whole catalogue as one numbered list — sixty-two lines,
    which is four screens of scrolling before the first question. The menu is
    category-first on the website for the same reason it should be here: a
    dozen choices is a question, sixty is a document.
    """
    groups = _by_section(products)
    lines = ["*What are you looking for?*\n"]
    for i, (name, items) in enumerate(groups, 1):
        lines.append(f"{i}. {name} ({len(items)})")
    lines.append("\nReply with a number — or just type the cake you want.")
    return "\n".join(lines), [name for name, _ in groups]


def _product_prompt(phone: str, state: dict, category: str,
                    items: list[dict]) -> str:
    """List one category's products and wait for a choice."""
    lines = [f"*{category}*\n"]
    for i, p in enumerate(items, 1):
        lines.append(f"{i}. {p['name']} — {_price_label(p)}")
    lines.append("\nReply with the number or the name. *0* goes back.")
    _set_state(phone, {**state, "step": "SELECT_PRODUCT",
                       "products": items, "category": category})
    return "\n".join(lines)


def _match_product(products: list[dict], text: str) -> Optional[dict]:
    """
    The product this text names, or None.

    Exact name first, then a whole-name containment, so "biscoff cookie" does
    not land on "Biscoff Cheesecake" just because it was earlier in the list.
    """
    wanted = (text or "").strip().lower()
    if len(wanted) < 3:
        return None
    for p in products:
        if p["name"].lower() == wanted:
            return p
    for p in products:
        if wanted in p["name"].lower():
            return p
    return None


def _choose_product(db, phone: str, state: dict, selected: dict) -> str:
    """
    Take a chosen product and ask the next question about it.

    Reached from the product list and from someone naming a cake at the
    category step, which is why it is a function rather than the tail of one
    branch: the two routes have to put the conversation in the same place.
    """
    # This product sells in its own sizes. Offer those and nothing else - the
    # global list is not consulted for it, which is how a cake that is not
    # sold under 1kg stops offering 500g.
    if _options(selected):
        return _ask_for_size(phone, state, selected)

    # A fixed-price product with no options has no size to ask about -
    # base_price is its price - so it goes straight to the flavour step.
    # Asking a customer whether they want 500g or 5kg of a pack of six buns is
    # a question with no answer.
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


# How long a half-finished order stays resumable. Long enough to find your
# address or ask someone what to write on the cake; short enough that a
# question hours later is not read as the next answer in it.
STALE_AFTER_SECONDS = 20 * 60


def _is_stale(state: dict) -> bool:
    """Whether this conversation has been sitting unanswered too long."""
    touched = state.get("touched_at")
    if not touched:
        # Written before this field existed. Treat as current: one resumed
        # order is a smaller harm than dropping a live one.
        return False
    return (time.time() - touched) > STALE_AFTER_SECONDS


# What the bot is waiting for at each step, for when something interrupts and
# the customer needs putting back where they were.
_STEP_QUESTION = {
    "SELECT_CATEGORY": "which part of the menu you are after",
    "SELECT_PRODUCT": "which cake you would like",
    "SELECT_SIZE": "the size",
    "SELECT_FLAVOR": "the flavour",
    "CAKE_MESSAGE": "the message for the cake (or *skip*)",
    "DELIVERY_ADDRESS": "your delivery address",
    "SELECT_AREA": "your area, by number",
    "SELECT_DATE": "the delivery date, by number",
    "SELECT_TIME": "the delivery time, by number",
    "CONFIRM": "*confirm* to place the order",
    "AWAITING_STATUS_ID": "your order number",
}


def _answer_without_losing_the_order(db, phone: str, state: dict, step: str,
                                     reason: str, user: Optional[User]) -> str:
    """
    Answer something that needs a person, mid-order, without eating the answer
    the step was waiting for and without throwing the order away.
    """
    if reason.startswith("a cake over"):
        head = (f"A cake that size is quoted by hand rather than priced online, "
                f"so I'll pass this on and someone will come back to you.")
    elif reason == "a problem with an order":
        head = "I'm sorry — I'll pass this to someone who can sort it out."
    else:
        head = "Of course — I'll pass this on and someone will get back to you."

    # Staff are the people this would be passed to; see handle_customer_message.
    from app.models.user import UserRole
    is_staff = user is not None and user.role in (
        UserRole.ADMIN, UserRole.BAKER, UserRole.RIDER)
    if not is_staff:
        line = wa_escalation.contact_line(db, reason)
        if line and not _escalation_already_sent(phone):
            _mark_escalation_sent(phone)
            head += line

    waiting_for = _STEP_QUESTION.get(step)
    if waiting_for:
        head += (f"\n\nYour order is still here — I was waiting for "
                 f"{waiting_for}. Reply *cancel* to start again.")
    else:
        # Nothing in progress, so there is nothing to put them back into.
        # Somewhere to go beats a dead end while they wait for a call.
        head += f"\n\nIn the meantime, everything we bake is at {SITE}/menu."
    return head


def _escalation_already_sent(phone: str) -> bool:
    """Whether this person has already been given the contact recently."""
    r = _get_redis()
    if r:
        return bool(r.get(f"wa:esc:{phone}"))
    sent_at = _escalated_memory.get(phone)
    if sent_at is None:
        return False
    return (time.time() - sent_at) < wa_escalation.REPEAT_AFTER_SECONDS


def _mark_escalation_sent(phone: str) -> None:
    r = _get_redis()
    if r:
        r.setex(f"wa:esc:{phone}", wa_escalation.REPEAT_AFTER_SECONDS, "1")
    else:
        _escalated_memory[phone] = time.time()


def handle_customer_message(phone: str, message: str, user: Optional[User]) -> str:
    """
    The reply, plus the owner's contact when the question has earned it.

    The decision is made here, around the whole conversation rather than
    inside one of its branches, so a customer asking about a 5kg cake gets the
    same answer whether they asked in passing, mid-order, or while the bot was
    waiting for a delivery date.
    """
    reply = _reply_to_customer(phone, message, user)

    # Staff reach this path too, now that a message which is not a command is
    # answered rather than met with the command list. They must not be handed
    # the owner's number: they are the shop, and for Shriya herself it would be
    # her own number coming back.
    from app.models.user import UserRole
    if user is not None and user.role in (UserRole.ADMIN, UserRole.BAKER, UserRole.RIDER):
        return reply

    db = SessionLocal()
    try:
        return wa_escalation.augment(
            phone, message, reply, db,
            _escalation_already_sent, _mark_escalation_sent,
        )
    finally:
        db.close()


def _reply_to_customer(phone: str, message: str, user: Optional[User]) -> str:
    state = _get_state(phone)
    step = state.get("step", "IDLE")

    # An order someone walked away from is not a conversation they are still
    # having. The state used to live a full hour and simply resume, so a
    # question asked later was answered as though it were the next thing in a
    # half-finished order — the delivery-address step took "cake above 3.5kg"
    # as the address and put it on the order.
    if step != "IDLE" and _is_stale(state):
        _clear_state(phone)
        state, step = {"step": "IDLE"}, "IDLE"

    db = SessionLocal()

    try:
        # Three things are answered before anything else, because whatever
        # they land on would otherwise swallow them: a cake over the size the
        # shop can price, a complaint, and someone asking for a person.
        #
        # This used to be skipped at IDLE, on the reasoning that there was no
        # step to protect. There is something worse. "cake above 3.5kg" sent
        # to an idle conversation was read by the model as "they want to
        # order", and the bot answered a question it cannot answer with the
        # whole catalogue and its prices - useless, and the opposite of
        # passing it to a person. Checked in code, before the model, so it
        # holds whatever the model makes of the sentence.
        interrupt = wa_escalation.reason_to_interrupt(message)
        if interrupt:
            return _answer_without_losing_the_order(db, phone, state, step, interrupt, user)

        products = _catalogue(db)
        context = {"step": step, "products": products}
        action = parse_message(message, "customer", context)
        act = action.get("action", "UNKNOWN")

        # ─── CANCEL ──────────────────────────
        # Before the steps, not after them. Every step that cannot read what
        # the customer typed answers "pick one of these" — including when what
        # they typed was *cancel*, which is the word the bot itself tells them
        # to use to get out. It sat at the bottom of this function where only a
        # step that had already fallen through could reach it.
        if act == "CANCEL_ORDER":
            had_order = step != "IDLE"
            _clear_state(phone)
            return ("Order cancelled. You can start a new order anytime."
                    if had_order else
                    "Nothing to cancel — reply *1* whenever you would like to order.")

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
            if reply:
                # Answering a question mid-order used to leave the customer
                # with no idea the order was still open, or what it was
                # waiting for — the next thing they typed went into whatever
                # step they had forgotten they were on.
                waiting_for = _STEP_QUESTION.get(step)
                if waiting_for:
                    reply += (f"\n\nBack to your order — I was waiting for "
                              f"{waiting_for}. Reply *cancel* to start again.")
                return reply

        # ─── VIEW MENU ───────────────────────
        if act == "VIEW_MENU":
            # The categories and how much is in each, not every product. The
            # whole catalogue printed into a chat window is something nobody
            # reads; the website renders it properly, and the link is here.
            lines = ["*Our Menu*\n"]
            for name, items in _by_section(products):
                lines.append(f"· {name} ({len(items)})")
            lines.append(f"\nSee it all, with photographs: {SITE}/menu")
            lines.append("\nReply *order* to start, or name a category to see what is in it.")
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
            prompt, categories = _category_prompt(products)
            _set_state(phone, {"step": "SELECT_CATEGORY", "categories": categories})
            return prompt

        # ─── SELECT CATEGORY ─────────────────
        if step == "SELECT_CATEGORY":
            categories = state.get("categories") or []
            groups = dict(_by_section(products))
            typed = message.strip()

            # The number, read straight from the message. This step only picks
            # which part of the menu to show, but reading "3" out of a language
            # model when the digit is right there is a dependency with nothing
            # to gain from it.
            choice = int(typed) if typed.isdigit() else (
                action.get("value") if act == "SELECT_OPTION" else None)
            chosen = None
            if isinstance(choice, int) and 1 <= choice <= len(categories):
                chosen = categories[choice - 1]
            if chosen is None:
                for name in categories:
                    if name.lower() == typed.lower():
                        chosen = name
                        break
            if chosen is not None:
                return _product_prompt(phone, state, chosen, groups.get(chosen, []))

            # Somebody who already knows what they want types the cake, not
            # the category it lives in. Both the model's reading and the raw
            # text are tried, so "tiramisu cake" works whether or not the
            # model classified it as a product name.
            named = (action.get("name") if act == "SELECT_PRODUCT_BY_NAME" else None) or typed
            direct = _match_product(products, named)
            if direct is not None:
                return _choose_product(db, phone, state, direct)

            prompt, _ = _category_prompt(products)
            return "Sorry, I did not catch that.\n\n" + prompt

        # ─── SELECT PRODUCT ──────────────────
        if step == "SELECT_PRODUCT":
            # Rebuilt from the catalogue as it is right now, not from the copy
            # saved into the conversation when the list was printed. A cake
            # taken off sale, repriced or moved mid-conversation would
            # otherwise still be orderable at its old price for the hour the
            # state lives. The saved copy is the fallback for a category that
            # no longer exists.
            category = state.get("category")
            live = dict(_by_section(products)).get(category) if category else None
            prods = live if live else state.get("products", products)
            typed = message.strip()

            # Back to the categories. The list the customer is looking at is
            # one of a dozen, and without this the only way out of the wrong
            # one is to cancel the order.
            if typed == "0" or typed.lower() in ("back", "menu", "categories"):
                prompt, categories = _category_prompt(products)
                _set_state(phone, {"step": "SELECT_CATEGORY", "categories": categories})
                return prompt

            selected = None
            # The digit first, for the same reason as the category step.
            if typed.isdigit() and 1 <= int(typed) <= len(prods):
                selected = prods[int(typed) - 1]
            elif act == "SELECT_OPTION":
                idx = action.get("value", 0) - 1
                if 0 <= idx < len(prods): selected = prods[idx]
            if selected is None:
                name = action.get("name", "") if act == "SELECT_PRODUCT_BY_NAME" else typed
                # Within this category first, then the whole catalogue: asking
                # for a brownie while looking at the breads is a customer who
                # knows what they want, not a mistake.
                selected = _match_product(prods, name) or _match_product(products, name)
            if not selected:
                return (f"Please pick one of 1 to {len(prods)}, or type the cake's name. "
                        f"*0* goes back to the categories.")

            return _choose_product(db, phone, state, selected)

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

            # A question is never an address: this step takes whatever arrives,
            # which is how "cake above 3.5kg" became the delivery address and
            # reached the order summary as one. Questions are caught earlier —
            # the CONVERSATIONAL branch answers them and re-asks — and anything
            # the shop cannot price is caught before that, in code.
            if is_pickup:
                return _go_to_dates(db, phone, {**state, "address": "Self Pickup"})

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
                return _go_to_dates(db, phone, {**state, "address": addr})

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

            return _go_to_dates(db, phone, {**state,
                                            "delivery_zone": options[choice - 1]})

        # ─── DATE ────────────────────────────
        if step == "SELECT_DATE":
            offered = state.get("date_options") or []
            if not offered:
                # As above: an in-flight conversation that was asked the old
                # hardcoded question. Ask again from a real list.
                return _go_to_dates(db, phone, state)

            val = action.get("value", 0) if act == "SELECT_OPTION" else 0
            # Out of range is re-asked rather than clamped. `max(1, min(val, 7))`
            # silently turned a 9 into a week away and a 0 into tomorrow, so a
            # mistyped number booked a date nobody picked and the summary was
            # the first the customer heard of it.
            if not 1 <= val <= len(offered):
                listing = "\n".join(f"{i}. {o['label']}"
                                    for i, o in enumerate(offered, 1))
                return f"Please pick a date by number:\n\n{listing}"

            chosen = offered[val - 1]
            day = date.fromisoformat(chosen["date"])
            slots = slots_for_date(db, day)
            if not slots:
                # The last slot on that day went while the customer was
                # choosing. Re-ask from a freshly built list.
                return _go_to_dates(db, phone, state)

            _set_state(phone, {**state, "step": "SELECT_TIME",
                               "delivery_date": chosen["date"],
                               "delivery_date_display": day.strftime("%d %b %Y"),
                               "time_options": [{"label": s["label"], "hour": s["hour"]}
                                                for s in slots]})
            return _slot_prompt(slots)

        # ─── TIME → SUMMARY ──────────────────
        if step == "SELECT_TIME":
            offered = state.get("time_options") or []
            if not offered:
                # A conversation that reached this step before the slots were
                # computed up front - one already in flight across a deploy -
                # has a date but no list. Rebuild it from that date rather
                # than re-asking with nothing in it.
                try:
                    offered = [{"label": s["label"], "hour": s["hour"]}
                               for s in slots_for_date(
                                   db, date.fromisoformat(state["delivery_date"]))]
                except (KeyError, TypeError, ValueError):
                    offered = []
                if not offered:
                    return _go_to_dates(db, phone, state)

            val = action.get("value", 0) if act == "SELECT_OPTION" else 0
            # Was `.get(val, 14)`, so anything unrecognised became the
            # afternoon - including a slot that had already been filtered out
            # for being too soon, which is how an order could be booked inside
            # the five hours the kitchen needs.
            if not 1 <= val <= len(offered):
                return f"Please pick a time by number:\n\n" + "\n".join(
                    f"{i}. {o['label']}" for i, o in enumerate(offered, 1))

            hours = offered[val - 1]["hour"]
            time_label = offered[val - 1]["label"]

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
            # Same gate as the size line above. A fixed-price product is never
            # asked for a flavour, so printing one states a choice the customer
            # did not make - the summary read "Flavor: Classic" against the
            # rupee test cake, naming a flavour the shop does not sell.
            if flavor.get("name"):
                summary += f"Flavor: {flavor['name']}\n"
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
                        # `or ""` rather than a default flavour name. "Classic"
                        # sat here as a fallback that nothing could reach while
                        # the bot asked every customer to pick a flavour. Once
                        # fixed-price products skipped that step the fallback
                        # became live, and there is no FlavorRule called
                        # Classic - so the engine refused the whole order with
                        # "Unknown flavour: 'Classic'" and the customer was told
                        # we couldn't process it. Blank is the value the engine
                        # documents for "not chosen", and it is what _quote has
                        # always sent, which is why the quote succeeded and only
                        # the order failed.
                        customization={"size": size.get("name") or "", "flavor": (flavor or {}).get("name") or "",
                            # The WhatsApp conversation never asks about design
                            # or rush, so it must not assert values for them.
                            # Both were hardcoded to zero-cost rule names, which
                            # priced the same as "not selected" but broke
                            # outright on any deployment missing those rules.
                            "design": "", "addons": [], "rush": ""})],
                    # Recorded as what it is. The address was simply dropped
                    # before, which left the order indistinguishable from a
                    # delivery whose address had failed to save — so the
                    # kitchen packed it, a rider was assigned, and the customer
                    # was told their cake was out for delivery when they were
                    # the one coming to fetch it.
                    fulfilment="PICKUP" if addr == "Self Pickup" else "DELIVERY",
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
                # A collection has to say where to collect from. The bot used
                # to take "pickup" and then never mention it again — the
                # customer was given a payment link and left to guess where
                # the shop was.
                if order.is_pickup:
                    from app.services.store_hours import pickup_details
                    how = f"Collection: {pickup_details(db)['line']}\n"
                else:
                    how = f"Delivery to: {addr}\n"

                return (
                    f"*Order #{order.id} created*\n\n"
                    f"Amount: Rs {amount:,.2f}\n"
                    f"Payment: pending\n"
                    f"{how}\n"
                    f"Complete your payment to confirm the order:\n"
                    f"{SITE}/orders?order_id={order.id}\n\n"
                    f"We'll start baking once payment is received. "
                    f"You'll get updates on this number."
                )
            except Exception as e:
                logger.error(f"[WA ORDER] Failed: {e}")
                _clear_state(phone)
                return f"We couldn't process your order at the moment. Please try again or order online at {SITE}"

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
