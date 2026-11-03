from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship

from app.db.base import Base


class AttachmentKind:
    """
    What a row is carrying. A plain string column rather than a native enum,
    deliberately: `orders.status` is a native Postgres enum and adding a value
    to it is a migration instead of a line of Python. Nothing here is worth
    that.
    """
    PHOTO = "photo"
    VOICE = "voice"
    NOTE = "note"

    ALL = (PHOTO, VOICE, NOTE)


class OrderAttachment(Base):
    """
    The brief that travels with an order: reference pictures, written
    instructions, and voice notes.

    Before this there was exactly one free-text field — `orders.notes`, which
    the customer writes at checkout — and no way at all for Shriya to tell a
    baker anything about a cake. Everything a baker needed beyond the line
    items ("the customer sent three photos of what they want", "the name is
    spelled Aarav not Arav", "do the piping like last week's") travelled by
    WhatsApp or not at all, and none of it was attached to the order it was
    about. A baker opening order #41 could not see any of it.

    Three kinds share one table because they are one conversation and are read
    in one list, in time order. A photo and a voice note each carry a
    `file_url`; a note carries `body`. A photo may carry both — a caption on a
    reference picture is worth more than the picture alone.
    """
    __tablename__ = "order_attachments"

    id = Column(Integer, primary_key=True, index=True)
    # CASCADE because a brief is meaningless without the order it describes,
    # and the Order model already cascades its items and events the same way.
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    # Nullable so deleting a staff account never deletes the instructions they
    # left on a cake. The name is denormalised below for the same reason.
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Copied at write time rather than joined. A brief has to keep saying who
    # wrote it after that person has left and their row is gone, and "Shriya"
    # beside an instruction is most of what makes it carry any authority.
    author_name = Column(String, nullable=True)
    author_role = Column(String, nullable=True)

    kind = Column(String, nullable=False)
    file_url = Column(String, nullable=True)     # /media/... for photo and voice
    body = Column(Text, nullable=True)           # the words, for a note or a caption
    # Only set for a voice note, and only when the browser reported one. Shown
    # on the play button so a baker knows whether this is a ten second remark
    # or a two minute explanation before committing to it.
    duration_seconds = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    order = relationship("Order", back_populates="attachments")
