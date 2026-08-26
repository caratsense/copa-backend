"""
WhatsApp Webhook — Professional, handles text/voice/image.

SECURITY: incoming POSTs are authenticated by the X-Hub-Signature-256 header
(HMAC-SHA256 of the raw body, keyed with the Meta App Secret). Without that
check, anyone who learns this URL can forge a message from any phone number —
including a staff/admin number, which would let them approve or cancel orders.
Set WHATSAPP_APP_SECRET to enable it.

Meta retries webhooks it thinks failed, so every message id is recorded in
Redis and replays are dropped. Otherwise a retry re-runs the command.
"""

import hashlib
import hmac
import json
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus, VALID_TRANSITIONS
from app.schemas import StatusUpdate
from app.services.order_service import update_order_status, _enrich_order
from app.services.assignment_engine import auto_assign_rider
from app.services.whatsapp_sender import send_text
from app.services.gemini_parser import transcribe_voice, get_audio_url
from app.services import wa_commands
from app.services.wa_customer_flow import handle_customer_message
from app.services.wa_consent import is_opt_out_request, record_opt_in, record_opt_out
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["Webhook"])
SITE = settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "") if settings.WHATSAPP_TRACKING_BASE_URL else ""


@router.get("/whatsapp")
async def verify_webhook(request: Request):
    params = request.query_params
    token = params.get("hub.verify_token") or ""
    expected = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN or ""
    challenge = params.get("hub.challenge")
    # compare_digest, matching the POST path — a plain == leaks the token length
    # and prefix through timing.
    if (
        params.get("hub.mode") == "subscribe"
        and expected
        and hmac.compare_digest(token, expected)
        and challenge
    ):
        logger.info("[WA] Webhook verified")
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


def _verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 over the raw request body."""
    if not settings.WHATSAPP_APP_SECRET:
        # Fail CLOSED. This used to return True with only a log line, which made
        # the endpoint a world-writable order-management API: anyone who knew the
        # URL could forge a message from any number — including the business
        # number published on the website — and approve, cancel or mark orders
        # paid. Refusing everything until the secret is configured is the only
        # safe default; an inbound outage is recoverable, a forged admin command
        # is not.
        logger.error(
            "[WA] WHATSAPP_APP_SECRET is not set — rejecting webhook. "
            "Set it from Meta App Dashboard > Settings > Basic > App Secret."
        )
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(header[len("sha256="):], expected)


def _already_processed(message_id: str) -> bool:
    """True if we've already handled this message id (Meta retry / duplicate)."""
    if not message_id:
        return False
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.REDIS_URL, db=2, decode_responses=True)
        # SET NX returns None when the key already exists.
        was_set = r.set(f"wa:msg:{message_id}", "1", nx=True, ex=86400)
        return not was_set
    except Exception:
        # Redis down — better to process (and risk a rare duplicate) than to
        # silently drop every real message.
        return False


def _find_user_by_phone(db: Session, sender: str) -> User | None:
    """
    Resolve a WhatsApp sender to a user.

    The suffix fallback previously passed the raw sender straight into
    SQLAlchemy's `endswith`, which builds a LIKE pattern WITHOUT escaping. A
    sender of ten underscores therefore matched the first user in the table —
    an admin — handing full admin command rights to anyone who could reach the
    webhook. Digits are now the only thing that can reach the query at all.
    """
    digits = "".join(ch for ch in (sender or "") if ch.isdigit())
    if not digits:
        return None

    user = db.query(User).filter(User.phone == f"+{digits}").first()
    if user:
        return user
    user = db.query(User).filter(User.phone == digits).first()
    if user:
        return user

    if len(digits) >= 10:
        suffix = digits[-10:]
        # autoescape=True so any wildcard in the pattern is treated literally.
        # `suffix` is digits-only by construction; this is belt and braces.
        matches = (
            db.query(User)
            .filter(User.phone.endswith(suffix, autoescape=True))
            .limit(2)
            .all()
        )
        # Two users sharing the last 10 digits (different country codes) is
        # ambiguous — refuse rather than act as the wrong person.
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("[WA] Ambiguous phone suffix %s — refusing to guess", suffix)
    return None


@router.post("/whatsapp")
async def receive_whatsapp(request: Request):
    """
    Meta inbound webhook.

    Always answers 200 once the signature checks out. Meta treats any non-2xx as
    a delivery failure and retries the whole batch, so a single unparseable
    event must not become a retry loop that re-runs the events beside it.
    """
    raw = await request.body()
    if not _verify_signature(raw, request.headers.get("x-hub-signature-256")):
        logger.warning("[WA] Rejected webhook with bad signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        body = json.loads(raw)
    except Exception:
        return {"status": "invalid"}
    if not isinstance(body, dict):
        return {"status": "invalid"}

    handled = 0
    # Meta batches: several entries, each with several changes, each with
    # several messages. Only entry[0]/changes[0]/messages[0] used to be read and
    # the rest were dropped with a 200, so Meta never retried them - staff
    # commands simply vanished under load.
    for entry in _as_list(body.get("entry")):
        for change in _as_list(_as_dict(entry).get("changes")):
            value = _as_dict(_as_dict(change).get("value"))

            for st in _as_list(value.get("statuses")):
                _log_status_callback(_as_dict(st))

            for msg in _as_list(value.get("messages")):
                try:
                    _process_message(_as_dict(msg))
                    handled += 1
                except Exception as e:
                    # One bad message must not abort its siblings or the batch.
                    logger.error("[WA] Failed to process message: %s", e, exc_info=True)

    return {"status": "ok", "handled": handled}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value) -> dict:
    """`x.get(k, {})` returns None when the key exists with a null value."""
    return value if isinstance(value, dict) else {}


def _log_status_callback(st: dict) -> None:
    """
    Delivery receipts. Template FAILURES arrive here - a wrong template name or
    language code otherwise fails silently forever.
    """
    state = st.get("status", "")
    if state == "failed":
        logger.error(
            "[WA] Send FAILED to %s (msg %s): %s",
            st.get("recipient_id"), st.get("id"), st.get("errors", []),
        )
    else:
        logger.info("[WA] Message %s -> %s", st.get("id"), state)


def _extract_text(msg: dict, sender: str) -> tuple:
    """Returns (text, early_status). A non-None early_status means stop here."""
    msg_type = msg.get("type", "")

    if msg_type == "text":
        return _as_dict(msg.get("text")).get("body", "").strip(), None

    if msg_type == "interactive":
        interactive = _as_dict(msg.get("interactive"))
        button = _as_dict(interactive.get("button_reply"))
        listed = _as_dict(interactive.get("list_reply"))
        # Prefer the payload id over the display title: ids are ours and stable,
        # titles are whatever the template renders and can be localised.
        return (button.get("id") or button.get("title")
                or listed.get("id") or listed.get("title") or ""), None

    if msg_type == "audio":
        media_id = _as_dict(msg.get("audio")).get("id", "")
        text = ""
        if media_id:
            url = get_audio_url(media_id)
            if url:
                text = transcribe_voice(url) or ""
        if not text:
            send_text(sender, "Could not process the voice message. Please type your message.")
            return "", "voice failed"
        return text, None

    if msg_type == "image":
        send_text(sender, "We cannot process images at the moment. Please describe what you need.")
        return "", "image"

    if msg_type == "reaction":
        return "", "reaction ignored"

    send_text(sender, "Please send a text or voice message.")
    return "", "unsupported"


def _process_message(msg: dict) -> str:
    """Handle exactly one inbound message."""
    sender = msg.get("from", "")
    if not sender:
        return "no sender"

    # Drop Meta retries so a command never runs twice. Checked before any side
    # effect, including the outbound replies inside _extract_text.
    if _already_processed(msg.get("id", "")):
        logger.info("[WA] Duplicate message %s ignored", msg.get("id"))
        return "duplicate"

    text, early = _extract_text(msg, sender)
    if early:
        return early
    if not text:
        return "empty"

    logger.info("[WA] inbound from %s (%d chars)", _mask(sender), len(text))

    db = SessionLocal()
    try:
        user = _find_user_by_phone(db, sender)

        # OPT-OUT - checked first, for every role. Policy requires we honour
        # "STOP" regardless of what the person was in the middle of doing.
        if is_opt_out_request(text):
            if user:
                record_opt_out(db, user)
                reply = (
                    "You've been unsubscribed from Cake O' Clock order updates on WhatsApp.\n\n"
                    "You can still order anytime at " + SITE + " - you just won't get "
                    "WhatsApp notifications.\n\n"
                    "Reply START to turn updates back on."
                )
            else:
                reply = "You're not subscribed to any Cake O' Clock updates."
            send_text(sender, reply)
            return "opted_out"

        # OPT-IN (re-subscribe)
        if user and not user.whatsapp_opt_in and text.strip().upper() in ("START", "SUBSCRIBE", "RESUME"):
            record_opt_in(db, user, source="whatsapp_reply")
            send_text(sender, "You're subscribed to Cake O' Clock order updates. Reply STOP anytime to unsubscribe.")
            return "opted_in"

        if not settings.WHATSAPP_ENABLED:
            # Outbound sending was already gated on this flag, but inbound was
            # not: commands still mutated real orders while WhatsApp was
            # nominally "disabled". Turning the integration off must mean off in
            # both directions.
            logger.warning(
                "[WA] Ignoring inbound message from %s — WHATSAPP_ENABLED is false",
                _mask(sender),
            )
            return "disabled"

        if user and user.role in (UserRole.ADMIN, UserRole.BAKER, UserRole.RIDER):
            # Staff commands are parsed deterministically - never by the language
            # model. An inferred "APPROVE 145" is indistinguishable from a typed
            # one by the time it reaches the order service.
            reply = _handle_staff(db, user, text)
        elif user:
            reply = handle_customer_message(sender, text, user)
        else:
            reply = _handle_new_user(db, sender, text)

        send_text(sender, reply)
        return "ok"
    except Exception as e:
        logger.error("[WA] Error handling message from %s: %s", _mask(sender), e, exc_info=True)
        send_text(sender, "We're experiencing a temporary issue. Please try again shortly.")
        return "error"
    finally:
        db.close()


def _mask(phone: str) -> str:
    """Phone numbers are personal data; keep only enough to correlate a log."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _handle_staff(db, user, text: str) -> str:
    """Route a deterministic staff command to the matching handler."""
    command = wa_commands.parse(text, user.role)
    if command is None:
        return wa_commands.help_text(user.role)

    if user.role == UserRole.ADMIN:
        return _handle_admin(db, user, command)
    if user.role == UserRole.BAKER:
        return _handle_baker(db, user, command)
    return _handle_rider(db, user, command)


def _resolve_target(db, user, command, valid_statuses, order_field):
    """
    Work out which order a command without an explicit number refers to.

    A baker replying just "Completed" is the flow the business actually wants,
    but guessing wrong finishes someone else's cake. So we only act when there
    is exactly one candidate:

      exactly one  -> that order
      none         -> say there is nothing to complete
      two or more  -> refuse and list the numbers

    Returns (order, error_reply). Exactly one is non-None.
    """
    if command.order_id is not None:
        order = (
            db.query(Order)
            .filter(Order.id == command.order_id, order_field == user.id)
            .first()
        )
        if not order:
            return None, f"Order #{command.order_id} is not assigned to you."
        if order.status not in valid_statuses:
            allowed = ", ".join(st.value for st in valid_statuses)
            return None, (f"Order #{order.id} is {order.status.value}. "
                          f"This command applies to: {allowed}.")
        return order, None

    candidates = (
        db.query(Order)
        .filter(order_field == user.id, Order.status.in_(valid_statuses))
        .order_by(Order.id.asc())
        .all()
    )
    if not candidates:
        return None, "You have no order at that stage right now. Reply QUEUE to see your list."
    if len(candidates) > 1:
        numbers = ", ".join(f"#{o.id}" for o in candidates)
        return None, (f"You have {len(candidates)} orders at that stage: {numbers}.\n"
                      f"Reply with the order number, e.g. \"DONE {candidates[0].id}\".")
    return candidates[0], None


def _handle_admin(db, user, command):
    act = command.action

    if act == "ADMIN_ORDERS":
        from datetime import datetime, timezone
        from sqlalchemy import func
        today = datetime.now(timezone.utc).date()
        base = db.query(Order).filter(func.date(Order.created_at) == today)
        total = base.count()
        revenue = (
            db.query(func.sum(Order.total_price))
            .filter(func.date(Order.created_at) == today,
                    Order.status != OrderStatus.CANCELLED)
            .scalar() or 0
        )
        pending = base.filter(Order.status.in_([
            OrderStatus.CONFIRMED, OrderStatus.ASSIGNED,
            OrderStatus.IN_PRODUCTION, OrderStatus.AWAITING_APPROVAL,
        ])).count()
        return (f"Today's Summary\n\nOrders: {total}\nRevenue: Rs {revenue:,.0f}\n"
                f"Pending: {pending}\n\nDashboard: {SITE}/admin")

    # Every remaining admin command targets one order.
    if act == "ADMIN_APPROVE":
        order, err = _resolve_target(db, user, command,
                                     [OrderStatus.AWAITING_APPROVAL], Order.assigned_baker_id)
        # Admins do not own orders, so re-resolve without the assignment filter.
        if err and command.order_id is not None:
            order = db.query(Order).filter(Order.id == command.order_id).first()
            if not order:
                return f"Order #{command.order_id} not found."
            if order.status != OrderStatus.AWAITING_APPROVAL:
                return f"Order #{order.id} is {order.status.value}. Only orders awaiting approval can be passed."
            err = None
        if err:
            return err
        update_order_status(db, order.id, StatusUpdate(status="PACKAGED"))
        try:
            auto_assign_rider(db, order.id, force=True)
        except Exception as e:
            logger.warning("[WA] rider auto-assign failed for order %s: %s", order.id, e)
        return f"Order #{order.id} approved and packaged."

    if command.order_id is None:
        return "Include the order number, e.g. \"APPROVE 145\"."

    order = db.query(Order).filter(Order.id == command.order_id).first()
    if not order:
        return f"Order #{command.order_id} not found."

    if act == "ADMIN_REJECT":
        if order.status != OrderStatus.AWAITING_APPROVAL:
            return f"Order #{order.id} is {order.status.value}. Only orders awaiting approval can be sent back."
        update_order_status(db, order.id, StatusUpdate(status="IN_PRODUCTION"), rework=True)
        return f"Order #{order.id} sent back to the baker."

    if act == "ADMIN_PAID":
        from app.services.order_service import update_payment_status
        from app.schemas import PaymentUpdate
        update_payment_status(db, order.id, PaymentUpdate(payment_status="PAID"))
        return f"Order #{order.id} marked as paid."

    if act == "ADMIN_CANCEL":
        allowed = VALID_TRANSITIONS.get(order.status, [])
        if OrderStatus.CANCELLED not in allowed:
            return f"Order #{order.id} is {order.status.value} and can no longer be cancelled."
        update_order_status(db, order.id, StatusUpdate(status="CANCELLED"))
        return f"Order #{order.id} cancelled."

    return wa_commands.help_text(user.role)


def _handle_baker(db, user, command):
    act = command.action

    if act == "BAKER_QUEUE":
        orders = (
            db.query(Order)
            .filter(Order.assigned_baker_id == user.id,
                    Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION]))
            .order_by(Order.delivery_time.asc().nullslast())
            .all()
        )
        if not orders:
            return "No orders in your queue."
        lines = ["*Your Queue*" + "\n"]
        for o in orders:
            _enrich_order(o)
            items = ", ".join(
                f"{i.customization.get('size', '')} {i.customization.get('flavor', '')}"
                for i in o.items
            ) if o.items else "Cake"
            state = "Waiting" if o.status == OrderStatus.ASSIGNED else "In production"
            dt = o.delivery_time.strftime("%I:%M %p") if o.delivery_time else "ASAP"
            lines.append(f"#{o.id} - {items}\nStatus: {state} | By {dt}\n")
        return "\n".join(lines)

    if act == "BAKER_START":
        order, err = _resolve_target(db, user, command,
                                     [OrderStatus.ASSIGNED], Order.assigned_baker_id)
        if err:
            return err
        update_order_status(db, order.id, StatusUpdate(status="IN_PRODUCTION"))
        return f"Order #{order.id} - production started."

    if act == "BAKER_DONE":
        order, err = _resolve_target(db, user, command,
                                     [OrderStatus.IN_PRODUCTION], Order.assigned_baker_id)
        if err:
            return err
        update_order_status(db, order.id, StatusUpdate(status="AWAITING_APPROVAL"))
        return f"Order #{order.id} - marked complete. Awaiting approval."

    return wa_commands.help_text(user.role)


def _handle_rider(db, user, command):
    act = command.action

    if act == "RIDER_QUEUE":
        orders = (
            db.query(Order)
            .filter(Order.assigned_rider_id == user.id,
                    Order.status.in_([OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY]))
            .all()
        )
        if not orders:
            return "No deliveries assigned."
        lines = ["*Your Deliveries*" + "\n"]
        for o in orders:
            _enrich_order(o)
            state = "Ready for pickup" if o.status == OrderStatus.PACKAGED else "Out for delivery"
            addr = o.delivery_address or "Self Pickup"
            lines.append(f"#{o.id} - {state}\nCustomer: {o.customer_name or 'Customer'}\n"
                         f"Phone: {o.user.phone if o.user else 'N/A'}\nAddress: {addr}")
            if addr != "Self Pickup":
                lines.append("Map: " + _maps_url(addr))
            lines.append("")
        return "\n".join(lines)

    if act == "RIDER_PICKED":
        order, err = _resolve_target(db, user, command,
                                     [OrderStatus.PACKAGED], Order.assigned_rider_id)
        if err:
            return err
        update_order_status(db, order.id, StatusUpdate(status="OUT_FOR_DELIVERY"))
        return f"Order #{order.id} - picked up. Deliver safely."

    if act == "RIDER_DELIVERED":
        order, err = _resolve_target(db, user, command,
                                     [OrderStatus.OUT_FOR_DELIVERY], Order.assigned_rider_id)
        if err:
            return err
        update_order_status(db, order.id, StatusUpdate(status="DELIVERED"))
        return f"Order #{order.id} - delivered successfully."

    return wa_commands.help_text(user.role)


def _maps_url(address: str) -> str:
    """URL-encode the address; a raw replace(" ", "+") breaks on & and #."""
    from urllib.parse import quote_plus
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def _handle_new_user(db, phone, text):
    from app.core.auth import hash_password
    import json, random, string

    # Check if they are in the consent confirmation step (Redis/memory)
    r = None
    try:
        import redis as redis_lib
        r = redis_lib.from_url(settings.REDIS_URL, db=2, decode_responses=True)
        r.ping()
    except Exception:
        r = None

    pending_key = f"wa:pending_signup:{phone}"
    pending_raw = r.get(pending_key) if r else None
    pending = json.loads(pending_raw) if pending_raw else None

    # Step 2: They replied YES to consent → create account
    if pending and text.strip().upper() in ("YES", "Y", "AGREE", "OK", "HAAN", "HA"):
        from datetime import datetime, timezone as _tz

        name = pending.get("name", "Customer")
        phone_with_plus = f"+{phone}" if not phone.startswith("+") else phone
        pwd = "".join(random.choices(string.ascii_letters + string.digits, k=12))
        # This YES *is* the WhatsApp opt-in — record it so we can demonstrate
        # consent later, and so dispatch_notifications is allowed to message them.
        new_user = User(
            name=name,
            phone=phone_with_plus,
            password_hash=hash_password(pwd),
            role=UserRole.CUSTOMER,
            is_active=True,
            whatsapp_opt_in=True,
            whatsapp_opt_in_at=datetime.now(_tz.utc),
            whatsapp_opt_in_source="whatsapp_reply",
        )
        db.add(new_user)
        db.commit()
        if r:
            r.delete(pending_key)
        return (
            f"Welcome, {name}! Your account has been created.\n\n"
            f"How can I assist you?\n"
            f"1. Place an order\n"
            f"2. Track an order\n"
            f"3. View our menu\n\n"
            f"{SITE}"
        )

    # Step 2 alt: They replied NO → clear and exit
    if pending and text.strip().upper() in ("NO", "N", "NAHI", "NAH"):
        if r:
            r.delete(pending_key)
        return "No problem! You can visit us online anytime at " + SITE

    # Step 1: They sent their name → ask for consent before creating account
    if len(text) > 1 and not text.isdigit() and len(text) < 50:
        name = text.strip().title()
        if r:
            r.setex(pending_key, 600, json.dumps({"name": name}))
        return (
            f"Hi {name}! Welcome to Cake O' Clock.\n\n"
            f"To place orders and track deliveries, we'll create a free account for you using your WhatsApp number.\n\n"
            f"By replying *YES* you agree to:\n"
            f"• Receive order updates on WhatsApp\n"
            f"• Your name and phone number being stored securely\n\n"
            f"Reply *YES* to continue or *NO* to cancel."
        )

    return (
        f"Welcome to Cake O' Clock.\n\n"
        f"We offer premium handcrafted cakes, delivered across Lucknow.\n\n"
        f"To get started, please share your name."
    )
