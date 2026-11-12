from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func

from app.db.base import Base


class StockMovement(Base):
    """
    Every change to a stock count, and why it happened.

    Stock used to move with no record whatsoever. create_order decremented a
    finite add-on, nothing ever put it back, and when a count reached zero the
    item vanished from the customer's builder — so the first anybody heard of
    it was a confused customer asking where the photo prints had gone. There
    was no way to find out what had happened to them except to ask whoever had
    been standing at the counter that week.

    A count on its own cannot answer "where did the other four go", and that is
    the question somebody always asks. The count is the running total; this is
    the ledger behind it. What moved, by how much, which order or which person
    moved it, and what it was left at.

    Rows are never updated or deleted. A mistake is corrected by another
    movement, the way a ledger is corrected — otherwise the history is only as
    trustworthy as the last person to edit it.
    """

    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True, index=True)

    # Which table the item lives in: "product", "extra" or "addon". A string
    # rather than three nullable foreign keys, because the inventory screen
    # reads all three as one list and one polymorphic key is what that query
    # wants. app/services/stock_service.py holds the registry mapping a kind to
    # its model, and is the only place that mapping exists.
    kind = Column(String, nullable=False, index=True)
    item_id = Column(Integer, nullable=False, index=True)
    # The item's name as it stood at the time. Snapshotted rather than joined:
    # a product can be renamed or deleted, and a ledger that reads "deleted
    # item 14: -2" has stopped being a ledger.
    item_name = Column(String, nullable=False)

    # Signed, in units. -2 means two left the shelf.
    delta = Column(Integer, nullable=False)
    # What the count stood at afterwards, or NULL for a row that is not counted.
    # Stored rather than recomputed by summing deltas, because a count can also
    # be set outright, and because the sum of a ledger that began after stock
    # was already on the shelf does not equal the count.
    balance = Column(Integer, nullable=True)

    # Why it moved — one of the REASON_* values in stock_service. Indexed
    # because the one query that matters asks for a single reason: which units
    # are held by orders nobody has paid for.
    reason = Column(String, nullable=False, index=True)

    # The order that caused it, for a sale or a release. Null for anything a
    # person did by hand.
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    # Who did it, for anything done by hand. Null for an automatic movement, so
    # the two are told apart by what is missing rather than by a flag.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    note = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
