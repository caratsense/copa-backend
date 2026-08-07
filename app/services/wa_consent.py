"""
WhatsApp consent — opt-in / opt-out bookkeeping.

WhatsApp Business policy requires that we:
  1. obtain an explicit opt-in before sending any business-initiated message,
  2. be able to demonstrate that opt-in (so we store when and how), and
  3. honour opt-out requests promptly.

Every outbound notification goes through `may_message()`. Replying inside the
24-hour customer service window is NOT business-initiated and does not need an
opt-in — those replies use send_text() directly and are unaffected.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.user import User

logger = logging.getLogger(__name__)

# Anything a person might reasonably send to make us stop.
OPT_OUT_KEYWORDS = {
    "STOP", "UNSUBSCRIBE", "OPTOUT", "OPT OUT", "OPT-OUT",
    "NO MESSAGES", "DND", "REMOVE ME", "BAND KARO", "MAT BHEJO",
}


def is_opt_out_request(text: str) -> bool:
    """True if this message is a request to stop receiving messages.

    Deliberately NOT matched to 'CANCEL' — cancelling a cake order and
    unsubscribing from messages are different intents, and conflating them
    meant STOP silently failed to unsubscribe anyone.
    """
    return (text or "").strip().upper() in OPT_OUT_KEYWORDS


def may_message(user: User | None) -> bool:
    """Whether we're allowed to send this user a business-initiated message."""
    if user is None:
        return False
    if not user.whatsapp_opt_in:
        logger.info(
            "[WA CONSENT] Skipping send to user %s - no opt-in on record", user.id
        )
        return False
    return True


def record_opt_in(db: Session, user: User, source: str) -> None:
    """Record an explicit opt-in. `source` is how consent was given."""
    if user.whatsapp_opt_in:
        return
    user.whatsapp_opt_in = True
    user.whatsapp_opt_in_at = datetime.now(timezone.utc)
    user.whatsapp_opt_in_source = source
    user.whatsapp_opt_out_at = None
    db.commit()
    logger.info("[WA CONSENT] User %s opted in via %s", user.id, source)


def record_opt_out(db: Session, user: User) -> None:
    """Honour an opt-out. Keeps the original opt-in trail for auditing."""
    user.whatsapp_opt_in = False
    user.whatsapp_opt_out_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("[WA CONSENT] User %s opted out", user.id)
