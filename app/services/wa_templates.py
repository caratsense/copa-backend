"""
WhatsApp Template Registry
===========================
The single place that knows which Meta template backs each business event.

Template names used to be string literals scattered across whatsapp_sender.py
and wa_notifications.py, which meant the set of templates Meta had to approve
could only be discovered by grepping, and renaming one was a code change in an
unknown number of places.

Meta-side template creation is owned by someone else. This module is the seam:
the code refers to a stable internal KEY, and the actual Meta template name is
resolved at runtime — overridable per deployment via the
WHATSAPP_TEMPLATE_NAMES environment variable without touching code.

    WHATSAPP_TEMPLATE_NAMES='{"order_confirmation": "coc_order_confirm_v2"}'

Optionally a per-template language, for when one template is approved under a
different locale than the rest:

    WHATSAPP_TEMPLATE_NAMES='{"order_delivered": {"name": "coc_delivered", "language": "en_US"}}'

`describe_all()` renders the handover table for whoever is building the
templates in WhatsApp Manager.
"""

import json
import logging
from dataclasses import dataclass, field

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Template:
    """One business event and the Meta template that carries it."""
    key: str
    default_name: str
    recipient: str          # who receives it
    trigger: str            # what causes it to send
    variables: tuple = field(default=())   # ordered body placeholders {{1}}..{{n}}

    # What to send as a plain message when Meta refuses the template.
    #
    # Six of these have been failing since they were written, with
    # "(#132001) Template name does not exist in the translation" - they were
    # never created in the Meta console. The customer was told nothing at
    # all, silently, including the one that says a collection order is ready.
    #
    # A plain message only reaches somebody who has written to the business
    # in the last 24 hours, so this does not replace getting the template
    # approved. It does mean the common case - a customer who ordered an hour
    # ago - hears something instead of nothing.
    #
    # Positional slots, matching `variables` in order.
    fallback: str = ""

    @property
    def meta_name(self) -> str:
        return _overrides().get(self.key, {}).get("name", self.default_name)

    @property
    def language(self) -> str:
        return _overrides().get(self.key, {}).get(
            "language", settings.WHATSAPP_TEMPLATE_LANG
        )


# ─── THE REGISTRY ─────────────────────────────────────
# Keys are internal and stable. `default_name` matches what the code sent before
# this registry existed, so behaviour is unchanged until an override is supplied.

TEMPLATES: dict[str, Template] = {
    t.key: t for t in [
        Template(
            key="order_confirmation",
            default_name="order_confirmation",
            recipient="customer",
            trigger="Order reaches CONFIRMED (placed on the website or WhatsApp)",
            variables=("customer_name", "order_id", "items", "total_amount", "delivery_time"),
        ),
        Template(
            key="admin_new_order",
            default_name="admin_new_order",
            recipient="admin",
            trigger="Order reaches CONFIRMED",
            variables=("order_id", "customer_name_and_phone", "items", "total_amount", "delivery_time"),
        ),
        Template(
            key="baker_new_order",
            default_name="baker_new_order",
            recipient="baker",
            trigger="Order assigned to a baker (CONFIRMED -> ASSIGNED)",
            variables=("order_id", "items", "special_instructions", "delivery_time"),
        ),
        Template(
            key="admin_approval_needed",
            default_name="admin_approval_needed",
            recipient="admin",
            trigger="Baker finishes; order reaches AWAITING_APPROVAL (quality check)",
            variables=("order_id", "items", "total_amount"),
        ),
        # ── Self pickup ──
        # The collection half of the lifecycle. None of these existed, which is
        # why a customer collecting an order was sent the delivery messages:
        # out for delivery, then delivered, both untrue.
        Template(
            key="order_ready_for_pickup",
            default_name="order_ready_for_pickup",
            recipient="customer",
            trigger="Pickup order is packed and waiting (PACKAGED -> READY_FOR_PICKUP)",
            variables=("customer_name", "order_id", "pickup_details", "slot"),
            fallback=(
                "Hi {1}, your order #{2} is ready to collect.\n\n{3}\n{4}\n\nSee you soon - Cake O' Clock"
            ),
        ),
        Template(
            key="order_collected",
            default_name="order_collected",
            recipient="customer",
            trigger="Customer collected the order (READY_FOR_PICKUP -> COLLECTED)",
            variables=("customer_name", "order_id"),
            fallback=(
                "Thank you {1}! Order #{2} has been collected. We hope you enjoy it.\n\nCake O' Clock"
            ),
        ),
        Template(
            key="order_not_collected",
            default_name="order_not_collected",
            recipient="customer",
            trigger="Pickup slot passed with the cake still on the counter",
            variables=("customer_name", "order_id", "pickup_details"),
            fallback=(
                "Hi {1}, order #{2} is still waiting for you.\n\n{3}\n\nDo let us know when you can come by - Cake O' Clock"
            ),
        ),
        Template(
            key="order_not_collected_admin",
            default_name="order_not_collected_admin",
            recipient="admin",
            trigger="A pickup nobody came for — the shop has to chase it",
            variables=("order_id", "customer_name", "customer_phone"),
            fallback=(
                "Order #{1} was not collected.\n{2} ({3})"
            ),
        ),
        Template(
            key="admin_proof_ready",
            default_name="admin_proof_ready",
            recipient="admin",
            trigger="Baker photographs the finished cake; the photo is waiting to be checked",
            variables=("order_id", "items", "photo_url"),
            fallback=(
                "Order #{1} has a finished photo waiting for approval.\n{2}\n{3}"
            ),
        ),
        Template(
            key="order_rework",
            default_name="order_rework",
            recipient="baker",
            trigger="Admin rejects at quality check (AWAITING_APPROVAL -> IN_PRODUCTION)",
            variables=("order_id",),
        ),
        Template(
            key="rider_new_delivery",
            default_name="rider_new_delivery",
            recipient="rider",
            trigger="Order packaged with a rider assigned, or a rider assigned to an already-packaged order",
            variables=("order_id", "customer_name", "customer_phone", "address", "maps_link", "amount_to_collect"),
            fallback=(
                "New delivery - order #{1}\n{2} ({3})\n{4}\n{5}\n\nCollect Rs {6}"
            ),
        ),
        Template(
            key="order_out_for_delivery",
            default_name="order_out_for_delivery",
            recipient="customer",
            trigger="Rider collects the order (PACKAGED -> OUT_FOR_DELIVERY)",
            variables=("customer_name", "order_id", "rider_name", "tracking_link"),
        ),
        Template(
            key="order_delivered",
            default_name="order_delivered",
            recipient="customer",
            trigger="Order reaches DELIVERED",
            variables=("customer_name", "order_id"),
        ),
        Template(
            key="order_delivered_admin",
            default_name="order_delivered_admin",
            recipient="admin",
            trigger="Order reaches DELIVERED",
            variables=("order_id", "customer_name"),
        ),
        Template(
            key="order_cancelled",
            default_name="order_cancelled",
            recipient="customer",
            trigger="Order reaches CANCELLED",
            variables=("customer_name", "order_id"),
        ),
        Template(
            key="order_cancelled_staff",
            default_name="order_cancelled_staff",
            recipient="baker and/or rider assigned to it",
            trigger="Order reaches CANCELLED",
            variables=("order_id",),
        ),
    ]
}


_cached_overrides: dict | None = None


def _overrides() -> dict:
    """
    Parse WHATSAPP_TEMPLATE_NAMES once.

    A malformed value must not take the notification system down, so a parse
    failure logs loudly and falls back to the built-in names.
    """
    global _cached_overrides
    if _cached_overrides is not None:
        return _cached_overrides

    raw = (settings.WHATSAPP_TEMPLATE_NAMES or "").strip()
    parsed: dict = {}
    if raw:
        try:
            data = json.loads(raw)
            for key, value in data.items():
                if key not in TEMPLATES:
                    logger.warning("[WA TEMPLATES] Unknown template key in override: %s", key)
                    continue
                parsed[key] = {"name": value} if isinstance(value, str) else dict(value)
        except Exception as e:
            logger.error("[WA TEMPLATES] WHATSAPP_TEMPLATE_NAMES is not valid JSON (%s) — using defaults", e)
            parsed = {}

    _cached_overrides = parsed
    return parsed


def get(key: str) -> Template:
    """Look up a template by internal key. Raises for an unknown key."""
    try:
        return TEMPLATES[key]
    except KeyError:
        raise KeyError(
            f"Unknown WhatsApp template key '{key}'. Known keys: {sorted(TEMPLATES)}"
        ) from None


def describe_all() -> list[dict]:
    """The handover table: what Meta templates this codebase expects to exist."""
    return [
        {
            "key": t.key,
            "meta_template_name": t.meta_name,
            "language": t.language,
            "recipient": t.recipient,
            "trigger": t.trigger,
            "variables": list(t.variables),
            "body_placeholders": len(t.variables),
        }
        for t in TEMPLATES.values()
    ]


def _reset_cache_for_tests() -> None:
    """Drop the memoised overrides. Tests change settings between cases."""
    global _cached_overrides
    _cached_overrides = None


def fallback_for(meta_name: str, params: list) -> str:
    """
    The plain-message version of a template Meta would not send, or "".

    Looked up by the name that was actually sent, because that is all the
    sender has: the registry key and the Meta name can differ once an
    override is configured, and a retry from the outbox replays the recorded
    name rather than re-resolving the key.
    """
    for template in TEMPLATES.values():
        if template.meta_name != meta_name or not template.fallback:
            continue
        slots = [""] + [str(p) for p in (params or [])]
        try:
            return template.fallback.format(*slots)
        except (IndexError, KeyError):
            # Fewer parameters than the text expects. Saying nothing is
            # better than sending a message with a gap in it.
            logger.warning("[WA TEMPLATES] %s fallback wants more parameters "
                           "than were passed", meta_name)
            return ""
    return ""
