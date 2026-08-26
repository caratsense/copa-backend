import enum

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Enum, func
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class WhatsAppMessageStatus(str, enum.Enum):
    PENDING = "PENDING"     # intent recorded, not yet attempted
    SENT = "SENT"           # Meta accepted it
    FAILED = "FAILED"       # attempted, still failing, may be retried
    SKIPPED = "SKIPPED"     # deliberately not sent (no consent, no phone, WA off)


class WhatsAppMessage(Base):
    """
    Durable record of every outbound WhatsApp notification we intended to send.

    Before this table a send failure was logged and lost: the order moved on and
    nobody ever learned that the baker was not actually told. The intent is now
    written first, then the send is attempted, then the outcome is recorded — so
    a Meta outage becomes a retryable backlog instead of silence.

    Deliberately not a queue engine. The order transition still commits
    independently; this only records what should have been sent and what
    happened, and `retry_pending` drains failures.
    """

    __tablename__ = "whatsapp_messages"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)

    recipient = Column(String, nullable=False)          # E.164-ish phone as sent to Meta
    recipient_role = Column(String, nullable=True)      # customer | baker | rider | admin
    event_type = Column(String, nullable=False, index=True)   # the order status that triggered it
    template_key = Column(String, nullable=False)       # internal key from wa_templates
    template_name = Column(String, nullable=True)       # the Meta name actually used
    payload = Column(JSONB, default=list)               # ordered body parameters

    status = Column(
        Enum(WhatsAppMessageStatus),
        default=WhatsAppMessageStatus.PENDING,
        nullable=False,
        index=True,
    )
    attempts = Column(Integer, default=0, nullable=False)
    meta_message_id = Column(String, nullable=True)     # wamid returned by Meta
    last_error = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    sent_at = Column(DateTime(timezone=True), nullable=True)
