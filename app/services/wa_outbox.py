"""
WhatsApp Notification Outbox
=============================
Records the intent to notify someone, attempts the send, and records what
happened — so a Meta outage produces a retryable backlog rather than silence.

    order transition commits          (already done by the caller)
            |
    record intent          -> whatsapp_messages row, PENDING
            |
    attempt send           -> Meta Cloud API
            |
    record outcome         -> SENT (with wamid) | FAILED (with error)
            |
    retry_pending()        -> bounded redelivery of FAILED rows

Deliberately small. No Celery, no Kafka, no separate broker: one Postgres table
and a function that drains it. The order state machine never depends on any of
this succeeding.
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus
from app.services import wa_templates

logger = logging.getLogger(__name__)

# A template that Meta rejects will never succeed, so retrying it forever just
# burns API quota. Three attempts covers transient outages and timeouts.
MAX_ATTEMPTS = 3


def queue_and_send(
    db: Session,
    *,
    template_key: str,
    recipient: str,
    params: Sequence[str],
    order_id: Optional[int] = None,
    recipient_role: Optional[str] = None,
    event_type: str = "",
    skip_reason: Optional[str] = None,
) -> WhatsAppMessage:
    """
    Record the intent, then try to send it.

    `skip_reason` records a message we deliberately did not send (no opt-in, no
    phone number, WhatsApp disabled) — worth keeping, because "the baker was
    never told" is an operational fact whether or not we chose it.
    """
    template = wa_templates.get(template_key)
    row = WhatsAppMessage(
        order_id=order_id,
        recipient=recipient or "",
        recipient_role=recipient_role or template.recipient,
        event_type=event_type,
        template_key=template_key,
        template_name=template.meta_name,
        payload=[str(p) for p in params],
        status=WhatsAppMessageStatus.PENDING,
        attempts=0,
    )
    db.add(row)

    if skip_reason:
        row.status = WhatsAppMessageStatus.SKIPPED
        row.last_error = skip_reason
        _commit(db, row)
        return row

    _commit(db, row)
    _attempt(db, row)
    return row


def retry_pending(db: Session, limit: int = 50) -> dict:
    """
    Re-attempt notifications that failed but have attempts left.

    Called from the admin endpoint; safe to run repeatedly. Rows at
    MAX_ATTEMPTS are left alone so a permanently bad template stops consuming
    quota but remains visible.
    """
    rows = (
        db.query(WhatsAppMessage)
        .filter(
            WhatsAppMessage.status.in_(
                [WhatsAppMessageStatus.FAILED, WhatsAppMessageStatus.PENDING]
            ),
            WhatsAppMessage.attempts < MAX_ATTEMPTS,
        )
        .order_by(WhatsAppMessage.created_at.asc())
        .limit(limit)
        .all()
    )

    sent = failed = 0
    for row in rows:
        if _attempt(db, row):
            sent += 1
        else:
            failed += 1

    return {"retried": len(rows), "sent": sent, "failed": failed}


def _attempt(db: Session, row: WhatsAppMessage) -> bool:
    """One send attempt. Never raises — the caller's transition already happened."""
    # Imported here so tests can monkeypatch the sender module.
    from app.services import whatsapp_sender as wa

    row.attempts = (row.attempts or 0) + 1
    try:
        result = wa.send_template_raw(
            to=row.recipient,
            template_name=row.template_name,
            language=wa_templates.get(row.template_key).language,
            params=row.payload or [],
        )
    except Exception as e:                      # noqa: BLE001 — best effort by design
        row.status = WhatsAppMessageStatus.FAILED
        row.last_error = f"{type(e).__name__}: {e}"[:500]
        logger.error("[WA OUTBOX] send raised for message %s: %s", row.id, e)
        _commit(db, row)
        return False

    if result is None:
        # Sender declined to send (WhatsApp disabled, or no phone id/token).
        row.status = WhatsAppMessageStatus.SKIPPED
        row.last_error = "sender disabled or not configured"
        _commit(db, row)
        return False

    wamid = _extract_wamid(result)
    if wamid:
        row.status = WhatsAppMessageStatus.SENT
        row.meta_message_id = wamid
        row.sent_at = datetime.now(timezone.utc)
        row.last_error = None
        _commit(db, row)
        return True

    row.status = WhatsAppMessageStatus.FAILED
    row.last_error = str(result.get("error", result))[:500]
    logger.error(
        "[WA OUTBOX] Meta rejected message %s (template %s): %s",
        row.id, row.template_name, row.last_error,
    )
    _commit(db, row)
    return False


def _extract_wamid(result: dict) -> Optional[str]:
    try:
        return (result.get("messages") or [{}])[0].get("id")
    except Exception:
        return None


def _commit(db: Session, row: WhatsAppMessage) -> None:
    """
    Persist outbox bookkeeping without letting it break the caller.

    The order transition is already committed by this point; failing to write a
    notification record must never surface as an error to the person who moved
    the order.
    """
    try:
        db.commit()
    except Exception as e:
        logger.error("[WA OUTBOX] could not persist message row: %s", e)
        db.rollback()
