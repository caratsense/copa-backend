"""
Deterministic WhatsApp Staff Commands
======================================
Parses admin / baker / rider WhatsApp messages into order state transitions.

Everything here is exact, anchored and case-insensitive. No language model is
involved, deliberately: these commands move real cakes and real money, and an
inferred `APPROVE 145` is indistinguishable from a typed one once it reaches the
order service.

This replaces substring matching in the previous parser, which produced genuine
false positives:

    admin  "Looks like order 145 is wrong"  -> ADMIN_APPROVE #145   ("LOOKS" contains "OK")
    baker  "I have not started 145 yet"     -> BAKER_START  #145
    baker  "not done with 145 yet"          -> BAKER_DONE   #145
    baker  "DONE 2 cakes for order 145"     -> BAKER_DONE   #2      (first number wins)

Grammar — the command must be the FIRST word, and the order number must be the
ONLY thing after it (optionally introduced by "order"/"no"/"#"):

    DONE 145 · DONE #145 · DONE ORDER 145 · DONE NO 145 · done 145

A verb followed by anything else is ambiguous and does not parse at all, so
"DONE 2 cakes for order 145" no longer completes order 2.

A bare verb with no number returns order_id=None and needs_order_id=True; the
caller decides (see _resolve_target in the webhook: "Completed" is honoured only
when the baker has exactly one order it could mean).
"""

import re
from dataclasses import dataclass
from typing import Optional

from app.models.user import UserRole


@dataclass(frozen=True)
class Command:
    action: str
    order_id: Optional[int] = None
    # True when the verb was given without an order number, so the caller may
    # try to infer the target. False for "no command recognised at all".
    needs_order_id: bool = False


# verb -> action. Aliases include the Hinglish forms the previous parser
# accepted, so staff who already learned those keep working.
_VERBS: dict[str, dict[str, str]] = {
    "admin": {
        "APPROVE": "ADMIN_APPROVE",
        "PASS": "ADMIN_APPROVE",
        "THEEK": "ADMIN_APPROVE",
        "REJECT": "ADMIN_REJECT",
        "REWORK": "ADMIN_REJECT",
        "WAPAS": "ADMIN_REJECT",
        "PAID": "ADMIN_PAID",
        "CANCEL": "ADMIN_CANCEL",
    },
    "baker": {
        "START": "BAKER_START",
        "BEGIN": "BAKER_START",
        "SHURU": "BAKER_START",
        "DONE": "BAKER_DONE",
        "COMPLETE": "BAKER_DONE",
        "COMPLETED": "BAKER_DONE",
        "FINISH": "BAKER_DONE",
        "FINISHED": "BAKER_DONE",
        "TAYYAR": "BAKER_DONE",
    },
    "rider": {
        "PICKED": "RIDER_PICKED",
        "PICKUP": "RIDER_PICKED",
        "COLLECTED": "RIDER_PICKED",
        "UTHA": "RIDER_PICKED",
        "DELIVERED": "RIDER_DELIVERED",
        "DELIVER": "RIDER_DELIVERED",
        "DROP": "RIDER_DELIVERED",
        "PAHUNCHA": "RIDER_DELIVERED",
    },
}

# Verbs that read the queue rather than change anything.
_QUERIES: dict[str, dict[str, str]] = {
    "admin": {"ORDERS": "ADMIN_ORDERS", "TODAY": "ADMIN_ORDERS", "SUMMARY": "ADMIN_ORDERS"},
    "baker": {"QUEUE": "BAKER_QUEUE", "ORDERS": "BAKER_QUEUE"},
    "rider": {"QUEUE": "RIDER_QUEUE", "DELIVERIES": "RIDER_QUEUE"},
}

ROLE_KEY = {
    UserRole.ADMIN: "admin",
    UserRole.BAKER: "baker",
    UserRole.RIDER: "rider",
}

# Accepted grammar for the order number, anchored to the token right after the
# verb (optionally introduced by "order"/"no"/"#"):
#     DONE 145 · DONE #145 · DONE ORDER 145 · DONE ORDER #145 · DONE NO 145
# Anything else is NOT an order id. Previously this took the first integer
# anywhere after the verb, so "DONE 2 cakes for order 145" targeted order 2.
_ORDER_ID = re.compile(r"^(?:(?:order|no|order\s+no)\s*)?#?(\d{1,9})\s*$", re.IGNORECASE)


def role_key(role: UserRole) -> Optional[str]:
    return ROLE_KEY.get(role)


def parse(text: str, role: UserRole) -> Optional[Command]:
    """
    Parse a staff message. Returns None when it is not a recognised command,
    in which case the caller should reply with help rather than guess.
    """
    key = role_key(role)
    if not key or not text:
        return None

    tokens = text.strip().split()
    if not tokens:
        return None

    # Anchored: the command is the first word. "not done with 145" therefore
    # parses as verb "NOT", which matches nothing, instead of BAKER_DONE.
    verb = tokens[0].upper().strip(".,!:;#")

    if verb in _QUERIES[key]:
        return Command(action=_QUERIES[key][verb])

    action = _VERBS[key].get(verb)
    if not action:
        return None

    rest = " ".join(tokens[1:]).strip()
    if not rest:
        return Command(action=action, needs_order_id=True)

    match = _ORDER_ID.match(rest)
    if match:
        return Command(action=action, order_id=int(match.group(1)))

    # A verb followed by prose is ambiguous. Refusing to parse is what stops
    # "DONE 2 cakes for order 145" from completing order 2; the caller replies
    # with help rather than guessing.
    return None


def help_text(role: UserRole) -> str:
    """The reply sent when a message is not a recognised command."""
    key = role_key(role)
    if key == "admin":
        return ("Commands:\n"
                "APPROVE <order no> — pass quality check\n"
                "REJECT <order no> — send back to baker\n"
                "PAID <order no>\n"
                "CANCEL <order no>\n"
                "ORDERS — today's summary")
    if key == "baker":
        return ("Commands:\n"
                "START <order no> — begin baking\n"
                "DONE <order no> — mark complete\n"
                "QUEUE — your orders")
    if key == "rider":
        return ("Commands:\n"
                "PICKED <order no> — collected from bakery\n"
                "DELIVERED <order no>\n"
                "QUEUE — your deliveries")
    return "Command not recognised."
